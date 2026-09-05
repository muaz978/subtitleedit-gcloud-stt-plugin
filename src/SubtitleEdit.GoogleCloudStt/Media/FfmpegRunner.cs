using System.Diagnostics;
using System.Text;

namespace SubtitleEdit.GoogleCloudStt.Media;

/// <summary>Thrown when ffmpeg or ffprobe fails, carrying the tail of its stderr.</summary>
public sealed class FfmpegException : Exception
{
    public FfmpegException(string message) : base(message)
    {
    }
}

/// <summary>
/// Runs ffmpeg and ffprobe. Every call is cancellable, because Subtitle Edit cannot
/// interrupt this plugin once it starts: a stuck child process would otherwise wedge
/// the host's menu action permanently.
/// </summary>
public sealed class FfmpegRunner
{
    private readonly string _ffmpegPath;
    private readonly Lazy<string?> _probePath;

    /// <param name="ffmpegPath">Path to a working ffmpeg.</param>
    /// <param name="useProbe">
    /// Set false to ignore any ffprobe on the machine and read media details from ffmpeg's
    /// own output instead. That is the path taken on installs where Subtitle Edit's bundled
    /// ffmpeg is the only binary available, and it exists as a parameter so the fallback can
    /// be tested on machines that do happen to have ffprobe.
    /// </param>
    public FfmpegRunner(string ffmpegPath, bool useProbe = true)
    {
        _ffmpegPath = ffmpegPath;
        _probePath = new Lazy<string?>(() => useProbe ? ResolveProbePath() : null);
    }

    /// <summary>True when a real ffprobe is available for structured queries.</summary>
    public bool HasProbe => ProbePath != null;

    /// <summary>
    /// Path to a real ffprobe, or null when none exists.
    ///
    /// Do NOT fall back to ffmpeg here. Subtitle Edit's macOS bundle ships ffmpeg with no
    /// ffprobe beside it, which is the common case for the users this plugin targets, and
    /// ffmpeg rejects ffprobe's options outright ("Unrecognized option 'select_streams'").
    /// A silent fallback would fail for exactly those users. Callers handle null by
    /// parsing ffmpeg's own diagnostic output instead.
    /// </summary>
    public string? ProbePath => _probePath.Value;

    private string? ResolveProbePath()
    {
        var candidates = new List<string>();

        var directory = Path.GetDirectoryName(_ffmpegPath);
        if (!string.IsNullOrEmpty(directory))
        {
            var fileName = Path.GetFileName(_ffmpegPath);
            candidates.Add(Path.Combine(directory, fileName.Replace("ffmpeg", "ffprobe", StringComparison.OrdinalIgnoreCase)));
        }

        var probeName = OperatingSystem.IsWindows() ? "ffprobe.exe" : "ffprobe";
        var path = Environment.GetEnvironmentVariable("PATH");
        if (!string.IsNullOrWhiteSpace(path))
        {
            foreach (var entry in path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
            {
                try
                {
                    candidates.Add(Path.Combine(entry.Trim(), probeName));
                }
                catch (ArgumentException)
                {
                    // A PATH entry containing invalid characters.
                }
            }
        }

        if (!OperatingSystem.IsWindows())
        {
            candidates.Add("/opt/homebrew/bin/ffprobe");
            candidates.Add("/usr/local/bin/ffprobe");
            candidates.Add("/usr/bin/ffprobe");
        }

        return candidates.FirstOrDefault(File.Exists);
    }

    /// <summary>
    /// Runs ffmpeg purely to read what it reports about a file, tolerating the non zero
    /// exit it returns when no output file was requested. Everything useful is on stderr.
    /// </summary>
    public async Task<string> DescribeWithFfmpegAsync(string mediaPath, CancellationToken cancellationToken)
    {
        var (_, _, standardError) = await RunToleratingFailureAsync(
            _ffmpegPath,
            ["-hide_banner", "-nostdin", "-i", mediaPath],
            cancellationToken);

        return standardError;
    }

    public Task<string> RunFfmpegAsync(IEnumerable<string> arguments, CancellationToken cancellationToken)
        => RunAsync(_ffmpegPath, arguments, cancellationToken);

    public Task<string> RunProbeAsync(IEnumerable<string> arguments, CancellationToken cancellationToken)
        => ProbePath is { } probe
            ? RunAsync(probe, arguments, cancellationToken)
            : throw new FfmpegException("No ffprobe is available.");

    private static async Task<string> RunAsync(string executable, IEnumerable<string> arguments, CancellationToken cancellationToken)
    {
        var (exitCode, standardOutput, standardError) = await ExecuteAsync(executable, arguments, cancellationToken);
        if (exitCode != 0)
        {
            throw new FfmpegException(
                $"{Path.GetFileName(executable)} failed with exit code {exitCode}. {Tail(standardError)}");
        }

        return standardOutput;
    }

    private static async Task<(int ExitCode, string StandardOutput, string StandardError)> RunToleratingFailureAsync(
        string executable,
        IEnumerable<string> arguments,
        CancellationToken cancellationToken)
        => await ExecuteAsync(executable, arguments, cancellationToken);

    private static async Task<(int ExitCode, string StandardOutput, string StandardError)> ExecuteAsync(
        string executable,
        IEnumerable<string> arguments,
        CancellationToken cancellationToken)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = executable,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };

        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        using var process = new Process { StartInfo = startInfo };
        var standardOutput = new StringBuilder();
        var standardError = new StringBuilder();

        process.OutputDataReceived += (_, e) =>
        {
            if (e.Data != null)
            {
                standardOutput.AppendLine(e.Data);
            }
        };

        process.ErrorDataReceived += (_, e) =>
        {
            if (e.Data != null)
            {
                standardError.AppendLine(e.Data);
            }
        };

        if (!process.Start())
        {
            throw new FfmpegException($"Could not start '{executable}'.");
        }

        process.BeginOutputReadLine();
        process.BeginErrorReadLine();

        try
        {
            await process.WaitForExitAsync(cancellationToken);
        }
        catch (OperationCanceledException)
        {
            TryKill(process);
            throw;
        }

        return (process.ExitCode, standardOutput.ToString(), standardError.ToString());
    }

    private static void TryKill(Process process)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception)
        {
            // Already exited.
        }
    }

    /// <summary>ffmpeg's stderr is long and its useful part is at the end.</summary>
    private static string Tail(string text)
    {
        var lines = text.Split('\n', StringSplitOptions.RemoveEmptyEntries);
        return string.Join(" ", lines[Math.Max(0, lines.Length - 4)..]).Trim();
    }
}
