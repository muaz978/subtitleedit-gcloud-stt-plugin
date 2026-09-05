using SubtitleEdit.GoogleCloudStt.Google;
using SubtitleEdit.GoogleCloudStt.Transcription;
using System.Text.Json;

namespace SubtitleEdit.GoogleCloudStt.Tests;

public sealed class SrtBuilderTests
{
    private readonly Xunit.Abstractions.ITestOutputHelper _output;

    public SrtBuilderTests(Xunit.Abstractions.ITestOutputHelper output) => _output = output;

    private static WordTiming Word(string text, double start, double end) => new(text, start, end);

    [Fact]
    public void BuildCues_BreaksOnARealPause()
    {
        var words = new[]
        {
            Word("merhaba", 1.0, 1.4),
            Word("dünya", 1.4, 1.9),
            // A five second silence: the two must not end up in one cue.
            Word("tekrar", 6.9, 7.3),
        };

        var cues = SrtBuilder.BuildCues(words);

        Assert.Equal(2, cues.Count);
        Assert.Contains("merhaba", cues[0].Text, StringComparison.Ordinal);
        Assert.Contains("tekrar", cues[1].Text, StringComparison.Ordinal);
    }

    [Fact]
    public void BuildCues_BreaksAtSentenceEnd()
    {
        var words = new[]
        {
            Word("Geldi.", 1.0, 1.5),
            Word("Sonra", 1.6, 2.0),
        };

        Assert.Equal(2, SrtBuilder.BuildCues(words).Count);
    }

    [Fact]
    public void BuildCues_NeverProducesTouchingCues()
    {
        // Cues butting together at exactly 1 ms is the signature of arithmetic
        // subdivision, which is what a text only engine produces.
        var words = new[]
        {
            Word("bir", 1.0, 1.05),
            Word("iki.", 1.06, 1.10),
            Word("üç", 1.20, 1.25),
        };

        var cues = SrtBuilder.BuildCues(words);

        for (var i = 1; i < cues.Count; i++)
        {
            Assert.True(
                cues[i].StartSeconds > cues[i - 1].EndSeconds,
                $"cue {i} starts at {cues[i].StartSeconds} but the previous ends at {cues[i - 1].EndSeconds}");
        }
    }

    [Fact]
    public void ToSrt_FormatsTimecodesTheWaySubRipRequires()
    {
        var cues = new[] { new SubtitleCue(3661.5, 3662.25, "test") };

        var srt = SrtBuilder.ToSrt(cues);

        Assert.Contains("01:01:01,500 --> 01:01:02,250", srt, StringComparison.Ordinal);
        Assert.StartsWith("1\n", srt, StringComparison.Ordinal);
    }

    /// <summary>
    /// Ground truth: the 13,175 real word offsets Google returned for a 145 minute episode.
    /// If the builder is sound, silence stays silent and the resulting subtitle looks like
    /// TV drama rather than like continuous speech.
    /// </summary>
    [Fact]
    public void BuildCues_AgainstRealEpisodeWordTimings_PreservesSilence()
    {
        const string path = "/Users/muazsabbagh/Movies/4K Video Downloader+/TEST GoogleCloud Chirp3.srt.words.json";
        if (!File.Exists(path))
        {
            return; // Ground truth not present on this machine.
        }

        using var document = JsonDocument.Parse(File.ReadAllText(path));
        var words = document.RootElement.EnumerateArray()
            .Select(e => new WordTiming(
                e.GetProperty("word").GetString() ?? string.Empty,
                e.GetProperty("start").GetDouble(),
                e.GetProperty("end").GetDouble()))
            .ToList();

        Assert.Equal(13175, words.Count);

        var cues = SrtBuilder.BuildCues(words);
        var mediaSeconds = 8716.98;

        var speechSeconds = cues.Sum(c => c.EndSeconds - c.StartSeconds);
        var density = speechSeconds / mediaSeconds;

        var gaps = new List<double>();
        for (var i = 1; i < cues.Count; i++)
        {
            gaps.Add(cues[i].StartSeconds - cues[i - 1].EndSeconds);
        }

        var pausesOverTwoSeconds = gaps.Count(g => g > 2.0);

        // A text only engine on this same episode produced 97.4% density with zero pauses
        // over two seconds. Real timings must land nowhere near that.
        Assert.InRange(density, 0.35, 0.70);
        Assert.True(pausesOverTwoSeconds > 200, $"expected many real pauses, found {pausesOverTwoSeconds}");

        _output.WriteLine($"cues            : {cues.Count}");
        _output.WriteLine($"speech density  : {density:P1}");
        _output.WriteLine($"pauses over 2 s : {pausesOverTwoSeconds}");
        _output.WriteLine($"largest gap     : {gaps.Max():F1} s");
        _output.WriteLine($"first cue starts: {cues[0].StartSeconds:F1} s");
        _output.WriteLine($"smallest gap    : {gaps.Min():F3} s");
        _output.WriteLine($"gaps under 79 ms: {gaps.Count(g => g < 0.079)} of {gaps.Count}");
        _output.WriteLine($"gaps in [79,81] ms: {gaps.Count(g => g >= 0.079 && g <= 0.081)}");
        _output.WriteLine($"gaps at the 1 ms floor: {gaps.Count(g => g < 0.002)}");
        _output.WriteLine($"median cue length: {cues.Select(c => c.EndSeconds - c.StartSeconds).Order().ElementAt(cues.Count / 2):F2} s");

        // The opening theme is genuinely empty, so the first cue arrives late.
        Assert.True(cues[0].StartSeconds >= 0.0);

        // And no cue may touch its neighbour.
        Assert.DoesNotContain(gaps, g => g <= 0.0);
    }
}
