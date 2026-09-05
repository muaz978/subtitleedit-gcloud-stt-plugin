using Google.Apis.Auth.OAuth2;
using System.Text.Json;

namespace SubtitleEdit.GoogleCloudStt.Google;

/// <summary>Raised for problems the user can actually act on.</summary>
public sealed class TranscriptionException : Exception
{
    public TranscriptionException(string message) : base(message)
    {
    }

    public TranscriptionException(string message, Exception inner) : base(message, inner)
    {
    }
}

/// <summary>
/// Loads a service account key.
///
/// Speech-to-Text v2 rejects API keys outright with
/// "API keys are not supported by this API. Expected OAuth2 access token", so a service
/// account JSON key is the only practical credential for a desktop plugin.
/// </summary>
public static class SpeechCredentials
{
    public static GoogleCredential Load(string credentialsPath)
    {
        if (string.IsNullOrWhiteSpace(credentialsPath) || !File.Exists(credentialsPath))
        {
            throw new TranscriptionException(
                "The Google Cloud service account key file could not be found. Choose the JSON key you downloaded from the Google Cloud console.");
        }

        try
        {
            // Asking for ServiceAccountCredential specifically, rather than any credential,
            // means picking the wrong JSON (an OAuth client secret, say) fails here with a
            // clear message instead of much later with an authentication error.
            //
            // This is also the exact reflection path a fully trimmed build breaks at
            // runtime, which is why the publish script uses TrimMode=partial and why the
            // smoke test loads a real credential against the published binary.
            var serviceAccount = CredentialFactory.FromFile<ServiceAccountCredential>(credentialsPath);
            return serviceAccount.ToGoogleCredential();
        }
        catch (TranscriptionException)
        {
            throw;
        }
        catch (Exception exception)
        {
            throw new TranscriptionException(
                "That file is not a Google Cloud service account key. In the Google Cloud console open IAM and Admin, " +
                "Service Accounts, pick your service account, then Keys, Add key, Create new key, and choose JSON. " +
                $"({exception.Message})",
                exception);
        }
    }

    /// <summary>Reads project_id out of the key file so the user does not have to type it.</summary>
    public static string? ReadProjectId(string credentialsPath)
    {
        try
        {
            using var stream = File.OpenRead(credentialsPath);
            using var document = JsonDocument.Parse(stream);
            if (document.RootElement.TryGetProperty("project_id", out var projectId) &&
                projectId.ValueKind == JsonValueKind.String)
            {
                return projectId.GetString();
            }
        }
        catch (Exception)
        {
            // Fall through: the user can still supply the project id explicitly.
        }

        return null;
    }
}
