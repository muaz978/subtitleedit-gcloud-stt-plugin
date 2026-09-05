using Google.Apis.Auth.OAuth2;
using Speech = Google.Cloud.Speech.V2;

namespace SubtitleEdit.GoogleCloudStt.Google;

/// <summary>
/// Runs BatchRecognize against Speech-to-Text v2.
///
/// One request per chunk, with inline results. Inline output is refused for multi file
/// requests, and batching several chunks into one request would force Cloud Storage
/// output plus a download and parse step for no benefit, since the requests run
/// concurrently either way. Keeping one file per request also sidesteps the five files
/// per request cap entirely.
/// </summary>
public sealed class SpeechBatchClient
{
    /// <summary>
    /// Chirp is not served on the global endpoint, so both the endpoint and the recognizer
    /// path have to name the same region.
    /// </summary>
    private readonly string _region;
    private readonly string _projectId;
    private readonly Speech.SpeechClient _client;

    private SpeechBatchClient(Speech.SpeechClient client, string projectId, string region)
    {
        _client = client;
        _projectId = projectId;
        _region = region;
    }

    public static async Task<SpeechBatchClient> CreateAsync(
        GoogleCredential credential,
        string projectId,
        string region,
        CancellationToken cancellationToken)
    {
        var builder = new Speech.SpeechClientBuilder
        {
            Endpoint = $"{region}-speech.googleapis.com",
            GoogleCredential = credential,
        };

        var client = await builder.BuildAsync(cancellationToken);
        return new SpeechBatchClient(client, projectId, region);
    }

    /// <summary>The implicit recognizer, meaning the config travels with each request.</summary>
    private string RecognizerName => $"projects/{_projectId}/locations/{_region}/recognizers/_";

    public async Task<ChunkTranscript> RecognizeAsync(
        int chunkIndex,
        string gcsUri,
        string languageCode,
        string model,
        bool useDynamicBatching,
        CancellationToken cancellationToken)
    {
        var request = new Speech.BatchRecognizeRequest
        {
            Recognizer = RecognizerName,
            Config = new Speech.RecognitionConfig
            {
                AutoDecodingConfig = new Speech.AutoDetectDecodingConfig(),
                LanguageCodes = { languageCode },
                Model = model,
                Features = new Speech.RecognitionFeatures
                {
                    // The entire reason this plugin exists. Without it the response carries
                    // text only, and cue times have to be faked from character counts.
                    EnableWordTimeOffsets = true,
                    EnableAutomaticPunctuation = true,
                },
            },
            Files = { new Speech.BatchRecognizeFileMetadata { Uri = gcsUri } },
            RecognitionOutputConfig = new Speech.RecognitionOutputConfig
            {
                InlineResponseConfig = new Speech.InlineOutputConfig(),
            },
            ProcessingStrategy = useDynamicBatching
                ? Speech.BatchRecognizeRequest.Types.ProcessingStrategy.DynamicBatching
                : Speech.BatchRecognizeRequest.Types.ProcessingStrategy.Unspecified,
        };

        Speech.BatchRecognizeResponse response;
        try
        {
            var operation = await _client.BatchRecognizeAsync(request, cancellationToken);
            var completed = await operation.PollUntilCompletedAsync();
            response = completed.Result;
        }
        catch (Grpc.Core.RpcException exception)
        {
            throw new TranscriptionException(Explain(exception, model), exception);
        }

        return new ChunkTranscript(chunkIndex, ExtractWords(response, gcsUri));
    }

    private static IReadOnlyList<WordTiming> ExtractWords(Speech.BatchRecognizeResponse response, string gcsUri)
    {
        if (!response.Results.TryGetValue(gcsUri, out var fileResult))
        {
            fileResult = response.Results.Values.FirstOrDefault();
        }

        if (fileResult == null)
        {
            return [];
        }

        if (fileResult.Error != null && fileResult.Error.Code != 0)
        {
            throw new TranscriptionException(
                $"Google Cloud could not transcribe part of the audio: {fileResult.Error.Message}");
        }

        var words = new List<WordTiming>();
        var inline = fileResult.InlineResult;
        if (inline == null)
        {
            return words;
        }

        foreach (var result in inline.Transcript?.Results ?? [])
        {
            var alternative = result.Alternatives.FirstOrDefault();
            if (alternative == null)
            {
                continue;
            }

            // Word timings are populated on the top alternative only.
            foreach (var word in alternative.Words)
            {
                var text = word.Word;
                if (string.IsNullOrWhiteSpace(text))
                {
                    continue;
                }

                words.Add(new WordTiming(
                    text,
                    word.StartOffset?.ToTimeSpan().TotalSeconds ?? 0.0,
                    word.EndOffset?.ToTimeSpan().TotalSeconds ?? 0.0));
            }
        }

        return words;
    }

    /// <summary>
    /// Turns the gRPC failures that actually happen into something a user can act on.
    /// The model and region combination is the usual culprit, because Chirp is served
    /// only from particular regional endpoints.
    /// </summary>
    private string Explain(Grpc.Core.RpcException exception, string model) => exception.StatusCode switch
    {
        Grpc.Core.StatusCode.InvalidArgument when exception.Status.Detail.Contains("does not exist", StringComparison.OrdinalIgnoreCase)
            => $"Google Cloud reports that the model '{model}' does not exist in region '{_region}'. " +
               "Chirp models are served only from specific regional endpoints, not from the global one.",
        Grpc.Core.StatusCode.PermissionDenied
            => "This service account is not allowed to use Speech-to-Text. Grant it the Cloud Speech Client role, " +
               $"and check that the Speech-to-Text API is enabled in project '{_projectId}'. ({exception.Status.Detail})",
        Grpc.Core.StatusCode.Unauthenticated
            => "Google Cloud rejected the credentials. Speech-to-Text v2 does not accept API keys, only a service account key. " +
               $"({exception.Status.Detail})",
        Grpc.Core.StatusCode.ResourceExhausted
            => $"Google Cloud quota was exhausted while transcribing. ({exception.Status.Detail})",
        _ => $"Google Cloud returned {exception.StatusCode}: {exception.Status.Detail}",
    };
}
