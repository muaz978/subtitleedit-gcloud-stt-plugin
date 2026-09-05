using SubtitleEdit.GoogleCloudStt.Google;
using System.Globalization;
using System.Text;

namespace SubtitleEdit.GoogleCloudStt.Transcription;

public sealed record SubtitleCue(double StartSeconds, double EndSeconds, string Text);

/// <summary>
/// Groups timed words into subtitle cues.
///
/// Because real word offsets are available, cues can break where speech actually pauses.
/// That is the whole difference from a text only engine, where cue times are derived from
/// character counts and silence cannot be represented at all.
/// </summary>
public static class SrtBuilder
{
    /// <summary>A pause at least this long ends a cue, so silence stays silent.</summary>
    private const double PauseSeconds = 0.7;

    private const double MaxCueSeconds = 6.0;
    private const double MinCueSeconds = 0.9;

    /// <summary>
    /// Consecutive cues must never touch. Two words can legitimately abut in the word
    /// offsets (one ends exactly where the next begins), and if a cue boundary falls
    /// there the two cues meet at a zero gap. That is the visual signature of arithmetic
    /// subdivision rather than measured speech, and it also makes players flash between
    /// cues. Two frames at 25 fps is the usual convention.
    /// </summary>
    private const double MinGapSeconds = 0.08;

    /// <summary>The shortest a cue may be squeezed to in order to open a gap.</summary>
    private const double FloorCueSeconds = 0.3;
    private const int MaxCharacters = 84;
    private const int MaxLineLength = 42;

    private static readonly char[] SentenceEndings = ['.', '!', '?', '…'];

    public static IReadOnlyList<SubtitleCue> BuildCues(IReadOnlyList<WordTiming> words)
    {
        var cues = new List<SubtitleCue>();
        if (words.Count == 0)
        {
            return cues;
        }

        var current = new List<WordTiming>();

        foreach (var word in words)
        {
            if (current.Count > 0 && ShouldBreakBefore(current, word))
            {
                cues.Add(CreateCue(current));
                current.Clear();
            }

            current.Add(word);
        }

        if (current.Count > 0)
        {
            cues.Add(CreateCue(current));
        }

        return EnforceMinimumDurations(cues);
    }

    private static bool ShouldBreakBefore(List<WordTiming> current, WordTiming next)
    {
        var last = current[^1];

        // A real pause in the audio.
        if (next.StartSeconds - last.EndSeconds >= PauseSeconds)
        {
            return true;
        }

        // End of a sentence.
        if (last.Text.Length > 0 && SentenceEndings.Contains(last.Text[^1]))
        {
            return true;
        }

        if (next.EndSeconds - current[0].StartSeconds > MaxCueSeconds)
        {
            return true;
        }

        var length = current.Sum(w => w.Text.Length + 1) + next.Text.Length;
        return length > MaxCharacters;
    }

    private static SubtitleCue CreateCue(List<WordTiming> words)
        => new(words[0].StartSeconds, words[^1].EndSeconds, WrapLines(string.Join(' ', words.Select(w => w.Text))));

    /// <summary>Wraps to at most two roughly balanced lines, breaking on a space.</summary>
    private static string WrapLines(string text)
    {
        if (text.Length <= MaxLineLength)
        {
            return text;
        }

        var middle = text.Length / 2;
        var breakIndex = -1;
        var bestDistance = int.MaxValue;

        for (var i = 0; i < text.Length; i++)
        {
            if (text[i] != ' ')
            {
                continue;
            }

            var distance = Math.Abs(i - middle);
            if (distance < bestDistance)
            {
                bestDistance = distance;
                breakIndex = i;
            }
        }

        return breakIndex <= 0
            ? text
            : text[..breakIndex] + "\n" + text[(breakIndex + 1)..];
    }

    /// <summary>
    /// Applies the two timing rules that survive contact with real word offsets: a cue
    /// must be readable, and no two cues may touch.
    /// </summary>
    private static IReadOnlyList<SubtitleCue> EnforceMinimumDurations(List<SubtitleCue> cues)
    {
        // Lengthen anything too brief to read, without running into the next cue.
        for (var i = 0; i < cues.Count; i++)
        {
            var cue = cues[i];
            if (cue.EndSeconds - cue.StartSeconds >= MinCueSeconds)
            {
                continue;
            }

            var desiredEnd = cue.StartSeconds + MinCueSeconds;
            if (i + 1 < cues.Count)
            {
                desiredEnd = Math.Min(desiredEnd, cues[i + 1].StartSeconds - MinGapSeconds);
            }

            if (desiredEnd > cue.EndSeconds)
            {
                cues[i] = cue with { EndSeconds = desiredEnd };
            }
        }

        // Then open a gap wherever two cues still meet. The earlier cue gives way, because
        // pulling its end in is less noticeable than delaying the next line's appearance.
        for (var i = 0; i + 1 < cues.Count; i++)
        {
            var current = cues[i];
            var next = cues[i + 1];
            var gap = next.StartSeconds - current.EndSeconds;
            if (gap >= MinGapSeconds)
            {
                continue;
            }

            var trimmedEnd = next.StartSeconds - MinGapSeconds;

            // Never trim a cue below the readable floor. When there is not enough room for
            // the full gap, take the largest one that still fits.
            var floorEnd = current.StartSeconds + FloorCueSeconds;
            if (trimmedEnd < floorEnd)
            {
                trimmedEnd = Math.Max(floorEnd, next.StartSeconds - 0.001);
                trimmedEnd = Math.Min(trimmedEnd, next.StartSeconds - 0.001);
            }

            if (trimmedEnd < current.EndSeconds)
            {
                cues[i] = current with { EndSeconds = trimmedEnd };
            }
        }

        return cues;
    }

    public static string ToSrt(IReadOnlyList<SubtitleCue> cues)
    {
        var builder = new StringBuilder();
        for (var i = 0; i < cues.Count; i++)
        {
            builder.Append((i + 1).ToString(CultureInfo.InvariantCulture)).Append('\n');
            builder.Append(FormatTime(cues[i].StartSeconds))
                .Append(" --> ")
                .Append(FormatTime(cues[i].EndSeconds))
                .Append('\n');
            builder.Append(cues[i].Text).Append('\n').Append('\n');
        }

        return builder.ToString();
    }

    private static string FormatTime(double seconds)
    {
        if (seconds < 0.0)
        {
            seconds = 0.0;
        }

        var time = TimeSpan.FromSeconds(seconds);
        return string.Create(CultureInfo.InvariantCulture,
            $"{(int)time.TotalHours:D2}:{time.Minutes:D2}:{time.Seconds:D2},{time.Milliseconds:D3}");
    }
}
