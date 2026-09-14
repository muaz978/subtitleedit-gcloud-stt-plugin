# Transcribing an episode

## Prerequisites

Python 3.9 or newer, plus `ffmpeg`, `ffprobe` and the Google Cloud CLI on PATH. Works on macOS,
Linux and Windows. If any of the three is not on PATH, point at it directly with `SE_STT_FFMPEG`,
`SE_STT_FFPROBE` or `SE_STT_GCLOUD`. Nothing else to install: the script uses the standard
library only.

## Setup, once

The script carries no project, bucket or account name. Put yours in
`~/.config/se-stt/config.env`:

```
SE_STT_PROJECT=your-gcp-project-id
SE_STT_BUCKET=your-gcs-bucket
SE_STT_LANGUAGE=tr-TR
SE_STT_SERVICE_ACCOUNT=stt@your-project.iam.gserviceaccount.com   # optional
SE_STT_REGION=us                                                  # optional
SE_STT_MODEL=chirp_3                                              # optional
```

Any of these can be exported in the shell instead; the file never overrides a variable that
is already set. If you set `SE_STT_SERVICE_ACCOUNT`, register its key once and every gcloud
call is pinned to it, so a run never depends on whichever account happens to be active:

```bash
gcloud auth activate-service-account --key-file=/path/to/key.json
```

The account needs Cloud Speech Client on the project and read/write on the bucket. Bucket
level admin is not required: the script only ever reads, writes and deletes objects.

## Running it

```bash
python3 tools/transcribe-episode.py "Episode 12.mp4"
```

On Windows the command is `python`, not `python3`. The installer ships `python.exe` only, and the
`python3` that Windows puts on PATH is a Microsoft Store stub which opens the Store instead of
running anything.

The full form is `transcribe-episode.py <video> [output.srt] [work-dir]`. A normal run writes:

- the subtitle next to the video, one line per cue as before
- `<subtitle name> - transcription notes.md` next to it: what an editor should check, with times
  and cue numbers (turn off with `SE_STT_NOTES=0`)
- `report.json` in the work directory (`~/.cache/se-stt/<video>`), with the same findings in
  machine-readable form
- `before-recovery.srt` in the work directory, when words were recovered (see below), so the
  recovered words can be compared against the plain run

## Read the last lines when it finishes

```
VERIFY: Google returned 13136 words, 33 looped removed, 1 dropped at tail splices, subtitle carries 13489 (387 recovered)
RECOVERY: 38 gaps checked, 51 pieces (1608 s of audio), 387 words inserted, 3 already present, 7 filtered, 0 gaps skipped
TIMING: 00:01:46-00:01:53 7 words at a chunk start moved +105.8 s
CHECKS: warn (0 fail, 4 warn, 4 note)
billed 1616 s (about $0.08)
```

- **VERIFY** adds up: Google's words, minus looped repeats removed, minus words a truncated
  chunk handed over to its re-transcribed tail, plus recovered words, is what the subtitle
  carries. The script checks the arithmetic itself and prints `WARNING: internal word accounting
  mismatch` if it ever fails, which would be a bug, not an audio problem.
- **RECOVERY** reports the stretches without words that were transcribed again (see below).
  "Already present" words were found in the subtitle already, usually because the model had
  timed them elsewhere; that is expected, not a loss. Skipped gaps are broken down as over
  budget, failed, or still running at Google.
- **TIMING** lists every place where more than 5 s of timing was repaired. The words were
  there all along with wrong timestamps, and the repair can still leave them a little off (one
  opening moved 161 s still sat 51 s early), so check the timing at each listed place.
- **CHECKS** is `pass`, `warn` or `fail`. The notes file lists each item.
  - `fail`: the subtitle file has a broken cue (inverted, out of order, overlapping, or outside
    the episode), which should never happen, or words cover under 20% of the audio, which
    usually means `SE_STT_LANGUAGE` is wrong. Check before using the file.
  - `warn`: a stretch to check by ear: a timing move over 5 s, three or more words timed inside
    detected silence, subtitle words that recovery heard again far from where they are timed (a
    displaced run of 20 words or more, or more than 5 s away, or a few words that are also moved
    or inside silence), a part of a recovered stretch where a piece answered with its first
    seconds only, or a stretch that could not be checked for missing speech (the recovery budget
    ran out, its pieces failed, or Google is still working on them). When recovery hears a
    displaced run, nothing is inserted in that gap, so listen to it too. The subtitle is still
    complete for everything else.
  - `note`: recovered stretches, stretches of 45 s or more with no speech found (usually music),
    short displaced runs, and chunks with unusually few words per minute (listed in the notes under
    "Slow chunks": speech can be skipped in pauses too short for recovery to look at).
- The **billing** line counts only audio sent to Google in this run. A rerun that reuses every
  saved response says `all responses reused, nothing billed`. Operations still running when the
  run ends are billed when Google finishes them, and the line says so: `nothing billed yet; 1
  operation(s) still pending, billed when they finish`.

The final line also gives the speech density: the share of the episode covered by words. On the
cached test set it was about 45% for Turkish drama, 29 to 31% for Arabic drama, 70% for a
narration-heavy film and 33 to 38% for short clips. A figure far below what the material
usually gives means speech was missed or the language setting is wrong. Density used to be
measured from cue durations, which the minimum display time now stretches, so older reports
are about 2 to 8 points higher.

The script always writes the subtitle once the chunks are recognized. Recovery, checks, notes
and the report are extras: if one of them fails it logs `WARNING:` and the run continues.

In the notes, a finding about the whole episode (broken cues, too little speech, recovery
failing) comes first as its own `Whole episode:` line, so the stretches below it keep their own
times and cue numbers. Recovered stretches of fewer than 5 words are listed on one line under
"Short recovered words": these are the most likely to be noise from music, so check they are
speech. Laughter or sighs timed inside silence or moved by the timing repair are not listed: there
is nothing to check about where a laugh sits.

Closing the terminal or killing the process (SIGTERM, SIGHUP, or closing the console on Windows)
stops the run: uploaded audio is still cleaned up, no subtitle is written, and the exit status is
128 plus the signal number (143 for SIGTERM).

## What it does, and why

Cutting audio by the clock lands mid-sentence, and that single fact caused nearly every
defect seen on real episodes: silently truncated chunks, words timestamped past the end of
their chunk, words with no timestamp at all, and scrambled sentences. So:

1. **Boundaries land in silence.** Silences are detected and each cut moves up to 40 s to
   reach one. Measured effect: chunk 1 went from 76 broken timings to 0, and a chunk that
   had truncated deterministically twice, losing 164 s of dialogue, stopped truncating.
2. **A bad chunk is re-cut and re-transcribed.** Recognition is deterministic, so resending
   the same audio returns the same failure. Changing where it is cut changes the answer.
   Fired three times on one episode, each time taking about 28% broken timings to zero.
3. **Hallucinated loops are collapsed.** The model sometimes gets stuck: one chunk repeated
   `Benim evim.` 55 times. A phrase repeated 3 or more times back to back is trimmed to 2.
   This is Google's failure, not the script's; it can be contained but not removed.
4. **Every raw response is cached, and checked before it is reused.** Saved responses under
   `~/.cache/se-stt/<video>` are looked up before anything is cut or uploaded, so a rerun that
   has them all makes no network call and needs no project or bucket setting. Each response
   records the span, language, model and video size it was made for; one that does not match
   is moved to `raw/stale` and fetched again. A submitted operation is written to disk at
   once, so a crash or Ctrl+C resumes it instead of paying for the same audio twice. A rerun
   that cannot reach Google, or cannot get an access token, keeps that record for the next run;
   only Google answering that the operation no longer exists sends the audio again.
5. **Timings are rebuilt from the end offsets, never by deleting words.** On cached episodes the
   END of each word was accurate (median 0.10 s against an independent aligner) while the start
   was usually just the previous word's end, and Google's word order held even where the numbers
   did not (262 of 263 checked words). The repair keeps each word's end, moves whole blocks that
   were timed minutes away (a block of 81 words sat 165 s too early on one episode), re-places the
   compressed first words of a chunk, and packs words without a usable end right before the next
   trusted word. Measured on the words it moves: mean end error from 74 s to 0.55 s. Against an
   editor's timing of an Arabic episode, cues starting more than 5 s off went from 17 to 1. Which
   words survive a tail splice, and whether a chunk is re-cut, is still decided by the previous
   method, so the word sequence and the API calls are exactly what they were.
6. **Long stretches without words are transcribed again in short pieces.** The model can skip
   minutes of speech inside one response; one episode lost 6 minutes of dialogue under music
   that no word count could see. Every word gap of 15 s or more (the start and end of the episode
   included) is re-sent as 45 s pieces with 4 s overlap, which returned that speech: about 400
   words. Sung lyrics are the least reliable part: a piece that starts half a second elsewhere
   inside a song can return a different line, or nothing. Recovered words are only inserted when
   they are really missing, because gaps are often made by words the model timed elsewhere; a
   naive merge was measured to add 154 duplicated words on an Arabic episode. So a recovered word
   is dropped when it lines up with a word already near it, when it spells a word at the same
   place differently (a name heard two ways), when it repeats a loop the collapser removed, or
   when the edge of the gap turns out to be a run of mistimed words (then nothing is inserted for
   that gap and the notes say so). Near a chunk start the comparison reaches back to the chunk
   start, where a compressed opening can sit minutes early, and the notes point at that early
   copy. Also dropped: a gap whose new words are all sighs or laughter, Latin fragments under
   Arabic speech, and a word that kept its own timestamp yet lies inside detected silence (a
   second copy from an overlapping piece). Where two pieces overlap, a word both heard is kept
   once, from the piece that kept the word's own timestamp. Existing words are never moved or removed. Gaps are
   handled longest first, and recovery never sends more than 60% of the episode's length. A piece
   that starts on the last words before its gap keeps them where they are; one that starts inside
   music gets its compressed opening repaired like a chunk, including a later piece whose first
   words came back seconds before the rest of what it heard.
7. **Cues keep phrases whole and can no longer be inverted.** Cues end at a sentence end
   (including `؟`), 84 characters, 7 s of speech or a pause of 0.7 s, except that a short pause
   (under 1.5 s) right after a word like `ve` or `في`, or just before a sentence finishes within
   two words, does not split. A word that would take a cue past 84 characters or 7 s starts the
   next cue, and the cut backs off to the best comma or pause among the last six words, so no cue
   of two or more words is longer. A final timing pass keeps 80 ms between cues, fixes cues that start
   before the previous one by moving whichever of the two moves less (and reports it), and holds
   short cues for 1 s plus a 0.1 s linger, only into free time.
8. **Every run checks itself and leaves notes.** Besides the arithmetic above: cue integrity,
   runs of words timed inside silence (3 real misplacements found on cached episodes, no false
   alarm), large timing moves, stretches still silent after recovery, and slow chunks.

## Settings

`chirp_3` on Speech-to-Text v2, regional endpoint `us-speech.googleapis.com`, language
`tr-TR`, word time offsets and automatic punctuation on, `DYNAMIC_BATCHING` for cost, audio
as 16 kHz mono 16-bit FLAC in about 18 minute chunks.

Deployment values come from the environment (see Setup). These change behavior:

| Variable | Default | Effect |
|---|---|---|
| `SE_STT_GAP_MIN` | `15` | Seconds without words before a stretch is transcribed again, 5 to 600. Gaps under 20 s yielded only interjections when measured; raise it to spend less. A value outside the range is clamped, one that is not a number falls back to 15, each with a warning. |
| `SE_STT_RECOVER` | on | `0` skips hole recovery entirely. |
| `SE_STT_NOTES` | on | `0` skips the transcription notes file. |
| `SE_STT_CUE_BUILDER` | new | `legacy` uses the previous cue rules (84 characters, 6 s, 0.7 s pause, no bridging). |

The rest are constants at the top of the script and are fine to edit, with two cautions:
- keep `-sample_fmt s16`, or ffmpeg writes 24-bit FLAC, 78% larger than the raw PCM
- keep chunks under 20 minutes, which is Google's cap when word timestamps are enabled (the
  script asserts it)

## What it costs and how long

About **8x realtime** and **$0.003 per minute** of audio. A 169 minute episode took 20.8
minutes and $0.51. Without dynamic batching the same episode costs $2.71.

Hole recovery adds billed audio: typically 7 to 50% more, the most on music-heavy episodes,
which is a few cents. Measured: 1616 s billed ($0.08, 17% more) for a 154 minute Turkish episode
and 1344 s ($0.07, about 50% more) for a 44 minute Arabic one, in under 3 minutes each: the pieces
are uploaded four at a time and polled together. A rerun reuses the saved pieces and costs nothing.
Recovered audio can still carry noise: on Arabic episodes a few short words from music or
chanting per episode get inserted, so check the "Short recovered words" line of the notes.

Every upload location is written to `uploads.json` in the work directory before anything is
uploaded, so audio is found again even when the terminal is closed or the process is killed.
Uploaded audio is deleted at the end of each run, except the files an operation still reads:
those stay, the location stays listed, and a later run in the same work directory removes them
once no saved operation needs them. An episode that is never run again in that work directory
keeps those files, so give the bucket a lifecycle rule that deletes objects under `stt/` older
than 2 days (longer than the 24 hours an operation record is resumed for). Setting a lifecycle
rule needs bucket admin rights, which the transcription account does not need and should not
have, so whoever administers the bucket runs it, for example:

```bash
cat > lifecycle.json <<'JSON'
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 2, "matchesPrefix": ["stt/"]}}]}
JSON
gcloud storage buckets update gs://your-gcs-bucket --lifecycle-file=lifecycle.json
```

Google usually answers a recovery piece within two minutes, but it has held one for almost three
hours before failing it. The run waits up to 20 minutes for the pieces it sends; once all but the
last tenth of them have answered, the rest get five times the usual answer time, and at least 2
minutes. Then the run ends and writes the subtitle without them; those gaps are reported as still
running. A later run picks the operations up: it waits for whatever is left of their 20 minutes,
and at least 2 minutes, sends a failed piece once more, and reports a piece that failed twice
instead of sending it a third time. If Google cannot be reached at all, the wait ends after three
rounds without any answer, and when no access token can be had, recovery stops before any piece is
uploaded and the notes say so once. A status request whose answer is lost on the way (a dropped
connection, a cut-off response) is asked again. A submission is only sent again when it cannot
have reached Google (the name did not resolve, the connection was refused) or Google answered
with a rate limit or server error: once the request is out, Google may have started the operation,
and a second one would be billed too.

## Tests

From the repository root:

```bash
python3 -B -m unittest discover tools/tests
```

(`python` on Windows.) The tests use synthetic data only: no network, no media, no gcloud. `-B`
keeps Python from leaving `__pycache__` folders in the repository.
