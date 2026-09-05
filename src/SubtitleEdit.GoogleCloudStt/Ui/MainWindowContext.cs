using SubtitleEdit.GoogleCloudStt.Contract;
using SubtitleEdit.GoogleCloudStt.Settings;

namespace SubtitleEdit.GoogleCloudStt.Ui;

/// <summary>
/// Everything the window needs, plus the response it produces. The window is the only
/// thing that decides what this run returns to Subtitle Edit.
/// </summary>
public sealed class MainWindowContext
{
    public MainWindowContext(PluginRequest request, PluginSettings settings)
    {
        Request = request;
        Settings = settings;

        // Default to cancelled. If the user closes the window without transcribing, that
        // is exactly right, and the window replaces it with an "ok" carrying settings when
        // there is something worth remembering.
        Response = new PluginResponse { Status = PluginStatus.Cancelled };
    }

    public PluginRequest Request { get; }

    public PluginSettings Settings { get; }

    public PluginResponse Response { get; set; }

    /// <summary>Shown when the window opens, for problems detected before it was built.</summary>
    public string? InitialMessage { get; set; }
}
