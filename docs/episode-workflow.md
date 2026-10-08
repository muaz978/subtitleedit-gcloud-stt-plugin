# How we run an episode

[lessons.md](lessons.md) says what to expect from the model. This file says what we do, step by
step, so that anyone, or a fresh session on another device, can repeat it. It describes the way of
working only. It names no recording, and nothing from a real episode (subtitle, notes, titles,
paths) belongs in git.

## 1. The request

"Work on this ep" followed by the path to a video means: run the episode profile, which is
Turkish drama, `tr-TR`, `chirp_3`, through `tools/transcribe-episode.py`. We deliver two files:

- the corrected subtitle (SRT)
- a notes file that says what was fixed, what was checked and left alone, and what to listen to

The deliverable is the corrected subtitle, not the raw output of the tool.

The steps, in order:

1. Run the tool in the background and watch its log.
2. Read what it flagged.
3. Re-check each flagged stretch with a fresh short request.
4. Patch the subtitle safely.
5. Run the final checks.
6. Write the notes.
7. Deliver the two files.

## 2. First time on a device

- Python 3.9 or newer, `ffmpeg`, `ffprobe` and the Google Cloud CLI. See
  [tools/README.md](../tools/README.md) for the one-time setup and the settings file.
- The settings values (cloud project, bucket, account) come from whoever administers the cloud
  project. They are never committed. Register the service account key with
  `gcloud auth activate-service-account`, and keep the key file out of git, chat and screenshots.
- Optional second recognizer: Whisper large-v3 (`mlx_whisper` on Apple silicon). Used only as a
  diagnostic, see section 7.
- The tool keeps its work files in `~/.cache/se-stt/<video name>` (responses, `report.json`, the
  extracted audio). **That folder lives on one device and is not synced.** The saved responses are
  what make a rerun free. If an episode is started on one device and continued on another, copy that
  folder across, or finish on the device that started it. Otherwise the second device sends all the
  audio again and pays again.
- Keep one folder per episode for the output subtitle, the notes, the run log and a `diag/` folder
  of scratch scripts. It is not committed.
- Keep the scratch scripts (the ones you write for the steps of section 5) in one folder of their
  own as well, not only inside an episode folder. An episode folder can be moved or cleared when the
  episode is done, and the scripts go with it.

## 3. Run it

```bash
python3 tools/transcribe-episode.py "<video>" "<output>.srt" > run.log 2>&1 &
```

On Windows the command is `python`. Watch the log for these lines:

```
WARNING|Traceback|Stalled|ANOMALY|re-cut|waiting for|VERIFY|RECOVERY|TIMING|CHECKS|billed|wrote
```

A slow-chunk warning, a stall after 5 minutes, an anomaly with a re-cut and a truncated chunk whose
tail is recovered are all normal. The tool handles them itself. Only a traceback or a `fail` check
needs action. A 2.5 hour episode takes about 15 to 40 minutes, the recovery stage being the last and
longest part, and costs about 0.5 to 0.8 US dollars. A rerun that reuses the saved responses is
free.

## 4. Read what it flagged

Read the notes file, then `report.json` in the work folder. `report.json` is more complete than the
notes: the notes list only timing moves over 5 seconds, while `timing_events` in `report.json` has
every one, including small ones.

What each flag usually means, and what we do:

| Flag | What it usually is | What we do |
|:--|:--|:--|
| Words at a chunk start moved by a large shift | Often correct, for example the first line after opening titles or music. Sometimes a second or two off. | Compare with a fresh re-check. Retime only if it is off by more than about half a second. |
| The log says a chunk was truncated and its tail re-sent | The splice can hold a replay of a scene from elsewhere. | Read a fresh window on each side of the cut time, and look for one constant offset in the repeat scan. |
| Many words moved, or a block "past the end of its chunk" | A replayed copy of earlier dialogue squeezed into a few seconds, or a displaced block. | Section 5. Delete the copy, keep the first real pass, retime the genuine lines at the join. |
| One word moved by a small amount | Noise. | Leave it. |
| Recovered stretch ("N words recovered") | The weakest text in the file: wrong words and wrong proper names. | Re-check first. Compare every name with its use in the rest of the episode. |
| Short recovered words | Often noise, grunts or music vocals. | Verify there is speech. Delete if a fresh read finds nothing. |
| Words timed inside silence | A second or so misplaced, or genuine quiet singing or humming. | Check the audio level and a fresh read before deleting anything. |
| "Recognized again N seconds later where the subtitle has no words" | A large hole. Minutes of real speech may be missing. | Re-check the whole span, and the stretch where the words reappear. |
| No speech found, 45 seconds or more | Music. The recovery pass already looked. | Leave it unless something suggests speech. |
| Slow chunk | Silence or music, or sometimes a hole. | Compare fresh word counts per 150 second window with the subtitle. |

Also scan the subtitle itself for patterns the tool does not flag:

- runs of the same short word or phrase at cues of almost zero duration (some are a real stammer,
  count the repeats in a fresh read before deleting)
- cues that repeat text from a few seconds to a minute earlier
- cues in the wrong script (stray characters from another alphabet)

## 5. Check by fresh re-check

A fresh request for a short span with the same model is reliable on its own, cheap (cents) and quick.
It is the main tool for finding and fixing problems.

1. Collect the suspect spans from section 4. Add some context on each side.
2. Cut them into pieces of 60 to 250 seconds. Start a piece in silence when you can.
3. Send all the pieces in one call. They run concurrently, usually in two to three minutes.

```python
import importlib.util, json, os
spec = importlib.util.spec_from_file_location("te", "tools/transcribe-episode.py")
te = importlib.util.module_from_spec(spec); spec.loader.exec_module(te)
te.load_config()
work = os.path.expanduser("~/.cache/se-stt/<video name>")
ep = te.Episode("dummy.mp4", "x.srt", work)
ep.full = work + "/full.flac"
ep.ffmpeg = te._tool("SE_STT_FFMPEG", "ffmpeg")
ep.ffprobe = te._tool("SE_STT_FFPROBE", "ffprobe")
pieces = [(4250.0, 4350.0, None, None), (8630.0, 8780.0, None, None)]   # start, end in seconds
try:
    got = ep.fetch_pieces(pieces)
finally:
    ep.cleanup()
for s, e, _, _ in pieces:
    r = got[te.piece_tag(s, e)]
    if not isinstance(r, dict):   # None: the piece failed. "pending": Google has not answered it yet
        print(s, e, "MISSING", r)
        continue
    words = te.words_from_raw(r)   # [start, end, word], relative to the piece
    print(s, e, " ".join(f"[{(s + w[0]) if w[0] is not None else None}]{w[2]}" for w in words))
```

A piece that failed comes back as `None`, and one that Google has not finished as `"pending"`;
`words_from_raw` cannot read either, so the snippet skips them and says so. Finished pieces are
cached, so running only the missing pieces again costs nothing for the rest, and a pending piece
resumes.

4. Compare each span with the subtitle and apply three rules:
   - words in the subtitle but not in the fresh read are hallucinations: delete them
   - words in the fresh read but not in the subtitle are real dialogue: insert them
   - a block whose time in the fresh read is far from its time in the subtitle is displaced: move it
     using the fresh read's own times, never a "+N seconds" correction
5. Before deleting anything over music, check the audio level. Quiet singing is real.

Watch for these traps in a fresh read:

- A word at the very start of a piece may come back with no start time, or at the wrong place. Take
  its time from its neighbours.
- The first copy of a replayed passage is not automatically right. Compare its wording with the
  fresh read too.
- A name in a fresh read can be a mishearing of a name the episode uses consistently. Keep the
  episode's own name.
- Judge a fresh read only away from its piece edges (about 6 seconds). The first words of a piece can
  be placed at the piece start, so a "displaced" result at an edge, or in only one of two cuts, is
  an artifact.
- A fresh read can have a hole of its own, where the subtitle is right. Before deleting words the
  fresh read lacks, read the stretch again with the pieces cut elsewhere.

### A second full re-read and a vote (when quality matters more than speed)

The tool's checks can pass while many cues still have a wrong word, because one long recognition
words some sentences worse than a short fresh read. One fresh read is only one hypothesis. Two reads
that agree with each other against the subtitle are strong evidence. When there is time (about an
hour and about 1 US dollar more per episode), do this:

1. **Read A.** Cut the whole recording into pieces of about 150 seconds, at the widest gaps of the
   subtitle itself (its pauses are real silences), and send them all in one call as in the snippet
   above. Add a few overlapping pieces around anything suspicious.
2. **Read B.** Do it again with every cut shifted by half a piece, at least 30 seconds from any cut of
   read A, so every word is read in a different context.
3. **List proposals.** Align the subtitle with each read inside each piece, ignoring the 6 seconds at
   each edge. Propose an edit only where both reads differ from the subtitle in the same place and
   agree with each other. A typical episode gives 100 to 250 proposals, many of them harmless
   variants.
4. **Review them.** One reviewer per group of about 20 proposals, each with the evidence (the exact
   cue text, both reads' lines around it, the show's names) and written rules: both reads must agree,
   the new wording must read better in the scene, ignore variants, take wording only from the reads,
   delete only where both reads are silent and the words are implausible, flag what cannot be
   settled; for archaic or dialect speech add the dialect rule of
   [lessons.md](lessons.md#wording-that-differs-between-reads), or the reviewers will correct real
   speech into modern standard forms. Then three skeptics per reviewer, each with a different lens
   (natural language, the evidence, the structure of the subtitle), try to refute every edit. Keep
   an edit only when at least two of the three agree, and read the dissent of every split vote
   yourself (in the episode below it was right in 6 of the 7 split votes).
5. **Apply** with the method of section 6, and run all the final checks of section 9 again.

In one episode where the tool's checks gave only a warning, this changed 116 of 2,624 cues (about 100 were
wording fixes, the rest a displaced first word, a duplicated stretch, three unsupported flash cues and
a stray description), and it left about 100 places for a listener. Use the checks of section 8
instead when a team is waiting.

## 6. Patch the subtitle safely

1. Keep a copy of the raw output (`pipeline-output.srt`) before the first edit.
2. Load the cues into a dictionary by number.
3. Before each edit, assert the old text. If the same text appears more than once, assert the time
   as well. This catches the common mistake of editing the wrong cue after numbers have shifted.
4. Delete, change or insert. A new cue can take a fractional key, because it is renumbered at the
   end.
5. Sort by start time. A new cue gets at least one second on screen, but never overlaps the next
   cue. Leave the pipeline's own cues as they were.
6. Check that no cue is inverted or overlaps, then write the file with fresh numbers.
7. Look cue numbers up again in the written file before quoting them anywhere.

Change wording only when the fresh read is clearly better and comes from the same model. Do not
import wording from a second recognizer. When a reading is uncertain, keep the more plausible one
and flag it in the notes.

## 7. The second recognizer

Use it for an independent opinion on a disputed span, to confirm a hole, or to tell music from
speech. Do not run it over a whole episode unless asked and there is time (a 25 minute piece takes
about 4 to 5 minutes). Its wording never goes into the subtitle. Known traps:

- It can fall into a repetition loop that spoils the rest of a clip. Run it with
  `--condition-on-previous-text False`.
- It writes "thanks for watching" style text over music.
- Fuzzy matching of short common phrases gives false "displaced" results. Trust a match only for
  cues of four or more words with about 75 percent of the words matched in order.

## 8. When a team is waiting

By default, skip the full sweeps (the two whole-recording reads of section 5 and the second
recognizer of section 7). Check only what the tool flagged, the biggest events first, and
the slow chunk. This usually takes 10 to 15 minutes after the run. Say in the notes what was not
checked.

If the wording matters too, there is a middle path of about 40 minutes after the run. It keeps the
two whole-recording reads of section 5 (so it still costs about the extra US dollar) and drops the
skeptic round. Do Read A and Read B and list the proposals (section 5, steps 1 to 3), repair the
large timing defects first, then give the proposals, with the evidence and the written rules of the
review step (including the dialect rule), to one reviewer per group of about 20, all in parallel
(about 5 minutes). Skip the skeptic round and the second recognizer of section 7. In one episode
this delivered the corrected subtitle about 40 minutes after the run, with three large timing
defects repaired and 53 wording fixes. Say in the notes that there was no skeptic round and no
second recognizer, and list the places the reads could not settle.

## 9. Final checks

- No cue is inverted, out of order or overlapping. Record the final cue and word counts.
- A full-file repeat scan: six-word sequences that repeat with a gap of more than about 20 seconds.
  After the targeted fixes, expect only real repeats such as songs, choruses, chants and a line
  echoed on purpose. A run of hits that all share the same time offset is a replay,
  not a repeat. Look at the cue pacing of each hit before dismissing it.
- Every cue number and time quoted in the notes matches the final file.
- The notes contain no em dashes or en dashes.

## 10. The notes file

Plain language, for someone who will listen to the flagged places. In this order:

1. A title line: cues, words, minutes, and what was done (and which checks were used).
2. **Fixes applied**, biggest first. For each: where (time and cue numbers in the final file), what
   was wrong, what we did, and how it was verified.
3. **Checked, no fix needed**: what was looked at and left alone, and why.
4. **Please check by ear**: uncertain readings, overlapping speech, music stretches that were not
   re-listened to.
5. **Numbers**: final and original cue and word counts, cues removed and added, chunks re-cut.

Be exact about what was not checked. Do not describe a stretch as clean unless it was checked.

## 11. Delivery and keeping devices in step

- Deliver the subtitle and the notes together.
- To hand an episode to another device, send those two files, plus the work folder if the episode
  is not finished (section 2). Nothing from an episode goes into git: no subtitle, notes, title,
  path or name.
- When an episode teaches something general, add it to [lessons.md](lessons.md) as a rule, with no
  recording details. When the way of working changes, update this file. That is what keeps every
  device working the same way, without sending handoffs.

## 12. Gaps in the tool

Nothing below is detected or repaired by the tool yet. Each would be a small, tested change built
from observed values (see the habits at the end of [lessons.md](lessons.md)):

- a built-in second full re-read with a vote and a patch helper (the steps are in section 5, the
  scripts that did it are not in this repository yet)
- a replayed passage (earlier text again, with matching per-word offsets, squeezed to near zero
  duration), and telling it from a clip the show itself repeats
- the true position of a displaced block
- runs of the same short word at near-zero duration
- cues in a stray script
- a proper name that differs from its use in the rest of the episode
