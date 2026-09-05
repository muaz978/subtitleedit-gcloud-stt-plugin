using SubtitleEdit.GoogleCloudStt.Google;
using SubtitleEdit.GoogleCloudStt.Transcription;

namespace SubtitleEdit.GoogleCloudStt.Tests;

public sealed class TranscriptGuardsTests
{
    private static WordTiming Word(string text, double start, double end) => new(text, start, end);

    [Fact]
    public void DropImpossibleOffsets_RemovesWordBeyondChunk_TheRealObservedCase()
    {
        // Taken from a real run: a word claiming 6,324 s inside a 1,080 s chunk.
        var words = new[]
        {
            Word("merhaba", 1.0, 1.4),
            Word("bozuk", 6324.0, 6324.5),
            Word("devam", 2.0, 2.4),
        };

        var kept = TranscriptGuards.DropImpossibleOffsets(words, chunkDurationSeconds: 1080.0, out var dropped);

        Assert.Equal(1, dropped);
        Assert.Equal(2, kept.Count);
        Assert.DoesNotContain(kept, w => w.Text == "bozuk");
    }

    [Fact]
    public void DropImpossibleOffsets_RemovesWordEndingBeforeItStarts()
    {
        var words = new[] { Word("ters", 10.0, 9.0), Word("iyi", 11.0, 11.5) };

        var kept = TranscriptGuards.DropImpossibleOffsets(words, 1080.0, out var dropped);

        Assert.Equal(1, dropped);
        Assert.Single(kept);
    }

    [Fact]
    public void DropImpossibleOffsets_KeepsWordSlightlyPastChunkEnd()
    {
        // Recognition can legitimately spill a little past the nominal boundary.
        var words = new[] { Word("son", 1079.5, 1081.2) };

        var kept = TranscriptGuards.DropImpossibleOffsets(words, 1080.0, out var dropped);

        Assert.Equal(0, dropped);
        Assert.Single(kept);
    }

    [Fact]
    public void LooksTruncated_DetectsTheRealSilentTruncation()
    {
        // The observed defect: an 1,080 s chunk that stopped returning words at 398.36 s
        // while the job reported success.
        Assert.True(TranscriptGuards.LooksTruncated(coveredSeconds: 398.36, chunkDurationSeconds: 1080.0));
    }

    [Theory]
    [InlineData(1080.0, 1080.0)]  // complete
    [InlineData(1035.0, 1080.0)]  // ordinary trailing silence
    [InlineData(970.0, 1080.0)]   // a long musical outro, still plausible
    public void LooksTruncated_AcceptsPlausibleCoverage(double covered, double duration)
    {
        Assert.False(TranscriptGuards.LooksTruncated(covered, duration));
    }

    [Fact]
    public void EnsureWordTimingsWereReturned_ThrowsWhenTextArrivesWithoutTimings()
    {
        // The failure this plugin must never absorb quietly: transcripts arrive, timings
        // do not, and the subtitle silently degrades to character proportional guesses.
        var chunks = new[]
        {
            new ChunkTranscript(0, [], "bugün hava çok güzel"),
        };

        var exception = Assert.Throws<TranscriptionException>(
            () => TranscriptGuards.EnsureWordTimingsWereReturned(chunks, "chirp_3"));

        Assert.Contains("no word level timings", exception.Message, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("chirp_3", exception.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void EnsureWordTimingsWereReturned_AllowsGenuinelySilentAudio()
    {
        // No text and no timings is silence, which is a legitimate result.
        var chunks = new[] { new ChunkTranscript(0, [], string.Empty) };

        TranscriptGuards.EnsureWordTimingsWereReturned(chunks, "chirp_3");
    }

    [Fact]
    public void EnsureWordTimingsWereReturned_AllowsPartiallySilentMedia()
    {
        // One chunk with timings and one silent chunk is normal for a TV episode.
        var chunks = new[]
        {
            new ChunkTranscript(0, [Word("merhaba", 1.0, 1.5)], "merhaba"),
            new ChunkTranscript(1, [], string.Empty),
        };

        TranscriptGuards.EnsureWordTimingsWereReturned(chunks, "chirp_3");
    }
}
