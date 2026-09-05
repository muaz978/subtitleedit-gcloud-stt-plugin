using System.Text.Json;
using System.Text.Json.Serialization;

namespace SubtitleEdit.GoogleCloudStt.Contract;

/// <summary>
/// Mirrors Subtitle Edit's own PluginRequest. Subtitle Edit writes this to a JSON file
/// and passes the path as the first command line argument.
///
/// Only the members this plugin reads are declared. Unknown members are ignored on
/// purpose, so the host contract can grow without breaking this build.
/// </summary>
public sealed class PluginRequest
{
    public int ApiVersion { get; set; } = 1;
    public string RequestType { get; set; } = "run";

    /// <summary>Absolute path this plugin must write its response to.</summary>
    public string ResponseFilePath { get; set; } = string.Empty;

    /// <summary>Scratch directory, deleted by Subtitle Edit after the run.</summary>
    public string TempDirectory { get; set; } = string.Empty;

    /// <summary>
    /// Persistent per plugin directory, shaped &lt;DataFolder&gt;/Plugins/Data/&lt;plugin name&gt;.
    /// Walking three levels up from here yields Subtitle Edit's data folder, which is how
    /// this plugin finds Settings.json and the ffmpeg SE already has. That is exact even
    /// in portable installs, where guessing %APPDATA% would be wrong.
    /// </summary>
    public string PluginDataDirectory { get; set; } = string.Empty;

    public PluginSubtitle Subtitle { get; set; } = new();
    public string VideoFileName { get; set; } = string.Empty;
    public double FrameRate { get; set; }
    public double? VideoDurationSeconds { get; set; }
    public string UiLanguage { get; set; } = string.Empty;
    public string Theme { get; set; } = string.Empty;
    public PluginThemeColors? ThemeColors { get; set; }
    public string SeVersion { get; set; } = string.Empty;

    /// <summary>Whatever this plugin returned as settings last run. Null on first run.</summary>
    public JsonElement? Settings { get; set; }

    /// <summary>Schema version this plugin attached to Settings last run. Null on first run.</summary>
    public int? SettingsVersion { get; set; }
}

public sealed class PluginSubtitle
{
    public string Format { get; set; } = string.Empty;
    public string FileName { get; set; } = string.Empty;
    public string Native { get; set; } = string.Empty;
    public string SubRip { get; set; } = string.Empty;
}

public sealed class PluginThemeColors
{
    public bool IsDark { get; set; }
    public string BackgroundColor { get; set; } = string.Empty;
    public string ForegroundColor { get; set; } = string.Empty;
    public string AccentColor { get; set; } = string.Empty;
    public string BackgroundColorLighter { get; set; } = string.Empty;
    public string BackgroundColorHeader { get; set; } = string.Empty;
    public string BookmarkColor { get; set; } = string.Empty;
}

/// <summary>
/// Mirrors Subtitle Edit's PluginResponse.
///
/// Three behaviours of the host drive how this is used, all verified in MainViewModel.RunPlugin:
///   1. Settings are persisted only on status "ok". The cancelled and error paths return
///      before SavePluginSettings, so credentials entered during a run that the user then
///      cancels would be lost. To save settings without touching the subtitle, return "ok"
///      with a null Subtitle and a Message.
///   2. "ok" with a null or whitespace Subtitle.Native is a legitimate "show this message,
///      change nothing" result. It is not an error.
///   3. A non empty Native that fails to parse aborts the run with "returned an unparsable
///      subtitle", so never emit a partial or empty SRT body.
/// </summary>
public sealed class PluginResponse
{
    public int ApiVersion { get; set; } = 1;
    public string Status { get; set; } = PluginStatus.Cancelled;
    public string? Message { get; set; }
    public PluginResponseSubtitle? Subtitle { get; set; }
    public JsonElement? Settings { get; set; }
    public int? SettingsVersion { get; set; }
    public string? UndoDescription { get; set; }
}

public sealed class PluginResponseSubtitle
{
    public string Format { get; set; } = "SubRip";
    public string Native { get; set; } = string.Empty;
}

public static class PluginStatus
{
    public const string Ok = "ok";
    public const string Cancelled = "cancelled";
    public const string Error = "error";
}

/// <summary>
/// Source generated serialization, mirroring the host's own PluginJsonContext.
///
/// This is not only a performance choice. The plugin ships trimmed, and reflection based
/// System.Text.Json cannot be statically analysed, so a trimmed build would drop the types
/// it needs and fail at runtime rather than at build time. Source generation makes the
/// dependency visible to the trimmer.
/// </summary>
[JsonSourceGenerationOptions(
    PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase,
    PropertyNameCaseInsensitive = true,
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    ReadCommentHandling = JsonCommentHandling.Skip,
    AllowTrailingCommas = true,
    UnmappedMemberHandling = JsonUnmappedMemberHandling.Skip,
    WriteIndented = true)]
[JsonSerializable(typeof(PluginRequest))]
[JsonSerializable(typeof(PluginResponse))]
public partial class PluginJsonContext : JsonSerializerContext
{
}
