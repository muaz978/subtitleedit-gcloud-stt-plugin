using Avalonia;
using SubtitleEdit.GoogleCloudStt.Contract;
using SubtitleEdit.GoogleCloudStt.Settings;
using SubtitleEdit.GoogleCloudStt.Ui;
using System.Text.Json;

namespace SubtitleEdit.GoogleCloudStt;

public static class Program
{
    /// <summary>
    /// Deliberately NOT async.
    ///
    /// An async Main resumes on a thread pool thread after its first await, and Avalonia
    /// must be started on the process's main thread. Starting it anywhere else fails with
    /// "IDispatcherImpl belongs to a different thread". The request and response files are
    /// small, so reading and writing them synchronously costs nothing; everything genuinely
    /// long running happens inside the window, on its own dispatcher.
    /// </summary>
    [STAThread]
    public static int Main(string[] args)
    {
        // Nothing below may throw past this point without leaving a response behind: a
        // missing response file surfaces to the user as a bare "did not produce a response",
        // which tells them nothing about what actually went wrong.
        string? responseFilePath = null;

        try
        {
            if (args.Length >= 1 && args[0] == "--selftest")
            {
                return SelfTest();
            }

            if (args.Length < 1 || string.IsNullOrWhiteSpace(args[0]))
            {
                Console.Error.WriteLine(
                    "This program is a Subtitle Edit 5 plugin. It expects the path of a request JSON file as its first argument.");
                return 1;
            }

            var request = ReadRequest(args[0]);
            responseFilePath = request.ResponseFilePath;

            var settings = PluginSettings.FromRequest(request.Settings, request.SettingsVersion);
            var response = RunUserInterface(request, settings);

            WriteResponse(responseFilePath, response);
            return 0;
        }
        catch (Exception exception)
        {
            if (!string.IsNullOrWhiteSpace(responseFilePath))
            {
                TryWriteError(responseFilePath, exception);

                // Exit 0 on purpose. A non zero exit makes Subtitle Edit discard the
                // response and show "exited with code N" instead of the real message.
                return 0;
            }

            Console.Error.WriteLine(exception.ToString());
            return 1;
        }
    }

    /// <summary>
    /// Verifies that a packaged build actually works, without opening a window.
    ///
    /// Exercises the two things a broken package fails at: source generated JSON, and the
    /// reflection heavy credential loader. Both are runtime failures rather than build
    /// failures, so the published artifact has to be executed to know it is sound.
    /// </summary>
    private static int SelfTest()
    {
        try
        {
            var request = JsonSerializer.Deserialize(
                """{"apiVersion":1,"requestType":"run","responseFilePath":"x","videoFileName":"y","unknownFutureField":true}""",
                PluginJsonContext.Default.PluginRequest);

            if (request is null || request.ApiVersion != 1 || request.VideoFileName != "y")
            {
                Console.Error.WriteLine("self test: request deserialization is broken");
                return 1;
            }

            var json = JsonSerializer.Serialize(
                new PluginResponse { Status = PluginStatus.Ok, Message = "ok" },
                PluginJsonContext.Default.PluginResponse);

            if (!json.Contains("\"status\"", StringComparison.Ordinal))
            {
                Console.Error.WriteLine("self test: response serialization is broken");
                return 1;
            }

            var settings = PluginSettings.FromRequest(null, null);
            _ = settings.ToJsonElement();

            // Expected to throw: the point is that the credential code path is present and
            // reaches its own validation rather than dying inside a trimmed away type.
            try
            {
                Google.SpeechCredentials.Load(Path.Combine(Path.GetTempPath(), "definitely-missing-key.json"));
                Console.Error.WriteLine("self test: credential loader accepted a missing file");
                return 1;
            }
            catch (Google.TranscriptionException)
            {
                // Correct.
            }

            Console.Out.WriteLine("self test passed");
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine("self test failed: " + exception);
            return 1;
        }
    }

    /// <summary>
    /// Shows the plugin's window and returns whatever the user's session produced. The
    /// window owns the decision: transcribe, save settings only, or cancel.
    /// </summary>
    private static PluginResponse RunUserInterface(PluginRequest request, PluginSettings settings)
    {
        var context = new MainWindowContext(request, settings)
        {
            InitialMessage = PluginHost.DescribeBlockingProblem(request),
        };

        App.Context = context;

        AppBuilder.Configure<App>()
            .UsePlatformDetect()
            .StartWithClassicDesktopLifetime([]);

        return context.Response;
    }

    private static PluginRequest ReadRequest(string path)
    {
        using var stream = File.OpenRead(path);
        var request = JsonSerializer.Deserialize(stream, PluginJsonContext.Default.PluginRequest);
        return request ?? throw new InvalidOperationException($"The request file '{path}' was empty or unreadable.");
    }

    private static void WriteResponse(string path, PluginResponse response)
    {
        var directory = Path.GetDirectoryName(path);
        if (!string.IsNullOrWhiteSpace(directory))
        {
            Directory.CreateDirectory(directory);
        }

        using var stream = File.Create(path);
        JsonSerializer.Serialize(stream, response, PluginJsonContext.Default.PluginResponse);
    }

    private static void TryWriteError(string path, Exception exception)
    {
        try
        {
            WriteResponse(path, new PluginResponse
            {
                Status = PluginStatus.Error,
                Message = exception is OperationCanceledException
                    ? "Transcription was cancelled or exceeded the plugin's time budget."
                    : exception.Message,
            });
        }
        catch (Exception)
        {
            // The response path itself is unusable. There is nothing further to try.
        }
    }
}
