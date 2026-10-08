# Transcribe long audio and video with Google Speech-to-Text (chirp_3)

Real word level timings, guards against the ways this API fails on long recordings, and a worked
method for checking the result. It does not need Subtitle Edit. Use it the way that suits you:

| You want to | Use | Needs Subtitle Edit? |
|:--|:--|:--|
| Transcribe a video or audio file from the command line | [The standalone tool](#the-standalone-tool) | No |
| Transcribe a recording where two languages alternate, with speaker labels | [The two language tool](#two-languages-in-one-recording) | No |
| Do it from inside Subtitle Edit | [Subtitle Edit](#in-subtitle-edit): built in, or this plugin | Yes |
| Have an AI assistant (Claude, ChatGPT, others) run it and check the result | [With an AI assistant](#with-an-ai-assistant) | No |

All three rest on two guides, written as rules, with nothing specific to one recording:

- [docs/lessons.md](docs/lessons.md): what to expect from the model, what can come back wrong even when
  a job reports success, and how to recognise and repair it.
- [docs/episode-workflow.md](docs/episode-workflow.md): the step by step way of working: run the tool,
  read what it flags, re-check each flagged stretch with a fresh short request, patch the subtitle
  safely, and write the notes.

## Why word level timings

Online transcription engines that return text only force a subtitle editor to infer cue times by
splitting text proportionally to character count, which cannot represent silence. On a 145 minute
episode that produces a subtitle claiming 97.4% speech density with zero pauses over two seconds. The
same audio through Speech-to-Text v2 measures 57.2% density with 313 real pauses, and leaves the
opening theme correctly empty.

## What you need

1. A Google Cloud project with the **Speech-to-Text API** enabled.
2. A **service account JSON key**. Speech-to-Text v2 refuses API keys outright:
   `401, API keys are not supported by this API. Expected OAuth2 access token.`
   Create one under IAM and Admin, Service Accounts, Keys, Add key, Create new key, JSON.
3. Roles on that service account: **Cloud Speech Client**, plus read and write on one Cloud Storage
   bucket. BatchRecognize reads from Cloud Storage only, so long audio has no inline path. The plugin
   can create its own bucket and then needs **Storage Admin**; the standalone tool only reads, writes
   and deletes objects, so object level access on one bucket is enough.
4. **ffmpeg**.

Keep the key out of version control, chat and screenshots. Share its path, never its contents.

## The standalone tool

[tools/transcribe-episode.py](tools/README.md) turns a video or audio file into a subtitle with no
Subtitle Edit involved. It needs Python 3.9 or newer, `ffmpeg`, `ffprobe` and the Google Cloud CLI, and
nothing else to install: it uses the standard library only. It runs on macOS, Linux and Windows.

```bash
git clone https://github.com/muaz978/subtitleedit-gcloud-stt-plugin.git
cd subtitleedit-gcloud-stt-plugin
python3 tools/transcribe-episode.py "episode.mp4"      # on Windows: python
```

Put your project, bucket and language in a small settings file once (see
[tools/README.md](tools/README.md#setup-once)). The language is a setting, not a built-in: set
`SE_STT_LANGUAGE` to `tr-TR`, `ar-XA`, `en-US` or any other code `chirp_3` accepts.

A run writes the subtitle next to the video, a notes file that says what to check by ear, and a
`report.json` with the same findings in machine readable form. On the way it:

- cuts the audio in silence into chunks under the 20 minute limit, never by the clock
- sends several chunks at once, and gives up on a hung request and re-cuts it smaller
- detects a chunk Google cut short while reporting success, and re-sends the missing tail
- finds stretches with no words and sends them again in short pieces, to recover skipped speech
- collapses hallucinated loops and repairs impossible word timings
- builds cues on real pauses, runs its own checks and writes the notes

A rerun that reuses the saved responses is free. A 2.5 hour episode costs about 0.5 to 0.8 US dollars,
recovery included, and takes 15 to 40 minutes. See [Cost](#cost).

To check a finished subtitle, [tools/qc](tools/qc/README.md) holds helper scripts that read an episode again in
short pieces, compare the reads with the subtitle, vote between two reads and patch the subtitle safely.

## Two languages in one recording

[tools/transcribe-broadcast.py](tools/README.md#a-programme-in-two-languages-transcribe-broadcastpy) is a second standalone
tool, for a recording where two languages alternate: an interview with a host in Arabic and a guest answering in English,
say. It builds on the first tool's guards. It recognizes the audio once per language, adds a third pass in short windows
to get speaker labels, then decides the language of each stretch word by word from the first two. It writes a subtitle, a
word level JSON file with language and speaker on every word, an events file and a notes file.

```bash
python3 tools/transcribe-broadcast.py "interview.wav" "out/interview" "out/work"
```

It needs the same setup as the first tool. Because it runs three recognition jobs, billed audio is about three and a half
times the recording's length, so expect roughly that multiple of the cost in the [Cost](#cost) table below.

## In Subtitle Edit

> **Subtitle Edit 5 now ships Google Cloud Speech-to-Text as a built-in engine.** Pick it under
> **Video, Audio to text**, in the same list as Whisper and the other online engines. It uses the
> same API, the same `chirp_3` model and the same word level timings this plugin used, and it needs
> no 40 MB download. If you only want to transcribe inside Subtitle Edit, use that.
>
> The work here was merged upstream:
> [#14561](https://github.com/SubtitleEdit/subtitleedit/pull/14561) added the engine,
> [#14567](https://github.com/SubtitleEdit/subtitleedit/pull/14567) brought over the audio format,
> the cue building from word timings and the reliability guards below, and
> [#14582](https://github.com/SubtitleEdit/subtitleedit/pull/14582) adds sign-in with `gcloud` for
> organisations that do not allow service account keys. The testing evidence behind those guards is in
> [docs/testing-evidence.md](docs/testing-evidence.md).

The plugin in this repository is a Subtitle Edit 5 plugin that does the same job. The
[v1.0.0 release](https://github.com/muaz978/subtitleedit-gcloud-stt-plugin/releases/tag/v1.0.0) still
works, but the built-in engine is better maintained. For long or difficult audio, the standalone tool
above carries more guards than either (see the table in [docs/lessons.md](docs/lessons.md)).

**Installing the plugin.** Download the zip for your platform from the releases page and either
extract it into Subtitle Edit's `Plugins` folder, or use **Plugins, Manage plugins, Get plugins
online**. The plugin uses whichever `ffmpeg` Subtitle Edit already uses. If Subtitle Edit can extract
a waveform, the plugin can extract audio.

**Using it.**

1. Open a video in Subtitle Edit.
2. **Plugins, Google Cloud Speech-to-Text**.
3. Choose your service account key. The project id is read from the key file.
4. Pick the spoken language and press **Transcribe**.

The plugin replaces the current subtitle with the transcription, as one undo step.

## With an AI assistant

The two guides are written so that an assistant can follow them. How you use them depends on what
the assistant can do:

- **An assistant that can run commands on your machine** (for example Claude Code, OpenAI Codex, or any
  agent with a shell). Point it at a clone of this repository and ask: "Read AGENTS.md, then
  transcribe `<your video>` following docs/episode-workflow.md." It runs the tool with your own
  settings, reads what the tool flagged, re-checks each flagged stretch, patches the subtitle and
  writes the notes. Claude Code reads [CLAUDE.md](CLAUDE.md) on its own; Codex and many other agents
  read [AGENTS.md](AGENTS.md). Both say the same thing.
- **A chat assistant with no access to your files** (for example ChatGPT in the browser). It cannot run
  the tool. Paste [docs/lessons.md](docs/lessons.md) into the conversation, then paste the notes file
  the tool wrote, and ask it to help you work through what to check and how to repair each finding.
- **Anywhere else.** The guides are plain Markdown. Give them to whatever you use as instructions.

The assistant never needs your key in the conversation. It runs the tool on your machine, where your
settings and key already are. Do not paste the key file into a chat.

## The five rules

The short version of [docs/lessons.md](docs/lessons.md):

1. Cut audio in silence, never by the clock.
2. Never resend identical audio, because the answer is deterministic. Change the cut instead.
3. Success does not mean complete. Check coverage and read the notes.
4. A bad timestamp is not a bad word. Repair the timing, do not delete the word.
5. One language code, one source.

## Cost

Google bills per minute of audio.

| Mode | Price per minute | A 2.5 hour episode |
|:--|:--|:--|
| Dynamic batching (default) | $0.003 | about $0.44 |
| Standard | $0.016 | about $2.32 |

Dynamic batching is roughly an 81% discount in exchange for a slower turnaround. Billed audio is
usually 7 to 50% more than the recording's length, because recovery pieces and re-cuts are billed
too. The plugin window shows the estimate for the video you have open before you start.

Staged audio is deleted from Cloud Storage when a run finishes. The plugin's bucket carries a one day
lifecycle rule so an interrupted run cannot leave anything behind.

## How the plugin works

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

The standalone tool adds more guards, for stalls, loops, silent holes and replayed speech; each is
explained in [docs/lessons.md](docs/lessons.md).

## Known limitations

- **Subtitle Edit will not launch any plugin when the subtitle is empty.** Until that
  changes upstream, add one placeholder line before running the plugin on a fresh video.
- `chirp_3` is served from regional endpoints only, not the global one. The plugin uses
  `us-speech.googleapis.com`.
- Google's own documentation lists word level timestamps under features `chirp_3` does not
  support. In practice they are returned on every run, and the same page's own text and
  code sample describe enabling them. The guard above exists in case that ever changes.

## Building the plugin

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
