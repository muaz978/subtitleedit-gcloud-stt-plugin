using Google;
using Google.Apis.Auth.OAuth2;
using Google.Cloud.Storage.V1;
using System.Net;

namespace SubtitleEdit.GoogleCloudStt.Google;

/// <summary>
/// The Cloud Storage staging area BatchRecognize reads from.
///
/// This is not optional. BatchRecognize accepts Cloud Storage URIs only, and any audio
/// worth subtitling is far past the roughly one minute inline ceiling of the synchronous
/// Recognize call, so long audio has no inline path at all.
///
/// The plugin creates and owns one bucket and removes every object it uploads once a run
/// finishes, so the storage cost of a run rounds to nothing.
/// </summary>
public sealed class GcsWorkspace : IAsyncDisposable
{
    private readonly StorageClient _storage;
    private readonly string _bucketName;
    private readonly string _prefix;
    private readonly List<string> _uploadedObjects = [];

    private GcsWorkspace(StorageClient storage, string bucketName, string prefix)
    {
        _storage = storage;
        _bucketName = bucketName;
        _prefix = prefix;
    }

    public string BucketName => _bucketName;

    /// <summary>
    /// Bucket names are globally unique across all of Google Cloud, so this derives one
    /// from the project id, which is itself globally unique.
    /// </summary>
    public static string DeriveBucketName(string projectId)
    {
        var cleaned = new string(projectId
            .ToLowerInvariant()
            .Select(c => char.IsLetterOrDigit(c) || c == '-' ? c : '-')
            .ToArray())
            .Trim('-');

        const string suffix = "-subtitle-edit-stt";
        var maximumPrefix = 63 - suffix.Length;
        if (cleaned.Length > maximumPrefix)
        {
            cleaned = cleaned[..maximumPrefix];
        }

        return cleaned + suffix;
    }

    public static async Task<GcsWorkspace> OpenAsync(
        GoogleCredential credential,
        string projectId,
        string bucketName,
        string location,
        string runPrefix,
        CancellationToken cancellationToken)
    {
        var storage = await StorageClient.CreateAsync(credential);
        await EnsureBucketAsync(storage, projectId, bucketName, location, cancellationToken);
        return new GcsWorkspace(storage, bucketName, runPrefix.TrimEnd('/') + "/");
    }

    /// <summary>Returns true when the bucket had to be created.</summary>
    private static async Task<bool> EnsureBucketAsync(
        StorageClient storage,
        string projectId,
        string bucketName,
        string location,
        CancellationToken cancellationToken)
    {
        try
        {
            await storage.GetBucketAsync(bucketName, cancellationToken: cancellationToken);
            return false;
        }
        catch (GoogleApiException exception) when (exception.HttpStatusCode == HttpStatusCode.NotFound)
        {
            // Expected on first run.
        }
        catch (GoogleApiException exception) when (exception.HttpStatusCode == HttpStatusCode.Forbidden)
        {
            throw new TranscriptionException(
                $"The bucket '{bucketName}' exists but this service account cannot access it. " +
                "Either grant the service account the Storage Admin role, or choose a different bucket name in the plugin settings.",
                exception);
        }

        try
        {
            await storage.CreateBucketAsync(
                projectId,
                new global::Google.Apis.Storage.v1.Data.Bucket
                {
                    Name = bucketName,
                    Location = location,
                    // Audio staged here is deleted at the end of a run, but a run that is
                    // killed part way through would otherwise leave objects behind forever.
                    Lifecycle = new global::Google.Apis.Storage.v1.Data.Bucket.LifecycleData
                    {
                        Rule =
                        [
                            new global::Google.Apis.Storage.v1.Data.Bucket.LifecycleData.RuleData
                            {
                                Action = new global::Google.Apis.Storage.v1.Data.Bucket.LifecycleData.RuleData.ActionData { Type = "Delete" },
                                Condition = new global::Google.Apis.Storage.v1.Data.Bucket.LifecycleData.RuleData.ConditionData { Age = 1 },
                            },
                        ],
                    },
                },
                cancellationToken: cancellationToken);

            return true;
        }
        catch (GoogleApiException exception) when (exception.HttpStatusCode == HttpStatusCode.Forbidden)
        {
            throw new TranscriptionException(
                "This service account is not allowed to create a Cloud Storage bucket. Grant it the Storage Admin role, " +
                $"or create a bucket yourself and name it in the plugin settings. Tried to create '{bucketName}'.",
                exception);
        }
        catch (GoogleApiException exception) when (exception.HttpStatusCode == HttpStatusCode.Conflict)
        {
            throw new TranscriptionException(
                $"The bucket name '{bucketName}' is already taken by another Google Cloud project. " +
                "Set a different bucket name in the plugin settings.",
                exception);
        }
    }

    /// <summary>Uploads one file and returns its gs:// URI.</summary>
    public async Task<string> UploadAsync(string localPath, CancellationToken cancellationToken)
    {
        var objectName = _prefix + Path.GetFileName(localPath);

        await using var stream = File.OpenRead(localPath);
        await _storage.UploadObjectAsync(
            _bucketName,
            objectName,
            contentType: "audio/flac",
            source: stream,
            cancellationToken: cancellationToken);

        _uploadedObjects.Add(objectName);
        return $"gs://{_bucketName}/{objectName}";
    }

    /// <summary>
    /// Removes everything this run uploaded. Failures are swallowed: a leftover object
    /// costs a fraction of a cent and the bucket lifecycle rule sweeps it within a day,
    /// which is not worth failing a completed transcription over.
    /// </summary>
    public async ValueTask DisposeAsync()
    {
        foreach (var objectName in _uploadedObjects)
        {
            try
            {
                await _storage.DeleteObjectAsync(_bucketName, objectName);
            }
            catch (Exception)
            {
                // Swept by the bucket's one day lifecycle rule instead.
            }
        }

        _storage.Dispose();
    }
}
