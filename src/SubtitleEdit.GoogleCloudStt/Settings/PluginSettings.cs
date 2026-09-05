using System.Text.Json;
using System.Text.Json.Serialization;

namespace SubtitleEdit.GoogleCloudStt.Settings;

/// <summary>
/// Persisted between runs by Subtitle Edit, which hands the previous value back in
/// request.settings. Subtitle Edit stores this verbatim in its own Settings.json, so
/// the service account key itself is deliberately NOT stored here: only the path to
/// the key file the user chose.
/// </summary>
public sealed class PluginSettings
{
    /// <summary>Bump when the shape below changes, so a stale value can be discarded.</summary>
    public const int CurrentVersion = 1;

    /// <summary>Absolute path to the user's service account JSON key file.</summary>
    public string CredentialsPath { get; set; } = string.Empty;

    /// <summary>Google Cloud project id. Read from the key file when left empty.</summary>
    public string ProjectId { get; set; } = string.Empty;

    /// <summary>
    /// Cloud Storage bucket used as a staging area. BatchRecognize reads only from Cloud
    /// Storage, so long audio has no inline path. The plugin creates and manages this
    /// bucket itself and deletes the objects it uploads once a run completes.
    /// </summary>
    public string BucketName { get; set; } = string.Empty;

    /// <summary>Set when the plugin created the bucket, so it knows it may remove it.</summary>
    public bool BucketCreatedByPlugin { get; set; }

    public string LanguageCode { get; set; } = "tr-TR";

    /// <summary>
    /// chirp_3 is the only configuration verified end to end to return word level
    /// timings. See ModelGuard for why this is checked at runtime rather than trusted.
    /// </summary>
    public string Model { get; set; } = "chirp_3";

    /// <summary>
    /// Chirp is not served on the global endpoint, so a region and a matching regional
    /// endpoint are both required.
    /// </summary>
    public string Region { get; set; } = "us";

    /// <summary>Roughly an 81 percent discount, at the cost of a slower turnaround.</summary>
    public bool UseDynamicBatching { get; set; } = true;

    public static PluginSettings FromRequest(JsonElement? settings, int? settingsVersion)
    {
        if (settings is null || settings.Value.ValueKind != JsonValueKind.Object)
        {
            return new PluginSettings();
        }

        // Discard anything written by a build with a different schema rather than
        // deserializing it into a shape it was never written for.
        if (settingsVersion is not CurrentVersion)
        {
            return new PluginSettings();
        }

        try
        {
            return settings.Value.Deserialize(PluginSettingsJsonContext.Default.PluginSettings) ?? new PluginSettings();
        }
        catch (JsonException)
        {
            return new PluginSettings();
        }
    }

    public JsonElement ToJsonElement()
        => JsonSerializer.SerializeToElement(this, PluginSettingsJsonContext.Default.PluginSettings);
}

/// <summary>Source generated so the settings shape survives trimming. See PluginJsonContext.</summary>
[JsonSourceGenerationOptions(
    PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase,
    PropertyNameCaseInsensitive = true,
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull)]
[JsonSerializable(typeof(PluginSettings))]
internal partial class PluginSettingsJsonContext : JsonSerializerContext
{
}
