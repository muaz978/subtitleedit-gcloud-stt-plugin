# Lessons for transcribing with Google Speech-to-Text v2 (chirp_3)

These are the things learned from running this model on many hours of real, long-form audio
(TV drama, interviews, film). They are written as rules: what to do, what to avoid, and what to
watch for. Nothing here depends on a particular recording.

If you only read one section, read **The five rules** and **Reviewing a result**.

## The five rules

1. **Cut audio in silence, never by the clock.** Mid-sentence cuts caused most of the defects seen.
2. **Never resend identical audio.** Recognition is deterministic: the same audio and settings give
   the same answer, including the same failure. Change where it is cut instead.
3. **Success does not mean complete.** A job can report success while having dropped minutes of
   speech, repeated earlier speech, or placed words far from where they were spoken. Always check
   coverage and read the notes file.
4. **A bad timestamp is not a bad word.** Repair the timing, do not delete the word, and never sort
   by an untrusted field.
5. **Use one language code and one source.** Language lists silently return nothing, and mixing
   recognizers makes a subtitle impossible to audit.

## Who handles what

| Lesson | Python tool (`tools/transcribe-episode.py`) | Subtitle Edit plugin (C#) | You |
|:--|:--|:--|:--|
| Cut in silence | yes | fixed-length chunks | |
| Verify extracted audio | reads it back and checks the length | yes | check the format yourself |
| Check each chunk for truncation | yes, with re-cut | yes, with tail re-cut | |
| Range-check word timings | yes, and rebuilds them | yes, drops impossible words | |
| Collapse hallucinated loops | yes | no | review the result |
| Detect a hung request | yes, re-cuts | no | cancel and retry shorter |
| Recover skipped speech | yes | no | |
| Detect a replayed passage | no | no | yes, see "Replayed speech" |
| Verify proper names | no | no | yes |
| Pick the language code | you set it | you pick it | yes |
| Review notes and listen to flagged spots | writes the notes | no | yes |

If you run long or difficult audio, prefer the Python tool: it carries the guards the plugin lacks.

## Setup and access

**Do**
- Use a **service account** with the minimum roles: Cloud Speech Client on the project, and object
  read/write on the one staging bucket. Bucket-level admin is not needed.
- Use the **regional** endpoint and recognizer path (`us-speech.googleapis.com`,
  `projects/<id>/locations/us/recognizers/_`). `chirp_3` is not served on the global endpoint.
- Pin every `gcloud` call to the service account explicitly. A run must not depend on whichever
  account happens to be active.
- Give the staging bucket a lifecycle rule that deletes objects after about two days, so an
  interrupted run cannot leave audio behind. This needs bucket admin rights, so the person who
  administers the bucket sets it, not the transcription account.
- Keep the key file out of version control, chat and screenshots. Share the path, never the contents.

**Avoid**
- **API keys.** The API refuses them: `401, API keys are not supported by this API. Expected OAuth2
  access token.`
- Running two jobs with the same upload prefix. Include something unique per run (a timestamp is not
  enough when two start in the same second; add the process id).
- `gcloud auth revoke --all` on a machine that runs unattended. Expiring tokens in a non-interactive
  session fail with `Reauthentication failed. cannot prompt during non-interactive execution`.
- Assuming an environment variable is harmless. If `CLOUDSDK_AUTH_ACCESS_TOKEN` is set (some sandboxes
  and CI systems set a placeholder), it overrides your service account and produces 401s. Unset it for
  the run.

**Watch for**
- Models other than `chirp_3` are not generally available. `chirp` and `chirp_2` returned "does not
  exist" or "no longer generally available" in the regions tried.
- If bucket metadata is not readable by the account, tooling that insists on reading it will fail
  with a 403 even though uploads work.

## Language and model settings

**Do**
- Set **one** language code per run and match it to the audio.
- Turn on word time offsets and automatic punctuation. Word timings are the reason to use this API.
- Treat a speech density far below the norm (healthy dialogue-heavy material is roughly 40 to 55%,
  music-heavy material can be well under 30%) as a prompt to check the language setting first.

**Avoid**
- **Language lists** such as `ar-XA,en-US,fr-FR`. They return zero words with no error.
- A comma-joined single string such as `tr-TR,en-US`. This one at least fails loudly with
  `400: Bad language code`.
- Expecting `auto` to match a fixed code. In testing, `auto` and a fixed code agreed on only about
  nine words in ten, and `auto` can switch language in the middle of a cue.
- Expecting every locale to exist. Some (for example Pashto, `ps-AF`) are rejected by every model in
  the region, and the model may guess phonetically in Latin letters when pointed at a nearby code.
- Diarization (speaker labels) for anything but `en-US`. It is rejected for the Arabic locales tested,
  and long diarized windows come back cut short. Keep diarized windows to a few minutes.

**Watch for**
- Mixed-language audio. Run a language per pass and reconcile, rather than asking one pass to do both.
  The primary-language pass tends to transliterate the other language into its own script, and a pass
  in the other language can translate instead of transcribe.
- Google's documentation lists word timestamps as unsupported for `chirp_3`. In practice they are
  returned, but keep the guard that fails loudly if they ever stop, rather than silently falling back
  to timings spread by character count.

## Audio preparation

**Do**
- Extract **16 kHz, mono, 16-bit FLAC** and always pass `-sample_fmt s16`.
- **Verify the file you produced** before uploading it (sample rate, channels, bit depth, duration).
- Compute chunk boundaries arithmetically.

**Avoid**
- Omitting `-sample_fmt s16`. ffmpeg then writes 24-bit FLAC, which is about 78% larger for no benefit.
- Trusting `ffprobe` for stream-copied segments. It reports the source duration for each, so every
  chunk appears as long as the whole file.
- Lossy intermediates. Heavy mp3 compression changed roughly one word in seven.
- Letting FLAC cut offsets float. Some offsets are rejected with "invalid block size" unless the
  frame size is pinned.
- Text files in the local code page on Windows. Read and write cached responses as UTF-8.

## Chunking

**Do**
- Keep each file **under 20 minutes**. That is the cap when word timestamps are enabled. Chunks of
  about 18 minutes leave room for boundaries to move.
- Detect silences (for example `silencedetect=noise=-30dB:d=0.35`) and move each boundary up to
  about 40 seconds to land in one. Keep the longest chunk comfortably under the cap even after moving.
- Send **one file per request** with inline results. Batches accept at most five files, and inline
  output is only available for a single file.

**Avoid**
- Cutting on a fixed clock. It was behind silent truncation, words stamped outside their chunk,
  overlapping cues, and long repeated loops. Moving cuts into silence removed all of them in testing.
- Assuming shorter chunks are more accurate. Word end times were equally accurate from 45 seconds up
  to 18 minutes.
- Rebuilding a whole degraded chunk from short pieces. It lost or garbled several percent of words.
  Use short pieces to fill holes, not to replace a chunk.

## Sending and waiting

**Do**
- Fetch several chunks **concurrently** (four worked well) so one stuck chunk does not block the rest.
- Measure waiting time **from when Google received the operation**, not from when you queued it. With
  more chunks than workers, queue time looks exactly like a stall.
- Warn after about 3 minutes with no result and give up after about 5. Healthy answers arrive in
  roughly a tenth of the audio's length (an 18 minute chunk typically comes back in two to three
  minutes).
- When you give up, **re-cut** the span smaller (halves), down to a floor of about 45 seconds. Below
  the floor, report an honest gap. Do not keep re-cutting forever and do not invent text.
- Record each submitted operation to disk immediately, so a crash or Ctrl+C resumes it instead of
  paying twice.
- Make sure a stop signal reaches worker threads, not only the main thread.

**Avoid**
- Waiting more than a few minutes on an operation that has not moved. Some operations never answer at
  all. In testing the same span stalled again on resubmission, while smaller re-cut halves finished
  in seconds.
- Resending an operation that may already have started. Once the request is out, Google may be
  running it, and a second submission is billed too. Resend only when the request cannot have
  arrived (name did not resolve, connection refused) or on a rate-limit or server error.

## What can come back wrong, and how to recognise it

Each of these was seen on real audio. Several report success.

### Silent truncation
A chunk stops partway (for example 6 to 7 minutes into 18) and the job still says it succeeded.
- **Spot it:** the last word ends far before the chunk does.
- **Do:** compare coverage per chunk, re-cut the missing tail and resubmit. A tail can itself be
  troubled, so allow it to re-cut again, with a depth cap.

### Impossible word timings
A small share of words (under 1% in testing) carry timestamps outside their own chunk, one by
thousands of seconds. A single such word corrupts a whole timeline and can make total speech time negative.
- **Spot it:** any word whose offset is outside its chunk.
- **Do:** range-check every word against its chunk. Keep the word, rebuild its time from its end
  offset. Word **ends** are accurate (median error about a tenth of a second) while **starts** are
  often just the previous word's end. Word order was reliable even where the numbers were not.

### Hallucinated loops
The model gets stuck and repeats a phrase dozens of times.
- **Do:** re-cut once; then collapse any phrase repeated three or more times in a row to two. This
  contains loops but does not remove them, so review them.
- **Watch for:** short words repeated many times at near-zero-length cues. Some are real stammers.
  Confirm against a fresh short re-check before deleting.

### Internal holes with no warning
Minutes of real speech can be missing while word counts, loop counts and timing checks all look
clean.
- **Spot it:** compare words per minute chunk by chunk. A chunk far below the others (for example 36
  against 69 to 111) is suspect. Also watch for a note that a stretch was "recognized again N seconds
  later where the subtitle has no words".
- **Do:** re-send every word gap of 15 seconds or more in short pieces (about 45 seconds with a
  few seconds of overlap), including the start and end of the episode, capped at about 60% of the
  recording's length. Insert only words that are really missing.
- **Watch for:** the slow-chunk warning can be a false alarm caused by silence or music. Count words
  per window in a fresh re-check before concluding speech is missing.

### Replayed speech
An earlier passage is replayed, squeezed into a few seconds, usually at the end of a chunk, and
sometimes followed by genuinely new content. It can include stray characters from another script.
- **Spot it:** the same text appears again with exactly matching per-word offsets, earlier text
  shows up seconds to a minute later, or a burst of cues has near-zero duration.
- **Do:** treat the copy as a hallucination. Delete it, then retime the genuine content after it from
  a fresh re-check of that span. Do not trust the first copy word for word either, because it can be
  garbled too.
- **Watch for:** a replay at the splice where a truncated chunk was re-sent. The copy sits at the end
  of the first part, just before the cut, and in a repeat scan every hit shows the same time offset
  (here about 8 minutes). What is genuinely said where the copy sits is a different scene: read that
  span fresh, in two overlapping windows (two reads that agree are strong evidence), and insert it.

### Displaced blocks
A whole run of words is placed minutes away from where it was spoken (blocks of dozens of words, off
by 100 to 300 seconds).
- **Spot it:** a block whose position in a fresh re-check is far from its position in the subtitle.
- **Do:** move the block using the re-check's own timing, not by applying a "+N seconds" correction.

### Fake speech in music and noise
Music, chanting and silence can produce confident but invented text, including cues in the wrong
script. Sighs and laughs get transcribed as words.
- **Do:** treat cues over music as suspect, and a fresh piece that finds nothing there as the answer.
  The exception is singing. A fresh read can miss lyrics sung over music, so check whether the same
  lines come back later in the recording (a theme song does) before deleting them.
- **Do:** drop a word that sits inside detected silence and looks like a duplicate.

### Leaked model internals
Fragments of the model's own bracket tags (for example the tail of a music marker) can appear as if
spoken.
- **Do:** reject a word that still carries a stray bracket. Do not reject ordinary words that happen
  to spell the same thing.

### Head-of-piece artifacts
A word at the very start of a short piece can come back with no start time.
- **Do:** take its time from its neighbours, not from the piece.

### Weak recovered text
Text from the gap-filling pass is the least reliable. It produced wrong proper names (a familiar
character name replaced by a different, more common word) and wrong whole phrases.
- **Do:** review recovered text first, and check every proper name against its use in the rest of the
  recording.

### Cross-piece disagreement
Chunks processed in parallel can spell the same word two ways.
- **Do:** run a consistency pass over names and spellings after assembly.

## Repairing

**Do**
- Prefer a **fresh short re-check of the suspect span** (about 100 to 240 seconds per piece) with the
  same model. It was reliable on its own and cheap (a few minutes per problem span).
- Apply the re-check consistently: words in the subtitle but not in the re-check are hallucinations,
  words in the re-check but not in the subtitle are real dialogue, and blocks far apart are displaced.
- Insert recovered words only when they are really missing. A naive merge added well over a hundred
  duplicated words in testing. Drop recovered words that line up with a nearby word, that spell an
  existing word differently, that repeat a collapsed loop, or that sit in a run of mistimed words.
- Keep each fix **minimal**. Every broader rule that was tried changed the output of recordings that
  never needed it.

**Avoid**
- Mixing sources casually. A second recognizer is useful as a diagnostic, but its words are hard to
  audit once they are in the subtitle. Prefer repairs that come from the same model's own output.
- Applying a timing correction as arithmetic ("move +38 seconds") when a re-check gives the real time.

## Building cues

- Break at sentence ends (including the Arabic question mark), at about 84 characters, at about 7
  seconds of speech, or at a pause of about 0.7 seconds.
- Do **not** split on a short pause (under about 1.5 seconds) that follows a function word such as a
  conjunction or preposition, or that sits just before a sentence finishes within two words.
- If a word would push a cue over the limits, start the next cue, and back off to the best comma or
  pause among the last few words so no multi-word cue runs longer.
- Keep about 80 ms between cues, never invert or overlap them, and hold very short cues for about a
  second.
- Let words keep their silence. Spreading text across time by character count cannot represent a
  pause and turns a mostly quiet recording into one that appears fully spoken.

## Reviewing a result

Read the last lines of a run before using the subtitle:

- **VERIFY**: the word arithmetic should add up (Google's words, minus looped repeats, minus tail
  hand-overs, plus recovered words equals the subtitle's words). A mismatch is a bug, not an audio
  problem.
- **RECOVERY**: how many gaps were checked and how many words were inserted, dropped or skipped.
- **TIMING**: every place a timing repair moved words by more than about 5 seconds. Check those by ear.
- **CHECKS**: `pass`, `warn` or `fail`. A `fail` (a broken cue, or words covering under about 20% of
  the audio) means do not use the file until you have checked it.

Then, in the notes file, in this order:
1. Anything about the **whole recording** (broken cues, too little speech).
2. Any "recognized again N seconds later" item. Treat it as a large hole.
3. Stretches with no speech found. Over about 45 seconds is usually music, but listen to a few.
4. Short recovered words. These are the likeliest noise.
5. Slow chunks. Compare fresh word counts before acting.

Finish with these checks:
- A **full-recording repeat scan** (for example six-word sequences that repeat with a gap over about
  20 seconds). After targeted fixes it flagged only real repeats such as songs, choruses and chants.
- A structural check for overlapping or out-of-order cues.
- Verify every cue number you mention against the final file before you write it into notes. Cue
  numbers move when you delete or add cues.

## Cost and time

- Dynamic batching is about $0.003 per minute against $0.016 per minute for standard, roughly 81%
  cheaper, in exchange for slower turnaround. Leave it on by default.
- Expect about 8 times real time end to end.
- Billed audio is usually 7 to 50% more than the recording's length, because recovery pieces and
  re-cuts are billed too. Music-heavy material costs the most.
- A rerun that reuses saved responses makes no network call and bills nothing, so keep the cache.
- Raising the gap threshold spends less on recovery. Gaps under about 20 seconds mostly returned
  interjections.

## Habits that prevent most problems

- Validate saved responses (span, language, model, size) before reusing them. A response made for
  another language or span must be set aside, not reused.
- Report what the run did, in numbers, at the end of every run. A run that cannot explain itself
  cannot be trusted.
- Write the subtitle as soon as recognition completes. Recovery, checks and notes are extras, and
  their failure must not lose the result.
- Never write a partial or empty file as if it were complete.
- Add a regression test for each new defect, built from the observed values.
- Re-check where a duration is measured from before trusting a warning that relies on it.
- Clean up uploaded audio at the end of every run, and list upload locations before uploading so
  they are removed even after a kill.
