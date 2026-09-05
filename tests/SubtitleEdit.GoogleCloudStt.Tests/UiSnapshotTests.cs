using Avalonia;
using Avalonia.Headless;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using SubtitleEdit.GoogleCloudStt.Contract;
using SubtitleEdit.GoogleCloudStt.Settings;
using SubtitleEdit.GoogleCloudStt.Ui;

namespace SubtitleEdit.GoogleCloudStt.Tests;

/// <summary>
/// Renders the plugin window off screen and writes a PNG, so the layout can be reviewed
/// without a display and without screen recording permissions. Also proves the window
/// builds and themes cleanly, which a pure logic test cannot.
/// </summary>
public sealed class UiSnapshotTests
{
    private static readonly object StartLock = new();
    private static bool _started;

    private static void EnsureAvalonia()
    {
        lock (StartLock)
        {
            if (_started)
            {
                return;
            }

            AppBuilder.Configure<App>()
                .UseSkia()
                .UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false })
                .SetupWithoutStarting();

            _started = true;
        }
    }

    private static PluginRequest SampleRequest(bool dark) => new()
    {
        VideoFileName = "/Users/example/Movies/Gönül Dağı 220. Bölüm.mp4",
        VideoDurationSeconds = 8716.98,
        Theme = dark ? "Dark" : "Light",
        SeVersion = "5.0.0",
        PluginDataDirectory = Path.GetTempPath(),
        ThemeColors = dark
            ? new PluginThemeColors
            {
                IsDark = true,
                BackgroundColor = "#FF212121",
                ForegroundColor = "#FFDCDCDC",
                AccentColor = "#631E90FF",
                BackgroundColorLighter = "#FF262626",
                BackgroundColorHeader = "#FF303030",
            }
            : new PluginThemeColors
            {
                IsDark = false,
                BackgroundColor = "#FFFFFFFF",
                ForegroundColor = "#FF1A1A1A",
                AccentColor = "#FF1E90FF",
                BackgroundColorLighter = "#FFF5F5F5",
                BackgroundColorHeader = "#FFEAEAEA",
            },
    };

    [Theory]
    [InlineData(true, "dark")]
    [InlineData(false, "light")]
    public void Window_RendersInBothThemes(bool dark, string name)
    {
        EnsureAvalonia();

        var context = new MainWindowContext(SampleRequest(dark), new PluginSettings());
        var window = new MainWindow(context);
        window.Show();

        // Let layout and the render pass settle.
        Dispatcher.UIThread.RunJobs();

        var frame = window.CaptureRenderedFrame();
        Assert.NotNull(frame);

        var outputDirectory = Path.Combine(Path.GetTempPath(), "se-gcstt-ui");
        Directory.CreateDirectory(outputDirectory);
        var path = Path.Combine(outputDirectory, $"window-{name}.png");
        frame!.Save(path);

        Assert.True(new FileInfo(path).Length > 5000, "the rendered window should not be blank");
    }
}
