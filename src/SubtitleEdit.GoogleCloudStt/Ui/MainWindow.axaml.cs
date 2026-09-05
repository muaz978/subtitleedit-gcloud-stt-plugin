using Avalonia.Controls;
using Avalonia.Media;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using SubtitleEdit.GoogleCloudStt.Contract;
using SubtitleEdit.GoogleCloudStt.Google;
using SubtitleEdit.GoogleCloudStt.Transcription;
using System.Globalization;

namespace SubtitleEdit.GoogleCloudStt.Ui;

public sealed partial class MainWindow : Window
{
    private static readonly (string Code, string Name)[] Languages =
    [
        ("tr-TR", "Turkish"),
        ("en-US", "English (United States)"),
        ("en-GB", "English (United Kingdom)"),
        ("ar-SA", "Arabic"),
        ("de-DE", "German"),
        ("es-ES", "Spanish"),
        ("fr-FR", "French"),
        ("it-IT", "Italian"),
        ("nl-NL", "Dutch"),
        ("pt-BR", "Portuguese (Brazil)"),
        ("ru-RU", "Russian"),
        ("hi-IN", "Hindi"),
        ("ja-JP", "Japanese"),
        ("ko-KR", "Korean"),
        ("zh", "Chinese"),
    ];

    private readonly MainWindowContext _context;
    private readonly ThemePalette _palette;
    private CancellationTokenSource? _running;

    /// <summary>
    /// Exists only so Avalonia's runtime XAML loader can construct the type. The plugin
    /// always uses the constructor that takes a context.
    /// </summary>
    public MainWindow()
        : this(new MainWindowContext(new PluginRequest(), new Settings.PluginSettings()))
    {
    }

    public MainWindow(MainWindowContext context)
    {
        _context = context;
        _palette = ThemePalette.From(context.Request.ThemeColors);

        InitializeComponent();
        ApplyTheme();
        LoadSettingsIntoControls();
        WireEvents();
        UpdateCostEstimate();
        UpdateReadiness();

        if (!string.IsNullOrWhiteSpace(context.InitialMessage))
        {
            ShowMessage(context.InitialMessage!);
        }
    }

    private void ApplyTheme()
    {
        RequestedThemeVariant = _palette.IsDark
            ? Avalonia.Styling.ThemeVariant.Dark
            : Avalonia.Styling.ThemeVariant.Light;

        Background = _palette.Background;

        HeaderBorder.Background = _palette.Header;
        FooterBorder.Background = _palette.Header;
        VideoCard.Background = _palette.Panel;
        CostCard.Background = _palette.Panel;
        MessageCard.Background = _palette.Panel;

        TitleText.Foreground = _palette.Foreground;
        SubtitleText.Foreground = _palette.Muted;
        VideoLabel.Foreground = _palette.Muted;
        VideoName.Foreground = _palette.Foreground;
        VideoMeta.Foreground = _palette.Muted;
        CredentialsHint.Foreground = _palette.Muted;
        CostText.Foreground = _palette.Foreground;
        CostHint.Foreground = _palette.Muted;
        ProgressText.Foreground = _palette.Muted;
        StatusText.Foreground = _palette.Muted;
        MessageText.Foreground = _palette.Foreground;

        TranscribeButton.Background = _palette.Accent;
        TranscribeButton.Foreground = Brushes.White;
        ProgressBar.Foreground = _palette.Accent;
    }

    private void LoadSettingsIntoControls()
    {
        var request = _context.Request;
        var settings = _context.Settings;

        VideoName.Text = string.IsNullOrWhiteSpace(request.VideoFileName)
            ? "No video is open"
            : Path.GetFileName(request.VideoFileName);

        VideoMeta.Text = DescribeVideo(request);

        CredentialsBox.Text = settings.CredentialsPath;
        ProjectBox.Text = settings.ProjectId;
        DynamicBatchingBox.IsChecked = settings.UseDynamicBatching;

        LanguageBox.ItemsSource = Languages.Select(l => $"{l.Name}  ({l.Code})").ToList();
        var index = Array.FindIndex(Languages, l => l.Code.Equals(settings.LanguageCode, StringComparison.OrdinalIgnoreCase));
        LanguageBox.SelectedIndex = index >= 0 ? index : 0;
    }

    private static string DescribeVideo(PluginRequest request)
    {
        if (request.VideoDurationSeconds is not { } seconds || seconds <= 0)
        {
            return "Duration will be read when transcription starts";
        }

        var span = TimeSpan.FromSeconds(seconds);
        return span.TotalHours >= 1
            ? string.Create(CultureInfo.InvariantCulture, $"{(int)span.TotalHours} h {span.Minutes} min")
            : string.Create(CultureInfo.InvariantCulture, $"{span.Minutes} min {span.Seconds} s");
    }

    private void WireEvents()
    {
        BrowseButton.Click += async (_, _) => await BrowseForCredentialsAsync();
        CredentialsBox.TextChanged += (_, _) => UpdateReadiness();
        DynamicBatchingBox.IsCheckedChanged += (_, _) => UpdateCostEstimate();
        TranscribeButton.Click += async (_, _) => await TranscribeAsync();
        CloseButton.Click += (_, _) => Close();
        Closing += (_, _) => CommitSettingsIfNothingElseHappened();
    }

    private async Task BrowseForCredentialsAsync()
    {
        var files = await StorageProvider.OpenFilePickerAsync(new FilePickerOpenOptions
        {
            Title = "Choose the Google Cloud service account key",
            AllowMultiple = false,
            FileTypeFilter = [new FilePickerFileType("Service account key") { Patterns = ["*.json"] }],
        });

        var picked = files.FirstOrDefault()?.TryGetLocalPath();
        if (string.IsNullOrWhiteSpace(picked))
        {
            return;
        }

        CredentialsBox.Text = picked;

        // Saves the user from typing a project id they would have to look up.
        var projectId = SpeechCredentials.ReadProjectId(picked);
        if (!string.IsNullOrWhiteSpace(projectId) && string.IsNullOrWhiteSpace(ProjectBox.Text))
        {
            ProjectBox.Text = projectId;
        }

        UpdateReadiness();
    }

    private void UpdateReadiness()
    {
        var hasKey = !string.IsNullOrWhiteSpace(CredentialsBox.Text) && File.Exists(CredentialsBox.Text);
        var hasVideo = File.Exists(_context.Request.VideoFileName);

        TranscribeButton.IsEnabled = hasKey && hasVideo && _running == null;

        StatusText.Text = !hasVideo
            ? "Open a video in Subtitle Edit first"
            : hasKey ? string.Empty : "Choose a service account key to continue";
    }

    private void UpdateCostEstimate()
    {
        var minutes = (_context.Request.VideoDurationSeconds ?? 0) / 60.0;
        var dynamic = DynamicBatchingBox.IsChecked == true;

        if (minutes <= 0)
        {
            CostText.Text = dynamic ? "About $0.003 per minute" : "About $0.016 per minute";
            CostHint.Text = "Google bills Speech-to-Text by the minute of audio.";
            return;
        }

        var cost = minutes * (dynamic ? 0.003 : 0.016);
        CostText.Text = string.Create(CultureInfo.InvariantCulture, $"Estimated cost: ${cost:F2}");
        CostHint.Text = dynamic
            ? string.Create(CultureInfo.InvariantCulture,
                $"{minutes:F0} minutes at $0.003 per minute. Without dynamic batching this would be about ${minutes * 0.016:F2}.")
            : string.Create(CultureInfo.InvariantCulture,
                $"{minutes:F0} minutes at $0.016 per minute. Dynamic batching would bring this to about ${minutes * 0.003:F2}.");
    }

    private void ReadControlsIntoSettings()
    {
        var settings = _context.Settings;
        settings.CredentialsPath = CredentialsBox.Text?.Trim() ?? string.Empty;
        settings.ProjectId = ProjectBox.Text?.Trim() ?? string.Empty;
        settings.UseDynamicBatching = DynamicBatchingBox.IsChecked == true;

        var index = LanguageBox.SelectedIndex;
        if (index >= 0 && index < Languages.Length)
        {
            settings.LanguageCode = Languages[index].Code;
        }
    }

    /// <summary>
    /// Subtitle Edit persists a plugin's settings only when the response status is "ok".
    /// Closing the window without transcribing would otherwise throw away the key the user
    /// just picked, so a settings only "ok" is returned instead of "cancelled".
    /// </summary>
    private void CommitSettingsIfNothingElseHappened()
    {
        if (_context.Response.Status != PluginStatus.Cancelled)
        {
            return;
        }

        ReadControlsIntoSettings();

        if (!string.IsNullOrWhiteSpace(_context.Settings.CredentialsPath))
        {
            _context.Response = PluginHost.SettingsOnly(_context.Settings, "Google Cloud settings saved.");
        }
    }

    private async Task TranscribeAsync()
    {
        ReadControlsIntoSettings();

        _running = new CancellationTokenSource();
        SetBusy(true);

        var progress = new Progress<TranscriptionProgress>(p => Dispatcher.UIThread.Post(() =>
        {
            ProgressBar.Value = p.Fraction;
            ProgressText.Text = p.Detail is { Length: > 0 } detail ? $"{p.Stage}: {detail}" : p.Stage;
        }));

        try
        {
            var response = await PluginHost.RunAsync(_context.Request, _context.Settings, progress, _running.Token);
            _context.Response = response;

            if (response.Status == PluginStatus.Error)
            {
                ShowMessage(response.Message ?? "Transcription failed.");

                // Keep the window open so the user can correct the problem, but do not lose
                // what they typed if they close it now.
                _context.Response = new PluginResponse { Status = PluginStatus.Cancelled };
                return;
            }

            Close();
        }
        catch (OperationCanceledException)
        {
            ShowMessage("Transcription was cancelled.");
        }
        finally
        {
            _running?.Dispose();
            _running = null;
            SetBusy(false);
        }
    }

    private void SetBusy(bool busy)
    {
        ProgressPanel.IsVisible = busy;
        BrowseButton.IsEnabled = !busy;
        CredentialsBox.IsEnabled = !busy;
        ProjectBox.IsEnabled = !busy;
        LanguageBox.IsEnabled = !busy;
        DynamicBatchingBox.IsEnabled = !busy;
        TranscribeButton.Content = busy ? "Transcribing" : "Transcribe";

        if (busy)
        {
            MessageCard.IsVisible = false;
            TranscribeButton.IsEnabled = false;
        }
        else
        {
            UpdateReadiness();
        }
    }

    private void ShowMessage(string message)
    {
        MessageText.Text = message;
        MessageCard.IsVisible = true;
    }
}
