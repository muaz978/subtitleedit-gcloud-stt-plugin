namespace SubtitleEdit.GoogleCloudStt.Google;

/// <summary>A single recognized word with the timings Speech-to-Text returned for it.</summary>
public sealed record WordTiming(string Text, double StartSeconds, double EndSeconds)
{
    public double DurationSeconds => EndSeconds - StartSeconds;

    /// <summary>Shifts a chunk relative timing onto the timeline of the whole media.</summary>
    public WordTiming Shift(double offsetSeconds)
        => this with { StartSeconds = StartSeconds + offsetSeconds, EndSeconds = EndSeconds + offsetSeconds };
}

/// <summary>What one chunk's recognition produced, before any timeline assembly.</summary>
/// <param name="TranscriptText">
/// The plain transcript, kept so that "no words came back" can be told apart from
/// "no word timings came back". A silent chunk is normal; a chunk with text but no
/// timings means the model is not returning what this plugin depends on.
/// </param>
public sealed record ChunkTranscript(int ChunkIndex, IReadOnlyList<WordTiming> Words, string TranscriptText = "")
{
    public bool HasTextWithoutTimings => Words.Count == 0 && !string.IsNullOrWhiteSpace(TranscriptText);

    /// <summary>
    /// End of the last word, in chunk relative seconds. Used to detect the silent
    /// truncation defect, where a job reports success but stops transcribing part way
    /// through and discards the rest without raising anything.
    /// </summary>
    public double CoveredSeconds => Words.Count == 0 ? 0.0 : Words[^1].EndSeconds;
}
