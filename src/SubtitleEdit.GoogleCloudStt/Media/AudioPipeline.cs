using System.Globalization;
using System.Text.RegularExpressions;

namespace SubtitleEdit.GoogleCloudStt.Media;

/// <summary>One chunk of extracted audio, with the offset its timings must be shifted by.</summary>
public sealed record AudioChunk(int Index, string Path, double StartSeconds, double DurationSeconds)
{
    public double EndSeconds => StartSeconds + DurationSeconds;
}

/// <summary>
/// Turns the loaded video into FLAC chunks ready for BatchRecognize.
/// </summary>
public sealed class AudioPipeline
{
    /// <summary>
    /// chirp_3 caps BatchRecognize at 20 minutes per file when word level timestamps are
    /// enabled. 18 minutes sits comfortably under that without wasting requests.
    /// </summary>
    public const double ChunkSeconds = 1080.0;

    private const int TargetSampleRate = 16000;
    private const int TargetChannels = 1;
    private const string TargetSampleFormat = "s16";

    private static readonly Regex DurationPattern =
        new(@"Duration:\s*(\d+):(\d{2}):(\d{2}\.\d+)", RegexOptions.Compiled);

    private static readonly Regex AudioStreamPattern =
        new(@"Audio:\s*\w+[^,]*,\s*(\d+)\s*Hz,\s*(mono|stereo)[^,]*,\s*(\w+)", RegexOptions.Compiled);

    private readonly FfmpegRunner _ffmpeg;

    public AudioPipeline(FfmpegRunner ffmpeg)
    {
        _ffmpeg = ffmpeg;
    }

    /// <summary>
    /// Reads the duration of the ORIGINAL media. Never probe the stream copied segments
    /// for this: the segment muxer copies the source header, so every 18 minute segment
    /// reports the full source duration instead of its own.
    /// </summary>
    public async Task<double> GetDurationSecondsAsync(string mediaPath, CancellationToken cancellationToken)
    {
        if (_ffmpeg.HasProbe)
        {
            var output = await _ffmpeg.RunProbeAsync(
                [
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    mediaPath,
                ],
                cancellationToken);

            var text = output.Trim();
            if (double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var seconds) && seconds > 0)
            {
                return seconds;
            }
        }

        // No ffprobe beside this ffmpeg, which is the normal case for Subtitle Edit's own
        // macOS bundle. ffmpeg reports the same duration on stderr.
        var described = await _ffmpeg.DescribeWithFfmpegAsync(mediaPath, cancellationToken);
        var match = DurationPattern.Match(described);
        if (match.Success &&
            int.TryParse(match.Groups[1].Value, out var hours) &&
            int.TryParse(match.Groups[2].Value, out var minutes) &&
            double.TryParse(match.Groups[3].Value, NumberStyles.Float, CultureInfo.InvariantCulture, out var secondsPart))
        {
            var total = (hours * 3600.0) + (minutes * 60.0) + secondsPart;
            if (total > 0)
            {
                return total;
            }
        }

        throw new FfmpegException($"Could not read the duration of '{Path.GetFileName(mediaPath)}'.");
    }

    /// <summary>
    /// Extracts 16 kHz mono 16 bit FLAC, then verifies what was actually produced.
    ///
    /// The verification is not defensive padding. Passing -sample_fmt before -c:a lets
    /// ffmpeg select the encoder's own default instead, which silently yields 24 bit FLAC
    /// from an AAC source. That is larger than raw 16 bit PCM would have been, so an
    /// upload that should shrink instead grows by roughly a third.
    /// </summary>
    public async Task<string> ExtractAudioAsync(string videoPath, string workingDirectory, CancellationToken cancellationToken)
    {
        Directory.CreateDirectory(workingDirectory);
        var outputPath = Path.Combine(workingDirectory, "audio.flac");

        await _ffmpeg.RunFfmpegAsync(
            [
                "-hide_banner", "-nostdin", "-y",
                "-i", videoPath,
                "-vn",
                "-ac", TargetChannels.ToString(CultureInfo.InvariantCulture),
                "-ar", TargetSampleRate.ToString(CultureInfo.InvariantCulture),
                // Encoder first, then the sample format, so the format applies to it.
                "-c:a", "flac",
                "-sample_fmt", TargetSampleFormat,
                "-compression_level", "8",
                outputPath,
            ],
            cancellationToken);

        await VerifyExtractedAudioAsync(outputPath, cancellationToken);
        return outputPath;
    }

    private async Task VerifyExtractedAudioAsync(string audioPath, CancellationToken cancellationToken)
    {
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        if (_ffmpeg.HasProbe)
        {
            var output = await _ffmpeg.RunProbeAsync(
                [
                    "-v", "error",
                    "-select_streams", "a:0",
                    "-show_entries", "stream=sample_fmt,sample_rate,channels",
                    "-of", "default=noprint_wrappers=1",
                    audioPath,
                ],
                cancellationToken);

            foreach (var line in output.Split('\n', StringSplitOptions.RemoveEmptyEntries))
            {
                var parts = line.Split('=', 2);
                if (parts.Length == 2)
                {
                    values[parts[0].Trim()] = parts[1].Trim();
                }
            }
        }
        else
        {
            // Parse ffmpeg's own stream line, e.g.
            //   Stream #0:0: Audio: flac, 16000 Hz, mono, s16
            var described = await _ffmpeg.DescribeWithFfmpegAsync(audioPath, cancellationToken);
            var match = AudioStreamPattern.Match(described);
            if (match.Success)
            {
                values["sample_rate"] = match.Groups[1].Value;
                values["channels"] = match.Groups[2].Value.Equals("mono", StringComparison.OrdinalIgnoreCase) ? "1" : "2";
                values["sample_fmt"] = match.Groups[3].Value;
            }
        }

        var problems = new List<string>();

        if (values.TryGetValue("sample_fmt", out var sampleFormat) &&
            !sampleFormat.Equals(TargetSampleFormat, StringComparison.OrdinalIgnoreCase))
        {
            problems.Add($"sample format is {sampleFormat}, expected {TargetSampleFormat}");
        }

        if (values.TryGetValue("sample_rate", out var sampleRate) &&
            sampleRate != TargetSampleRate.ToString(CultureInfo.InvariantCulture))
        {
            problems.Add($"sample rate is {sampleRate}, expected {TargetSampleRate}");
        }

        if (values.TryGetValue("channels", out var channels) &&
            channels != TargetChannels.ToString(CultureInfo.InvariantCulture))
        {
            problems.Add($"channel count is {channels}, expected {TargetChannels}");
        }

        if (problems.Count > 0)
        {
            throw new FfmpegException(
                "The extracted audio is not in the expected format (" + string.Join("; ", problems) +
                "). This would inflate the upload and may change the transcription result, so the run was stopped.");
        }
    }

    /// <summary>
    /// Cuts the tail of a chunk that recognition stopped part way through, so it can be
    /// resubmitted on its own.
    ///
    /// This re-encodes rather than stream copies, and passes -sample_fmt explicitly.
    /// Omitting it yields 24 bit FLAC, which is larger than the raw 16 bit PCM would have
    /// been and makes the recovery upload slower than the original.
    /// </summary>
    public async Task<string> ExtractRemainderAsync(
        AudioChunk chunk,
        double fromSecondsWithinChunk,
        string workingDirectory,
        CancellationToken cancellationToken)
    {
        var recoveryDirectory = Path.Combine(workingDirectory, "recovery");
        Directory.CreateDirectory(recoveryDirectory);

        var outputPath = Path.Combine(
            recoveryDirectory,
            $"part-{chunk.Index:D3}-from-{(int)fromSecondsWithinChunk}.flac");

        await _ffmpeg.RunFfmpegAsync(
            [
                "-hide_banner", "-nostdin", "-y",
                "-ss", fromSecondsWithinChunk.ToString("F3", CultureInfo.InvariantCulture),
                "-i", chunk.Path,
                "-vn",
                "-ac", TargetChannels.ToString(CultureInfo.InvariantCulture),
                "-ar", TargetSampleRate.ToString(CultureInfo.InvariantCulture),
                "-c:a", "flac",
                "-sample_fmt", TargetSampleFormat,
                "-compression_level", "8",
                outputPath,
            ],
            cancellationToken);

        await VerifyExtractedAudioAsync(outputPath, cancellationToken);
        return outputPath;
    }

    /// <summary>
    /// Splits the extracted audio into fixed length chunks by stream copy.
    ///
    /// Chunk boundaries are computed arithmetically rather than probed, for the reason
    /// given on GetDurationSecondsAsync: a stream copied segment lies about its duration.
    /// </summary>
    public async Task<IReadOnlyList<AudioChunk>> SplitAsync(
        string audioPath,
        double totalDurationSeconds,
        string workingDirectory,
        CancellationToken cancellationToken)
    {
        var chunkDirectory = Path.Combine(workingDirectory, "chunks");
        Directory.CreateDirectory(chunkDirectory);

        if (totalDurationSeconds <= ChunkSeconds)
        {
            return [new AudioChunk(0, audioPath, 0.0, totalDurationSeconds)];
        }

        await _ffmpeg.RunFfmpegAsync(
            [
                "-hide_banner", "-nostdin", "-y",
                "-i", audioPath,
                "-f", "segment",
                "-segment_time", ChunkSeconds.ToString(CultureInfo.InvariantCulture),
                "-c", "copy",
                Path.Combine(chunkDirectory, "part-%03d.flac"),
            ],
            cancellationToken);

        var files = Directory.GetFiles(chunkDirectory, "part-*.flac");
        Array.Sort(files, StringComparer.Ordinal);

        if (files.Length == 0)
        {
            throw new FfmpegException("Splitting the audio produced no chunks.");
        }

        var chunks = new List<AudioChunk>(files.Length);
        for (var index = 0; index < files.Length; index++)
        {
            var start = index * ChunkSeconds;
            var duration = Math.Min(ChunkSeconds, Math.Max(0.0, totalDurationSeconds - start));
            chunks.Add(new AudioChunk(index, files[index], start, duration));
        }

        return chunks;
    }
}
