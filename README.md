# Google Cloud Speech-to-Text for Subtitle Edit 5

> ### This is now built into Subtitle Edit. You probably do not need this plugin.
>
> Subtitle Edit 5 ships **Google Cloud Speech-to-Text** as a built-in engine. Pick it under
> **Video, Audio to text**, in the same list as Whisper and the other online engines. It uses
> the same API, the same `chirp_3` model and the same word level timings this plugin used,
> and it needs no 40 MB download.
>
> The work here was merged upstream instead:
> [#14561](https://github.com/SubtitleEdit/subtitleedit/pull/14561) added the engine,
> [#14567](https://github.com/SubtitleEdit/subtitleedit/pull/14567) brought over the audio
> format, the cue building from word timings and the reliability guards below, and
> [#14582](https://github.com/SubtitleEdit/subtitleedit/pull/14582) adds sign-in with
> `gcloud` for organisations that do not allow service account keys.
>
> This repository stays up for the testing evidence in
> [docs/testing-evidence.md](docs/testing-evidence.md), which documents the defects found in
> Google's API across three full episodes and is the reasoning behind the guards now running
> in Subtitle Edit itself. The v1.0.0 release still works, but the built-in engine is better
> maintained and produces the same results.


A Subtitle Edit 5 plugin that transcribes the open video with Google Cloud
Speech-to-Text v2, using **real word level timings**.

That is the whole point. Online transcription engines that return text only force
Subtitle Edit to infer cue times by splitting text proportionally to character count,
which cannot represent silence. On a 145 minute episode that produces a subtitle
claiming 97.4% speech density with zero pauses over two seconds. The same audio through
Speech-to-Text v2 measures 57.2% density with 313 real pauses, and leaves the opening
theme correctly empty.

## What you need

1. A Google Cloud project with the **Speech-to-Text API** enabled.
2. A **service account JSON key**. Speech-to-Text v2 refuses API keys outright:
   `401, API keys are not supported by this API. Expected OAuth2 access token.`
   Create one under IAM and Admin, Service Accounts, Keys, Add key, Create new key, JSON.
3. Two roles on that service account:
   - **Cloud Speech Client**, to transcribe.
   - **Storage Admin**, so the plugin can create and manage its own staging bucket.
     BatchRecognize reads from Cloud Storage only, so long audio has no inline path.
4. **ffmpeg**. The plugin uses whichever copy Subtitle Edit already uses and does not
   bundle its own. If Subtitle Edit can extract a waveform, this plugin can extract audio.

## Installing

Download the zip for your platform from the releases page and either extract it into
Subtitle Edit's `Plugins` folder, or use **Plugins, Manage plugins, Get plugins online**.

## Using it

1. Open a video in Subtitle Edit.
2. **Plugins, Google Cloud Speech-to-Text**.
3. Choose your service account key. The project id is read from the key file.
4. Pick the spoken language and press **Transcribe**.

The plugin replaces the current subtitle with the transcription, as one undo step.

## Cost

Google bills per minute of audio.

| Mode | Price per minute | A 2.5 hour episode |
|:--|:--|:--|
| Dynamic batching (default) | $0.003 | about $0.44 |
| Standard | $0.016 | about $2.32 |

Dynamic batching is roughly an 81% discount in exchange for a slower turnaround. The
window shows the estimate for the video you have open before you start.

Staged audio is deleted from Cloud Storage when a run finishes, and the bucket carries a
one day lifecycle rule so an interrupted run cannot leave anything behind.

## How it works

1. Extract 16 kHz mono 16 bit FLAC with ffmpeg, then **verify** what was produced.
2. Split into 18 minute chunks. `chirp_3` caps BatchRecognize at 20 minutes per file when
   word timestamps are enabled.
3. Upload each chunk, and run one BatchRecognize request per chunk with inline results.
4. Guard the output, then assemble cues on real pauses.

### The guards, and why each exists

Every one of these was written against a defect observed in real runs.

| Guard | The defect it catches |
|:--|:--|
| Offset range check | 79 of 9,432 words (0.8%) carried impossible timings, one claiming 6,324 s inside a 1,080 s chunk. A single such word corrupts the whole timeline. |
| Coverage check with recovery | One 18 minute chunk stopped transcribing 6.6 minutes in and discarded the remaining 11.4 minutes **while reporting success**. The plugin detects the shortfall, re-cuts the tail and resubmits it. |
| Word timing guard | If the model ever returns text without timings, the plugin fails loudly rather than silently degrading to character proportional cues, which is the exact failure it exists to avoid. |

## Known limitations

- **Subtitle Edit will not launch any plugin when the subtitle is empty.** Until that
  changes upstream, add one placeholder line before running this plugin on a fresh video.
- `chirp_3` is served from regional endpoints only, not the global one. The plugin uses
  `us-speech.googleapis.com`.
- Google's own documentation lists word level timestamps under features `chirp_3` does not
  support. In practice they are returned on every run, and the same page's own text and
  code sample describe enabling them. The guard above exists in case that ever changes.

## Building

```bash
./scripts/publish.sh                 # every platform
./scripts/publish.sh osx-arm64       # just one
```

Builds are self contained, so users need no .NET runtime. Trimming is deliberately off:
Google's client libraries and Avalonia's designer support declare trim warnings, and a
trimmed build of that stack fails at runtime rather than at build time. The script runs
the published binary's `--selftest` before packaging it.

```bash
dotnet test tests/SubtitleEdit.GoogleCloudStt.Tests
```
