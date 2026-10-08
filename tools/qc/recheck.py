#!/usr/bin/env python3
"""Fresh short Chirp re-check of spans of the episode.

usage: recheck.py WORKDIR SPANS.json OUT.json
  WORKDIR     the tool's work folder, ~/.cache/se-stt/<video name>, which holds full.flac
  SPANS.json  [[start_s, end_s], ...]   absolute seconds in the episode
  OUT.json    {"<start>-<end>": [[abs_start, abs_end, word], ...], ...}

Offsets that come back from Google are relative to the piece. A word at the very start of a piece may
come back with no start time: those are kept as None, take the time from the neighbours.
"""
import importlib.util, json, os, sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")   # a path with Turkish letters must not end the run

TOOL = os.environ.get("SE_STT_TOOL") or os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "transcribe-episode.py")
if len(sys.argv) < 4:
    sys.exit(__doc__)
work, spans_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
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
with open(spans_path, encoding="utf-8") as f:
    spans = [(float(a), float(b)) for a, b in json.load(f)]
pieces = [(a, b, None, None) for a, b in spans]
try:
    got = ep.fetch_pieces(pieces)
finally:
    ep.cleanup()
out = {}
if os.path.exists(out_path):           # a rerun adds to what an earlier run saved
    with open(out_path, encoding="utf-8") as f:
        out = json.load(f)
missing = []
for s, e, _, _ in pieces:
    r = got[te.piece_tag(s, e)]
    if not isinstance(r, dict):
        # fetch_pieces gives None for a piece that failed and "pending" for one Google is still working on
        missing.append([s, e, "pending" if r == "pending" else "failed"])
        continue
    words = te.words_from_raw(r)
    out["%g-%g" % (s, e)] = [[(s + w[0]) if w[0] is not None else None,
                              (s + w[1]) if w[1] is not None else None, w[2]] for w in words]
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)
miss_path = out_path + ".missing.json"
if not missing and os.path.exists(miss_path):
    os.remove(miss_path)                # a finished run leaves no stale list of missing pieces
print("pieces saved", len(out), "words", sum(len(v) for v in out.values()))
if missing:
    with open(miss_path, "w", encoding="utf-8") as f:
        json.dump([[a, b] for a, b, _ in missing], f)
    print("MISSING %d piece(s), run again with %s.missing.json as the spans (finished pieces are cached, pending ones resume):" % (len(missing), out_path))
    for a, b, why in missing:
        print("  %g-%g %s" % (a, b, why))
    sys.exit(2)
