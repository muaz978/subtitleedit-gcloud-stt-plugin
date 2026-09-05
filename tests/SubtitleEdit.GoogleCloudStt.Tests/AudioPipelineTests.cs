using SubtitleEdit.GoogleCloudStt.Media;

namespace SubtitleEdit.GoogleCloudStt.Tests;

/// <summary>
/// These exercise the real ffmpeg on the machine. They are skipped when none is present
/// rather than failing, so the suite still runs on a bare CI image.
/// </summary>
public sealed class AudioPipelineTests
{
    private const string SeBundledFfmpeg = "/Applications/Subtitle Edit.app/Contents/MacOS/ffmpeg";

    private static string? FindFfmpeg()
    {
        if (File.Exists(SeBundledFfmpeg))
        {
            return SeBundledFfmpeg;
        }

        foreach (var candidate in new[] { "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg" })
        {
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        return null;
    }

    private static string? FindSampleVideo()
    {
        var directory = "/Users/muazsabbagh/Movies/4K Video Downloader+";
        if (!Directory.Exists(directory))
        {
            return null;
        }

        return Directory.EnumerateFiles(directory, "*.mp4")
            .OrderBy(f => new FileInfo(f).Length)
            .FirstOrDefault();
    }

    [Fact]
    public void SubtitleEditBundledFfmpeg_HasNoFfprobeBesideIt()
    {
        if (!File.Exists(SeBundledFfmpeg))
        {
            return; // Subtitle Edit is not installed on this machine.
        }

        // This is the reason the pipeline must not assume ffprobe exists. If this ever
        // starts failing because SE began shipping ffprobe, the fallback simply stops
        // being exercised; nothing breaks.
        var beside = Path.Combine(Path.GetDirectoryName(SeBundledFfmpeg)!, "ffprobe");
        Assert.False(File.Exists(beside));
    }

    [Fact]
    public async Task ExtractAudio_ProducesSixteenBitMonoFlac()
    {
        var ffmpeg = FindFfmpeg();
        var video = FindSampleVideo();
        if (ffmpeg == null || video == null)
        {
            return;
        }

        var workspace = Path.Combine(Path.GetTempPath(), "se-gcstt-test-" + Guid.NewGuid().ToString("N"));
        try
        {
            var pipeline = new AudioPipeline(new FfmpegRunner(ffmpeg));

            // ExtractAudioAsync verifies its own output and throws when the format is not
            // 16 kHz mono s16, so completing without throwing is the assertion.
            var audioPath = await pipeline.ExtractAudioAsync(video, workspace, CancellationToken.None);

            Assert.True(File.Exists(audioPath));
            Assert.True(new FileInfo(audioPath).Length > 0);
        }
        finally
        {
            TryDelete(workspace);
        }
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task DurationAndVerification_WorkWithAndWithoutFfprobe(bool useProbe)
    {
        var ffmpeg = FindFfmpeg();
        var video = FindSampleVideo();
        if (ffmpeg == null || video == null)
        {
            return;
        }

        var workspace = Path.Combine(Path.GetTempPath(), "se-gcstt-test-" + Guid.NewGuid().ToString("N"));
        try
        {
            var runner = new FfmpegRunner(ffmpeg, useProbe);
            Assert.Equal(useProbe && runner.ProbePath != null, runner.HasProbe);

            var pipeline = new AudioPipeline(runner);

            var duration = await pipeline.GetDurationSecondsAsync(video, CancellationToken.None);
            Assert.True(duration > 0, $"duration should be readable (useProbe: {useProbe})");

            // ExtractAudioAsync verifies the produced format through whichever path is
            // active, and throws when it is not 16 kHz mono s16.
            var audioPath = await pipeline.ExtractAudioAsync(video, workspace, CancellationToken.None);
            Assert.True(File.Exists(audioPath));
        }
        finally
        {
            TryDelete(workspace);
        }
    }

    [Fact]
    public async Task DurationAgrees_BetweenProbeAndFfmpegFallback()
    {
        var ffmpeg = FindFfmpeg();
        var video = FindSampleVideo();
        if (ffmpeg == null || video == null || !new FfmpegRunner(ffmpeg).HasProbe)
        {
            return;
        }

        var viaProbe = await new AudioPipeline(new FfmpegRunner(ffmpeg, useProbe: true))
            .GetDurationSecondsAsync(video, CancellationToken.None);
        var viaFfmpeg = await new AudioPipeline(new FfmpegRunner(ffmpeg, useProbe: false))
            .GetDurationSecondsAsync(video, CancellationToken.None);

        // ffmpeg prints hundredths of a second, so the two can differ slightly.
        Assert.InRange(Math.Abs(viaProbe - viaFfmpeg), 0.0, 0.05);
    }

    private static void TryDelete(string directory)
    {
        try
        {
            if (Directory.Exists(directory))
            {
                Directory.Delete(directory, recursive: true);
            }
        }
        catch (Exception)
        {
            // Temp folder; the OS will reclaim it.
        }
    }
}
