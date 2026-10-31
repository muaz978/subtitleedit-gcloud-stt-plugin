#!/usr/bin/env python3
"""Fresh short Chirp re-check of spans of the episode.

usage: recheck.py WORKDIR SPANS.json OUT.json [SPANS2.json OUT2.json ...]
  WORKDIR     the tool's work folder, ~/.cache/se-stt/<video name>, which holds full.flac
  SPANS.json  [[start_s, end_s], ...]   absolute seconds in the episode
  OUT.json    {"<start>-<end>": [[abs_start, abs_end, word], ...], ...}
Give every read (A and B) in one call: all their pieces are sent and waited for together, so two reads
take as long as one. A piece Google is slow with gets a second attempt beside the first (see the tool),
and one that is still unanswered when the attempts are used up is sent again from scratch. The run
keeps at it until every piece is back or SE_STT_READ_DEADLINE seconds (default 2400) have passed; then
it stops with exit code 2 and the missing list, and delivers nothing by itself.

Offsets that come back from Google are relative to the piece. A word at the very start of a piece may
come back with no start time: those are kept as None, take the time from the neighbours.
"""
import importlib.util, json, os, sys, time

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")   # a path with Turkish letters must not end the run

TOOL = os.environ.get("SE_STT_TOOL") or os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "transcribe-episode.py")
if len(sys.argv) < 4 or len(sys.argv) % 2 != 0:
    sys.exit(__doc__)
work = sys.argv[1]
jobs = [(sys.argv[i], sys.argv[i + 1]) for i in range(2, len(sys.argv), 2)]
out_path = jobs[0][1]
if not os.path.isfile(TOOL):
    sys.exit("cannot find the episode tool at %s (set SE_STT_TOOL to its path)" % TOOL)
if not os.path.isfile(os.path.join(work, "full.flac")):
    sys.exit("%s has no full.flac: give the tool's work folder (see ls ~/.cache/se-stt)" % work)
spec = importlib.util.spec_from_file_location("te", TOOL)
te = importlib.util.module_from_spec(spec)
spec.loader.exec_module(te)
te.load_config()
ep = te.Episode("dummy.mp4", os.path.join(os.path.dirname(os.path.abspath(out_path)), "x.srt"), work)
ep.full = work + "/full.flac"
ep.ffmpeg = te._tool("SE_STT_FFMPEG", "ffmpeg")
ep.ffprobe = te._tool("SE_STT_FFPROBE", "ffprobe")
jobs_spans = []
for spans_path, job_out in jobs:
    with open(spans_path, encoding="utf-8") as f:
        jobs_spans.append([(float(a), float(b)) for a, b in json.load(f)])
pieces = []
for spans in jobs_spans:
    pieces += [(a, b, None, None) for a, b in spans]
deadline = time.time() + float(os.environ.get("SE_STT_READ_DEADLINE", "") or 2400.0)
got, round_no = {}, 0
try:
    while True:
        round_no += 1
        todo = [p for p in pieces if not isinstance(got.get(te.piece_tag(p[0], p[1])), dict)]
        if not todo:
            break
        round_start = time.time()
        got.update(ep.fetch_pieces(todo))
        left = [p for p in todo if not isinstance(got.get(te.piece_tag(p[0], p[1])), dict)]
        if not left or time.time() >= deadline or time.time() - round_start < 5.0:
            break       # done, out of time, or a round that did not wait for anything (Google unreachable)
        # Pieces still unanswered after every second attempt has had its time: cancel them and start over.
        for s, e, _, _ in left if hasattr(te, "MAX_HEDGES") else []:
            tag = te.piece_tag(s, e)
            path = "%s/rec/%s.op.json" % (ep.raw, tag)
            rec = te.read_json(path, {}) if os.path.exists(path) else {}
            hedges = rec.get("hedges", []) if isinstance(rec, dict) else []
            if isinstance(rec, dict) and rec.get("name") and len(hedges) >= te.MAX_HEDGES and \
                    time.time() - float(rec.get("submitted", time.time())) > te.REC_TIMEOUT:
                ep.cancel([rec["name"]] + hedges)
                ep.drop_op(path)
finally:
    ep.cleanup()
rc = 0
for (spans_path, job_out), spans in zip(jobs, jobs_spans):
    out = {}
    if os.path.exists(job_out):           # a rerun adds to what an earlier run saved
        with open(job_out, encoding="utf-8") as f:
            out = json.load(f)
    missing = []
    for s, e in spans:
        r = got[te.piece_tag(s, e)]
        if not isinstance(r, dict):
            # fetch_pieces gives None for a piece that failed and "pending" for one Google is still working on
            missing.append([s, e, "pending" if r == "pending" else "failed"])
            continue
        words = te.words_from_raw(r)
        out["%g-%g" % (s, e)] = [[(s + w[0]) if w[0] is not None else None,
                                  (s + w[1]) if w[1] is not None else None, w[2]] for w in words]
    with open(job_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    miss_path = job_out + ".missing.json"
    if not missing and os.path.exists(miss_path):
        os.remove(miss_path)                # a finished run leaves no stale list of missing pieces
    print("pieces saved", len(out), "words", sum(len(v) for v in out.values()))
    if missing:
        with open(miss_path, "w", encoding="utf-8") as f:
            json.dump([[a, b] for a, b, _ in missing], f)
        print("MISSING %d piece(s), run again with %s.missing.json as the spans (finished pieces are cached, pending ones resume):" % (len(missing), job_out))
        for a, b, why in missing:
            print("  %g-%g %s" % (a, b, why))
        rc = 2
sys.exit(rc)
