using SubtitleEdit.GoogleCloudStt.Contract;
using SubtitleEdit.GoogleCloudStt.Google;
using SubtitleEdit.GoogleCloudStt.Media;
using SubtitleEdit.GoogleCloudStt.Settings;
using SubtitleEdit.GoogleCloudStt.Transcription;
using System.Globalization;

namespace SubtitleEdit.GoogleCloudStt;

/// <summary>
/// Decides what a single plugin invocation does, and shapes the response so the host
/// behaves the way the user expects.
/// </summary>
public static class PluginHost
{
    public static async Task<PluginResponse> RunAsync(
        PluginRequest request,
        PluginSettings settings,
        IProgress<TranscriptionProgress>? progress,
        CancellationToken cancellationToken)
    {
        var ffmpegPath = FfmpegLocator.Locate(request.PluginDataDirectory);
        if (ffmpegPath == null)
        {
            return Error(
                "ffmpeg was not found. Subtitle Edit can download it for you under Options, Settings, or you can install ffmpeg and set its path there. This plugin uses the same copy Subtitle Edit does.");
        }

        var workingDirectory = Path.Combine(
            string.IsNullOrWhiteSpace(request.TempDirectory) ? Path.GetTempPath() : request.TempDirectory,
            "transcription");

        try
        {
            var pipeline = new TranscriptionPipeline(new AudioPipeline(new FfmpegRunner(ffmpegPath)), workingDirectory);
            var result = await pipeline.RunAsync(request.VideoFileName, settings, progress, cancellationToken);

            if (result.Cues.Count == 0)
            {
                // "ok" with no subtitle means "show this and change nothing". Returning an
                // empty SRT body instead would trip the host's unparsable subtitle error.
                return SettingsOnly(
                    settings,
                    "Google Cloud found no speech in this video, so the subtitle was left unchanged.");
            }

            return new PluginResponse
            {
                Status = PluginStatus.Ok,
                Message = Summarize(result, settings),
                Subtitle = new PluginResponseSubtitle
                {
                    Format = "SubRip",
                    Native = SrtBuilder.ToSrt(result.Cues),
                },
                Settings = settings.ToJsonElement(),
                SettingsVersion = PluginSettings.CurrentVersion,
                UndoDescription = "Google Cloud Speech-to-Text",
            };
        }
        catch (TranscriptionException exception)
        {
            return Error(exception.Message);
        }
        catch (FfmpegException exception)
        {
            return Error(exception.Message);
        }
    }

    /// <summary>Checks that can be made before any work or credentials are needed.</summary>
    public static string? DescribeBlockingProblem(PluginRequest request)
    {
        if (string.IsNullOrWhiteSpace(request.VideoFileName) || !File.Exists(request.VideoFileName))
        {
            return "Open the video you want to transcribe in Subtitle Edit first. This plugin reads the audio from the video that is currently loaded.";
        }

        return FfmpegLocator.Locate(request.PluginDataDirectory) == null
            ? "ffmpeg was not found. Subtitle Edit can download it for you under Options, Settings, or you can install ffmpeg and set its path there."
            : null;
    }

    private static string Summarize(TranscriptionResult result, PluginSettings settings)
    {
        var parts = new List<string>
        {
            string.Create(CultureInfo.InvariantCulture,
                $"{result.Cues.Count} lines from {result.WordCount} timed words ({result.AudioMinutes:F0} min, about ${result.EstimatedCostUsd(settings.UseDynamicBatching):F2})"),
        };

        if (result.RecoveredChunkCount > 0)
        {
            parts.Add(string.Create(CultureInfo.InvariantCulture,
                $"recovered {result.RecoveredChunkCount} truncated part(s)"));
        }

        if (result.DroppedWordCount > 0)
        {
            parts.Add(string.Create(CultureInfo.InvariantCulture,
                $"discarded {result.DroppedWordCount} word(s) with impossible timings"));
        }

        return string.Join(", ", parts) + ".";
    }

    /// <summary>
    /// Returns "ok" with no subtitle. The host reads that as "show this message and change
    /// nothing", and critically it is the only status that persists settings: the cancelled
    /// and error paths both return before SavePluginSettings runs.
    /// </summary>
    public static PluginResponse SettingsOnly(PluginSettings settings, string message) => new()
    {
        Status = PluginStatus.Ok,
        Message = message,
        Settings = settings.ToJsonElement(),
        SettingsVersion = PluginSettings.CurrentVersion,
    };

    public static PluginResponse Error(string message) => new()
    {
        Status = PluginStatus.Error,
        Message = message,
    };
}
