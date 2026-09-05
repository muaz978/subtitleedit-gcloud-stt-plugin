using SubtitleEdit.GoogleCloudStt.Google;

namespace SubtitleEdit.GoogleCloudStt.Transcription;

/// <summary>
/// The three checks that stand between Google's output and a subtitle a user would trust.
/// Every one of them exists because the defect it catches was observed in real runs, not
/// because it seemed prudent.
/// </summary>
public static class TranscriptGuards
{
    /// <summary>
    /// A word may not begin before its chunk, end after it, or end before it starts.
    ///
    /// Observed: 79 of 9,432 words (0.8%) carried impossible offsets, one of them claiming
    /// 6,324 s inside a 1,080 s chunk. A single such word corrupts the whole timeline, and
    /// unfiltered they produced a negative total speech time.
    /// </summary>
    public static IReadOnlyList<WordTiming> DropImpossibleOffsets(
        IReadOnlyList<WordTiming> words,
        double chunkDurationSeconds,
        out int droppedCount)
    {
        // Recognition can legitimately run a little past the nominal chunk end.
        const double toleranceSeconds = 5.0;
        var upperBound = chunkDurationSeconds + toleranceSeconds;

        var kept = new List<WordTiming>(words.Count);
        foreach (var word in words)
        {
            var plausible =
                word.StartSeconds >= 0.0 &&
                word.EndSeconds >= word.StartSeconds &&
                word.StartSeconds <= upperBound &&
                word.EndSeconds <= upperBound;

            if (plausible)
            {
                kept.Add(word);
            }
        }

        droppedCount = words.Count - kept.Count;
        return kept;
    }

    /// <summary>
    /// Detects the silent truncation defect: a job reports success, but recognition stopped
    /// part way through the chunk and the remainder was discarded with no error raised.
    ///
    /// Observed: one 1,080 s chunk returned words only up to 398.36 s, and 11.4 minutes of
    /// dialogue vanished without any failure being reported. Without this check a user
    /// silently loses minutes of subtitles.
    /// </summary>
    /// <param name="coveredSeconds">End of the last word kept for the chunk.</param>
    /// <param name="chunkDurationSeconds">How long the chunk actually is.</param>
    public static bool LooksTruncated(double coveredSeconds, double chunkDurationSeconds)
    {
        // Trailing silence is normal, so only a large shortfall counts. A chunk ending in a
        // long musical outro can legitimately fall short by a minute or two.
        const double toleranceSeconds = 120.0;
        const double minimumCoverageRatio = 0.80;

        if (chunkDurationSeconds <= 0.0)
        {
            return false;
        }

        if (coveredSeconds >= chunkDurationSeconds - toleranceSeconds)
        {
            return false;
        }

        return coveredSeconds / chunkDurationSeconds < minimumCoverageRatio;
    }

    /// <summary>
    /// The model guard.
    ///
    /// chirp_3 is the only configuration verified to return word level timings, and Google
    /// documents it as not supporting them. If that ever changes, the failure mode would
    /// otherwise be silent: transcripts still arrive, timings do not, and the subtitle
    /// degrades to character proportional guesses, which is precisely what this plugin
    /// exists to avoid. So it fails loudly instead.
    /// </summary>
    public static void EnsureWordTimingsWereReturned(IReadOnlyList<ChunkTranscript> chunks, string model)
    {
        if (chunks.Count == 0)
        {
            return;
        }

        var anyWords = chunks.Any(c => c.Words.Count > 0);
        var anyTextWithoutTimings = chunks.Any(c => c.HasTextWithoutTimings);

        if (!anyWords && anyTextWithoutTimings)
        {
            throw new TranscriptionException(
                $"Google Cloud transcribed the audio but returned no word level timings for model '{model}'. " +
                "This plugin builds subtitle timing from those word offsets, and without them the result would be " +
                "guesswork rather than measured speech, so the run was stopped rather than producing a subtitle " +
                "that looks right and is not.");
        }
    }
}
