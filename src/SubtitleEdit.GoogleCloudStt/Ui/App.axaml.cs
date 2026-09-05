using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Markup.Xaml;

namespace SubtitleEdit.GoogleCloudStt.Ui;

public sealed partial class App : Application
{
    /// <summary>Set before the app starts; the window is built from it.</summary>
    public static MainWindowContext? Context { get; set; }

    public override void Initialize() => AvaloniaXamlLoader.Load(this);

    public override void OnFrameworkInitializationCompleted()
    {
        if (ApplicationLifetime is IClassicDesktopStyleApplicationLifetime desktop && Context != null)
        {
            desktop.MainWindow = new MainWindow(Context);

            // The window closing is the only exit path, so the plugin never lingers after
            // the user is done with it.
            desktop.ShutdownMode = ShutdownMode.OnMainWindowClose;
        }

        base.OnFrameworkInitializationCompleted();
    }
}
