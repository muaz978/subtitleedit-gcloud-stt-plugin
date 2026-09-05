using SubtitleEdit.GoogleCloudStt.Google;
using SubtitleEdit.GoogleCloudStt.Media;
using SubtitleEdit.GoogleCloudStt.Settings;

namespace SubtitleEdit.GoogleCloudStt.Transcription;

public sealed record TranscriptionProgress(string Stage, double Fraction, string? Detail = null);

public sealed record TranscriptionResult(
    IReadOnlyList<SubtitleCue> Cues,
    int WordCount,
    int DroppedWordCount,
    int RecoveredChunkCount,
    double AudioMinutes)
{
    /// <summary>
    /// Indicative only. Google bills per the audio it processes, and recovery resubmissions
    /// are billed again, so this is the floor rather than the exact charge.
    /// </summary>
    public double EstimatedCostUsd(bool dynamicBatching)
        => AudioMinutes * (dynamicBatching ? 0.003 : 0.016);
}

/// <summary>
/// Runs one transcription end to end: extract, chunk, upload, recognize, guard, assemble.
/// </summary>
public sealed class TranscriptionPipeline
{
    /// <summary>
    /// Chunks recognize concurrently, but not unboundedly: each one holds an upload and a
    /// long running operation, and a whole episode at once invites quota rejections.
    /// </summary>
    private const int MaxConcurrentChunks = 4;

    private readonly AudioPipeline _audio;
    private readonly string _workingDirectory;

    public TranscriptionPipeline(AudioPipeline audio, string workingDirectory)
    {
        _audio = audio;
        _workingDirectory = workingDirectory;
    }

    public async Task<TranscriptionResult> RunAsync(
        string videoFileName,
        PluginSettings settings,
        IProgress<TranscriptionProgress>? progress,
        CancellationToken cancellationToken)
    {
        progress?.Report(new TranscriptionProgress("Reading the video", 0.02));
        var totalSeconds = await _audio.GetDurationSecondsAsync(videoFileName, cancellationToken);

        progress?.Report(new TranscriptionProgress("Extracting audio", 0.05));
        var audioPath = await _audio.ExtractAudioAsync(videoFileName, _workingDirectory, cancellationToken);

        progress?.Report(new TranscriptionProgress("Splitting audio", 0.10));
        var chunks = await _audio.SplitAsync(audioPath, totalSeconds, _workingDirectory, cancellationToken);

        var credential = SpeechCredentials.Load(settings.CredentialsPath);
        var projectId = ResolveProjectId(settings);
        var bucketName = string.IsNullOrWhiteSpace(settings.BucketName)
            ? GcsWorkspace.DeriveBucketName(projectId)
            : settings.BucketName;

        progress?.Report(new TranscriptionProgress("Preparing Cloud Storage", 0.12, bucketName));
        await using var workspace = await GcsWorkspace.OpenAsync(
            credential,
            projectId,
            bucketName,
            BucketLocationFor(settings.Region),
            $"subtitle-edit/{DateTimeOffset.UtcNow:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}",
            cancellationToken);

        settings.BucketName = bucketName;

        var speech = await SpeechBatchClient.CreateAsync(credential, projectId, settings.Region, cancellationToken);

        var transcripts = await RecognizeAllAsync(
            chunks, workspace, speech, settings, progress, cancellationToken);

        // The model guard runs before anything is assembled, so a model that stops
        // returning timings fails loudly instead of yielding a plausible looking subtitle.
        TranscriptGuards.EnsureWordTimingsWereReturned(transcripts, settings.Model);

        progress?.Report(new TranscriptionProgress("Checking coverage", 0.85));
        var (words, dropped, recovered) = await AssembleAsync(
            chunks, transcripts, workspace, speech, settings, progress, cancellationToken);

        progress?.Report(new TranscriptionProgress("Building subtitle", 0.95));
        var ordered = words.OrderBy(w => w.StartSeconds).ToList();
        var cues = SrtBuilder.BuildCues(ordered);

        progress?.Report(new TranscriptionProgress("Done", 1.0));
        return new TranscriptionResult(cues, ordered.Count, dropped, recovered, totalSeconds / 60.0);
    }

    private static string ResolveProjectId(PluginSettings settings)
    {
        if (!string.IsNullOrWhiteSpace(settings.ProjectId))
        {
            return settings.ProjectId;
        }

        var fromKey = SpeechCredentials.ReadProjectId(settings.CredentialsPath);
        if (!string.IsNullOrWhiteSpace(fromKey))
        {
            settings.ProjectId = fromKey;
            return fromKey;
        }

        throw new TranscriptionException(
            "The Google Cloud project could not be determined. The service account key file has no project_id, so set the project explicitly in the plugin settings.");
    }

    /// <summary>Speech's "us" is a multi region, and the bucket should match it.</summary>
    private static string BucketLocationFor(string region)
        => region.Equals("us", StringComparison.OrdinalIgnoreCase) ? "US"
            : region.Equals("eu", StringComparison.OrdinalIgnoreCase) ? "EU"
            : region.ToUpperInvariant();

    private static async Task<List<ChunkTranscript>> RecognizeAllAsync(
        IReadOnlyList<AudioChunk> chunks,
        GcsWorkspace workspace,
        SpeechBatchClient speech,
        PluginSettings settings,
        IProgress<TranscriptionProgress>? progress,
        CancellationToken cancellationToken)
    {
        var results = new ChunkTranscript[chunks.Count];
        var completed = 0;
        using var gate = new SemaphoreSlim(MaxConcurrentChunks);

        var tasks = chunks.Select(async chunk =>
        {
            await gate.WaitAsync(cancellationToken);
            try
            {
                var uri = await workspace.UploadAsync(chunk.Path, cancellationToken);
                var transcript = await speech.RecognizeAsync(
                    chunk.Index, uri, settings.LanguageCode, settings.Model, settings.UseDynamicBatching, cancellationToken);

                results[chunk.Index] = transcript;

                var done = Interlocked.Increment(ref completed);
                progress?.Report(new TranscriptionProgress(
                    "Transcribing",
                    0.15 + (0.70 * done / chunks.Count),
                    $"part {done} of {chunks.Count}"));
            }
            finally
            {
                gate.Release();
            }
        });

        await Task.WhenAll(tasks);
        return [.. results];
    }

    /// <summary>
    /// Range checks each chunk, recovers any that were silently truncated, and shifts every
    /// word onto the timeline of the whole media.
    /// </summary>
    private async Task<(List<WordTiming> Words, int Dropped, int Recovered)> AssembleAsync(
        IReadOnlyList<AudioChunk> chunks,
        IReadOnlyList<ChunkTranscript> transcripts,
        GcsWorkspace workspace,
        SpeechBatchClient speech,
        PluginSettings settings,
        IProgress<TranscriptionProgress>? progress,
        CancellationToken cancellationToken)
    {
        var words = new List<WordTiming>();
        var droppedTotal = 0;
        var recovered = 0;

        foreach (var chunk in chunks)
        {
            var transcript = transcripts[chunk.Index];
            var kept = TranscriptGuards.DropImpossibleOffsets(transcript.Words, chunk.DurationSeconds, out var dropped);
            droppedTotal += dropped;

            var covered = kept.Count == 0 ? 0.0 : kept[^1].EndSeconds;

            if (TranscriptGuards.LooksTruncated(covered, chunk.DurationSeconds))
            {
                progress?.Report(new TranscriptionProgress(
                    "Recovering a truncated part",
                    0.88,
                    $"part {chunk.Index + 1} stopped at {covered:F0}s of {chunk.DurationSeconds:F0}s"));

                var recoveredWords = await RecoverAsync(
                    chunk, covered, workspace, speech, settings, cancellationToken);

                if (recoveredWords.Count > 0)
                {
                    kept = [.. kept, .. recoveredWords];
                    recovered++;
                }
            }

            words.AddRange(kept.Select(w => w.Shift(chunk.StartSeconds)));
        }

        return (words, droppedTotal, recovered);
    }

    private async Task<IReadOnlyList<WordTiming>> RecoverAsync(
        AudioChunk chunk,
        double coveredSeconds,
        GcsWorkspace workspace,
        SpeechBatchClient speech,
        PluginSettings settings,
        CancellationToken cancellationToken)
    {
        // Start slightly before the last word so nothing falls between the two passes.
        var resumeAt = Math.Max(0.0, coveredSeconds - 1.0);

        var remainderPath = await _audio.ExtractRemainderAsync(chunk, resumeAt, _workingDirectory, cancellationToken);
        var uri = await workspace.UploadAsync(remainderPath, cancellationToken);

        var transcript = await speech.RecognizeAsync(
            chunk.Index, uri, settings.LanguageCode, settings.Model, settings.UseDynamicBatching, cancellationToken);

        var kept = TranscriptGuards.DropImpossibleOffsets(
            transcript.Words, chunk.DurationSeconds - resumeAt, out _);

        // The remainder's timings restart at zero, so they need the resume point added back
        // before they can join the rest of the chunk.
        return [.. kept.Select(w => w.Shift(resumeAt)).Where(w => w.StartSeconds > coveredSeconds - 0.5)];
    }
}
