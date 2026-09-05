# Testing evidence

Everything here comes from real runs against the live APIs during development, on Turkish
TV drama. Figures are marked with where they come from, and anything that did not survive
re-verification against the raw output has been corrected or dropped rather than repeated.

## What was run

| | Gönül Dağı 220 | Teşkilat 182 | Mehmed (trailer) |
|:--|:--|:--|:--|
| Audio length | 145.28 min | 139.74 min | 0.89 min |
| Billing mode | standard rate | dynamic batching | dynamic batching |
| Cost | about $2.79 | $0.42 | $0.003 |
| Words with timings | 13,175 | 9,432 | 78 |
| Subtitle cues | 2,978 | 2,370 | 16 |
| Speech density | 54.3% | 39.4% | 65.9% |
| Defect encountered | silent truncation, 11.4 min lost | 79 corrupt word offsets | clean |

Audio lengths are from `ffprobe`. Word counts, cue counts and densities were recomputed
directly from the delivered SRT and word offset files, not copied from a summary.

The Gönül Dağı cost is stated as about $2.79 because that is what the inputs support:
145.28 min plus 29.3 min of re-processing to recover the truncated chunk, at $0.016 per
minute. An earlier internal summary said $2.83, which does not reconcile with its own
stated components.

## Configuration that produced these results

- **API**: Google Cloud Speech-to-Text **v2** (`google.cloud.speech_v2`).
- **Model**: `chirp_3`.
- **Endpoint**: `us-speech.googleapis.com`, recognizer
  `projects/<id>/locations/us/recognizers/_`. Chirp is not served on the global endpoint.
- **Features**: `enable_word_time_offsets`, `enable_automatic_punctuation`.
- **Audio**: 16 kHz mono 16 bit FLAC, split into 1,080 second chunks.
- **Auth**: service account JSON. API keys are refused with
  `401, API keys are not supported by this API. Expected OAuth2 access token.`

`chirp_2` was also tried, and returned `400 does not exist` against the us and eu
endpoints.

## Timing quality, same episode, both engines

| Metric | OpenRouter, text only | Speech-to-Text v2 |
|:--|:--|:--|
| Cue timing source | character count | measured word offsets |
| Pauses longer than 2 s | 0 | 334 |
| Largest gap | 0.85 s | 138.4 s |
| Consecutive cues touching at 1 ms | 35 of 50 | none |
| Speech density | 97.4% | 54.3% |

The OpenRouter figures are measured over the sample it produced rather than a full
episode, so they describe the shape of the timing, not coverage. That shape does not
improve with length: character proportional timing cannot represent silence at any
duration.

An earlier internal summary gave 301 pauses and a 681 s largest gap for the v2 column.
Recomputing from the delivered SRT gives **334** and **138.4 s**; the 681 s figure was the
truncation hole before recovery, not a real gap.

## Defects found in Speech-to-Text v2, and what this plugin does about them

| Defect | Evidence | Handling |
|:--|:--|:--|
| **Silent truncation** | A 1,080 s chunk returned words only to 398.36 s with the job reporting success. Reproducible; it truncated identically on a single file retry. | Coverage is checked per chunk. A short chunk has its tail re-cut and resubmitted. |
| **Corrupt word offsets** | 79 of 9,432 words (0.8%) carried impossible timings, one claiming 6,324 s inside a 1,080 s chunk. Unfiltered they produced a negative total speech time. | Every word is range checked against its own chunk and dropped if impossible. |
| **Max 5 files per batch** | A 9 file request is rejected outright. | The plugin sends one file per request, so the cap cannot be hit. |
| **Inline results, single file only** | Inline output is refused for multi file requests. | Same: one file per request keeps inline output available and avoids a Cloud Storage download and parse step. |
| **24 bit FLAC** | Omitting `-sample_fmt s16` yields 24 bit FLAC, 78% larger than 16 bit, verified on ffmpeg 8.1 and 9.0.1. | The flag is always passed, and the produced file is verified before upload. |
| **ffprobe lies about segments** | Stream copied segments report the source duration, so every 18 minute chunk claimed 8,384 s. | Chunk boundaries are computed arithmetically and never probed. |

## OpenRouter, for comparison

From Subtitle Edit's own logs across roughly 40 hours of runs:

| Observed | Count |
|:--|--:|
| `The selected model does not support response_format` | 211 |
| Automatic plain json retries fired | 203 |
| Hard failures | 17 |
| Opaque `Provider returned 400` | 12 |
| `No allowed providers` (an account level provider filter) | 10 |

The mp3 versus wav result, and the 60 s versus 65 s versus 180 s duration cap, each come
from paired runs on byte identical source audio.

## Verification done for this plugin

- The SRT builder was run against the real 13,175 word offsets from Gönül Dağı 220. It
  produces 3,014 cues, 57.2% speech density, 313 pauses over 2 s and a 137.9 s largest
  gap, which is the same shape as the Python pipeline that originally produced that
  episode.
- Each guard has a unit test built from the actual observed defect values above.
- The packaged binary is executed before it is zipped, exercising source generated JSON
  and the credential loader, because both fail at runtime rather than at build time.
