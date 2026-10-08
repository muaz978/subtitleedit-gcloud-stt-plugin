# Checking a finished episode with fresh re-reads

Helper scripts for section 5 of [docs/episode-workflow.md](../../docs/episode-workflow.md): finding and
fixing what `transcribe-episode.py` cannot see, by reading the audio again in short pieces with the
same model and comparing the answers with the subtitle.

Run any script by its path from anywhere, for example `python3 tools/qc/cues.py episode.srt`, and keep the
episode's own files (subtitle, fresh reads, patches) in a folder of your own, never in the repository. Running
a script with no arguments prints its usage.

## Needs

- Python 3.9 or newer and the standard library only.
- `recheck.py` is the only script that talks to Google and the only one that costs money (cents per piece, about
  1 US dollar for two reads of a whole episode). It loads `tools/transcribe-episode.py` (or the file named by
  `SE_STT_TOOL`), so it needs that tool's settings, `ffmpeg`, `ffprobe` and `gcloud`, and it reads with the same
  language and model as the original run. Everything else works on files.
- Subtitles are read as UTF-8 (a byte order mark is fine). Cues are expected in the tool's own format, one
  line of text per cue; a cue with two lines is joined into one when a file is rewritten.

## File formats

- **Spans** (`make_spans.py` writes, `recheck.py` reads): `[[start, end], ...]`, absolute seconds in the episode.
- **Fresh reads** (`recheck.py` writes, the rest read): `{"START-END": [[start, end, word], ...], ...}`, absolute
  seconds. A word at the very start of a piece can have `null` for its start or end.
- **accepted.json** for `patch.py`: see below.

## What each script does

| Script | What it does |
|:--|:--|
| `cues.py` | Structure and pattern scan of a subtitle (inverted or overlapping cues, cues under 0.3 s, stray scripts, runs of the same short phrase (up to three words) in four or more cues, long gaps, repeated six-word sequences). `show A B` prints cues A to B, `at T0 T1` prints the cues in a time range |
| `make_spans.py` | Cuts the episode into pieces of about 150 s at the widest gaps of the subtitle itself. Mode A starts at 0, mode B shifts every cut by 75 s and keeps it at least 30 s from a cut of A |
| `recheck.py` | Sends the pieces to the model through the episode tool's own `fetch_pieces` and writes the fresh words with absolute times |
| `compare.py` | Per piece: how well the subtitle agrees with a fresh read, and the differences ("srt only", "fresh only", "swap") |
| `displace.py` | Cues whose words sit at another time than in a fresh read, with the distance from the piece edge (an edge hit is usually an artifact) |
| `fview.py` | A time range as spoken lines from every fresh piece, next to the subtitle's cues |
| `consensus.py` | The vote: lists a proposal only where both reads disagree with the subtitle in the same place and agree with each other away from piece edges. The kinds are `replace`, `insert` (with the cue it goes after, or the cue it belongs inside), `delete` (both reads silent) and `unsupported` (one read silent, the other hears different words). Places where the reads disagree with each other are not proposals, they are listed under `split` |
| `make_evidence.py` | Writes evidence files for reviewers (batches of about 20 proposals plus hand-picked trouble spots) |
| `review_rules.md` | The written rules to give each reviewer (and each skeptic, with a lens added) |
| `retime.py` | Retimes a displaced or squeezed block of cues from the fresh reads' own word times |
| `patch.py` | Applies reviewed edits with the old text checked, and writes the new subtitle, a change log and a map from original to final cue numbers |

## Order of work

1. Run the episode tool. Keep its raw subtitle as the "before" copy.
2. `python3 tools/qc/cues.py EPISODE.srt` for the scan.
3. `make_spans.py EPISODE.srt TOTAL_SECONDS A spans_a.json`, then `recheck.py WORK spans_a.json fresh_a.json`,
   where `WORK` is the tool's work folder: `~/.cache/se-stt/<video name>`, the video's file name with every
   character that is not a letter or digit replaced by `-`, cut to 60 characters (`ls ~/.cache/se-stt` shows
   it; it holds `full.flac`). If some pieces failed or are still pending, `recheck.py` saves the rest, writes
   `fresh_a.json.missing.json` and exits with code 2. Run it again with that file as the spans: finished
   pieces are cached and cost nothing, pending ones resume.
4. See where the subtitle and read A disagree:
   - `compare.py EPISODE.srt fresh_a.json compare_a.json` prints one line per piece with its agreement and, in the
     last column, any difference of four words or more away from the edges; every difference is in `compare_a.json`.
   - `displace.py EPISODE.srt fresh_a.json [2.5]` lists cues more than 2.5 s (or your threshold) from where
     the read puts the same words.
   - `fview.py fresh_a.json EPISODE.srt T0 T1` shows one stretch (note: the fresh file comes first).
5. Same again for read B: `make_spans.py EPISODE.srt TOTAL_SECONDS B spans_b.json spans_a.json`, then `recheck.py`.
6. `consensus.py EPISODE.srt fresh_a.json fresh_b.json consensus.json` for the proposals, then
   `make_evidence.py EPISODE.srt fresh_a.json fresh_b.json consensus.json evidence/` for the reviewers. An
   insert proposal says after which cue it goes, and every read line shows its start and end.
7. Review the evidence with the rules in `review_rules.md` (one reviewer per batch is enough when a team
   is waiting; add skeptics when quality matters more than speed). Give each reviewer the show's names list (term sheets) with the evidence file, because `make_evidence.py`
   does not include one. Each reviewer returns the edits it accepts (the output shape is at the end of
   `review_rules.md`; when you merge them into `accepted.json` use the keys shown below, a reviewer's `reason`
   becomes `why`). When skeptics split two to one, read the dissent yourself: in one episode it was right in 6
   of 7 cases.
8. For a block that sits at the wrong time,
   `retime.py EPISODE.srt FIRST_CUE LAST_CUE T0 T1 retime.json fresh_a.json fresh_b.json` gives every cue the
   start and end of its own words. Its output is a bare list of edits.
9. Put the accepted edits into one `accepted.json` and run
   `patch.py EPISODE.srt accepted.json final.srt changes.tsv [cue_map.json]`. Run `cues.py final.srt` again.
10. Write the notes from the cue map, not from the old numbers.

`accepted.json`:

```json
{"edits":   [{"cue": 12, "old": "exact current text", "new": "new text, or empty to delete the cue",
              "new_start": null, "new_end": null, "why": "short reason"}],
 "inserts": [{"after_cue": 40, "start": 123.4, "end": 125.0, "text": "a missing line", "why": "short reason"}]}
```

- Cue numbers are the numbers in the file you give to `patch.py`. A cue may appear in one edit only: when it needs
  both a retime and a wording change, merge them into one edit (the new text plus `new_start` and `new_end`),
  also when one of them came from `retime.py`.
- Either key may be missing, and a bare list (the output of `retime.py`) is read as a list of edits. Give both
  `new_start` and `new_end` or neither.
- A failed check (the old text differs, a cue would be inverted or overlap) stops everything and writes
  nothing. The checks also hold under `python -O`.
- For a subtitle in the tool's own format an empty patch reproduces the input exactly.
- `changes.tsv` lists every change with old and new text, old and new start and end, and your `why`. The cue map
  goes where you say, or beside the output as `final.cue_map.json`.

## Things to know

- A fresh piece is only trusted about 6 seconds away from its edges. The first and last words of a piece can
  be lost, invented or placed wrongly. `consensus.py`, `retime.py` and `make_evidence.py` already ignore them.
- One read is one hypothesis. Two reads that agree are evidence, except for archaic or dialect speech, where
  both reads normalise old forms the same way (see `review_rules.md`, rules 10 and 11).
- The scripts are written for Turkish in Latin script. For another language change three things: the
  folding table and `norm()` in `compare.py`, the non-Latin check in `cues.py` (it flags every non-Latin
  letter as a stray script), and the language examples in `review_rules.md`.
- `make_evidence.py` also writes one special batch (`special_nearzero.md`) that lists every cue shorter than
  0.3 s, and takes an optional `SPECIALS.json` of hand-picked stretches (see its docstring).
- Whisper or another recognizer is a diagnostic. Nothing here puts its wording into a subtitle.

## Not here

The notes generator (write the notes from the cue map) and the program that runs the reviewers: a reviewer is
whoever or whatever reads one evidence file with `review_rules.md`, so a person or an assistant can do it. A
built-in vote inside the episode tool is still a gap, see section 12 of the workflow.
