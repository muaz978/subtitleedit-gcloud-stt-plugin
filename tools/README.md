# Transcribing an episode

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
python3 tools/transcribe-episode.py "Gönül Dağı 221. Bölüm.mp4"
```

The subtitle is written next to the video. That is all a normal run needs.

## Read one line when it finishes

```
VERIFY: Google returned 12579 words, subtitle carries 12502
```

Those two numbers should differ only by the looped words the collapser removed, which the
log reports per chunk. If the gap is larger than that, something was dropped and the raw
responses are cached, so re-deriving costs nothing.

Also sanity check the density line: **40 to 55%** is normal for drama. Near 97% means the
engine returned text with no timings and the cue times are guesses.

## What it does, and why

Cutting audio by the clock lands mid-sentence, and that single fact caused nearly every
defect seen on real episodes: silently truncated chunks, words timestamped past the end of
their chunk, words with no timestamp at all, and scrambled sentences. So:

1. **Boundaries land in silence.** Silences are detected and each cut moves up to 40 s to
   reach one. Measured effect: chunk 1 went from 76 broken timings to 0, and a chunk that
   had truncated deterministically twice, losing 164 s of dialogue, stopped truncating.
2. **A bad chunk is re-cut and re-transcribed.** Recognition is deterministic, so resending
   the same audio returns the same failure. Changing where it is cut changes the answer.
   Fired three times on one episode, each time taking ~28% broken timings to zero.
3. **Hallucinated loops are collapsed.** The model sometimes gets stuck: one chunk repeated
   `Benim evim.` 55 times. A phrase repeated 3 or more times back to back is trimmed to 2.
   This is Google's failure, not ours; it can be contained but not removed.
4. **Timings are repaired, never words deleted.** Google's word order is reliable when its
   numbers are not, so broken timings are interpolated from trustworthy neighbours.
5. **Every raw response is cached** under `~/.cache/se-stt/<video>`. Re-running an episode
   reuses them and costs nothing, so a crash never loses paid work.

## Settings

`chirp_3` on Speech-to-Text v2, regional endpoint `us-speech.googleapis.com`, language
`tr-TR`, word time offsets and automatic punctuation on, `DYNAMIC_BATCHING` for cost, audio
as 16 kHz mono 16-bit FLAC in ~18 minute chunks.

Deployment values come from the environment (see Setup). The rest are constants at the top
of the script and are fine to edit, with two cautions:
- keep `-sample_fmt s16`, or ffmpeg writes 24-bit FLAC, 78% larger than the raw PCM
- keep chunks under 20 minutes, which is Google's cap when word timestamps are enabled

## What it costs and how long

About **8x realtime** and **$0.003 per minute** of audio. A 169 minute episode took 20.8
minutes and $0.51. Without dynamic batching the same episode costs $2.71.
