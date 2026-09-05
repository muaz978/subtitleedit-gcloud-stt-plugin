using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace SubtitleEdit.GoogleCloudStt.Media;

/// <summary>
/// Finds an ffmpeg this plugin can run.
///
/// Subtitle Edit does not tell a plugin where its ffmpeg is, so this mirrors SE's own
/// resolution order instead of bundling a copy. Bundling would add 20 to 40 MB of GPL
/// binary per platform to a plugin zip, and SE already owns the "download ffmpeg" prompt.
/// </summary>
public static class FfmpegLocator
{
    private static readonly string ExecutableName =
        RuntimeInformation.IsOSPlatform(OSPlatform.Windows) ? "ffmpeg.exe" : "ffmpeg";

    /// <summary>
    /// Derives Subtitle Edit's data folder from the request's pluginDataDirectory, which the
    /// host builds as &lt;DataFolder&gt;/Plugins/Data/&lt;plugin name&gt;. Walking three levels up is
    /// exact even for portable installs, where assuming %APPDATA% would be wrong.
    /// </summary>
    public static string? GetSubtitleEditDataFolder(string pluginDataDirectory)
    {
        if (string.IsNullOrWhiteSpace(pluginDataDirectory))
        {
            return null;
        }

        var dataDirectory = new DirectoryInfo(pluginDataDirectory);
        var dataFolder = dataDirectory.Parent?.Parent?.Parent;
        return dataFolder is { Exists: true } ? dataFolder.FullName : null;
    }

    /// <summary>Returns a working ffmpeg path, or null when nothing usable was found.</summary>
    public static string? Locate(string pluginDataDirectory)
    {
        foreach (var candidate in EnumerateCandidates(pluginDataDirectory))
        {
            if (!string.IsNullOrWhiteSpace(candidate) && IsUsable(candidate))
            {
                return candidate;
            }
        }

        return null;
    }

    private static IEnumerable<string?> EnumerateCandidates(string pluginDataDirectory)
    {
        var dataFolder = GetSubtitleEditDataFolder(pluginDataDirectory);

        // 1. Whatever Subtitle Edit itself is configured to use.
        yield return ReadConfiguredFfmpegPath(dataFolder);

        // 2. The copy Subtitle Edit downloads on demand.
        if (dataFolder != null)
        {
            yield return Path.Combine(dataFolder, "ffmpeg", ExecutableName);
        }

        // 3. PATH, scanned manually rather than shelling out to which/where, so a broken
        //    entry can never hang the plugin.
        foreach (var fromPath in EnumeratePathMatches())
        {
            yield return fromPath;
        }

        // 4. Well known absolute locations, including SE's own signed bundle on macOS.
        foreach (var known in KnownLocations())
        {
            yield return known;
        }
    }

    private static string? ReadConfiguredFfmpegPath(string? dataFolder)
    {
        if (dataFolder == null)
        {
            return null;
        }

        var settingsPath = Path.Combine(dataFolder, "Settings.json");
        if (!File.Exists(settingsPath))
        {
            return null;
        }

        try
        {
            using var stream = File.OpenRead(settingsPath);
            using var document = JsonDocument.Parse(stream, new JsonDocumentOptions
            {
                CommentHandling = JsonCommentHandling.Skip,
                AllowTrailingCommas = true,
            });

            // Subtitle Edit writes PascalCase keys: General.FfmpegPath.
            if (document.RootElement.TryGetProperty("General", out var general) &&
                general.TryGetProperty("FfmpegPath", out var ffmpegPath) &&
                ffmpegPath.ValueKind == JsonValueKind.String)
            {
                return ffmpegPath.GetString();
            }
        }
        catch (Exception)
        {
            // A malformed or partially written Settings.json just means this hint is
            // unavailable; the remaining candidates still apply.
        }

        return null;
    }

    private static IEnumerable<string> EnumeratePathMatches()
    {
        var path = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrWhiteSpace(path))
        {
            yield break;
        }

        foreach (var directory in path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            string candidate;
            try
            {
                candidate = Path.Combine(directory.Trim(), ExecutableName);
            }
            catch (ArgumentException)
            {
                continue; // A PATH entry with invalid characters.
            }

            yield return candidate;
        }
    }

    private static IEnumerable<string> KnownLocations()
    {
        if (RuntimeInformation.IsOSPlatform(OSPlatform.OSX))
        {
            // Subtitle Edit ships a signed ffmpeg inside its own app bundle.
            yield return "/Applications/Subtitle Edit.app/Contents/MacOS/ffmpeg";
            yield return "/opt/homebrew/bin/ffmpeg";
            yield return "/usr/local/bin/ffmpeg";
            yield return "/opt/local/bin/ffmpeg";
        }

        if (!RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
        {
            yield return "/usr/bin/ffmpeg";
            yield return "/usr/local/bin/ffmpeg";
            yield return "/snap/bin/ffmpeg";
        }
    }

    /// <summary>
    /// Existence is not enough: a stale configured path or a shim on PATH can point at
    /// something that is not runnable. Probe it, with a bound so a hung binary cannot
    /// stall the plugin. Subtitle Edit gives plugins no timeout of their own.
    /// </summary>
    private static bool IsUsable(string candidate)
    {
        if (!File.Exists(candidate))
        {
            return false;
        }

        try
        {
            using var process = Process.Start(new ProcessStartInfo
            {
                FileName = candidate,
                ArgumentList = { "-version" },
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            });

            if (process == null)
            {
                return false;
            }

            if (!process.WaitForExit(TimeSpan.FromSeconds(10)))
            {
                try
                {
                    process.Kill(entireProcessTree: true);
                }
                catch (Exception)
                {
                    // Already gone.
                }

                return false;
            }

            return process.ExitCode == 0;
        }
        catch (Exception)
        {
            return false;
        }
    }
}
