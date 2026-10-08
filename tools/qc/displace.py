#!/usr/bin/env python3
"""Cues whose matched words sit at a different time from the fresh read (displaced words).

usage: displace.py SRT FRESH.json [threshold_seconds=2.5]

Many hits are false: a one-word coincidence, or a head-of-piece artifact (a piece's first words are placed at
the piece start). Confirm a displacement only if it shows in a read AWAY from its piece edges.
"""
import difflib, json, os, statistics, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cues import parse_srt, fmt
from compare import srt_words, fresh_words

if len(sys.argv) < 3:
    sys.exit(__doc__)
cues = parse_srt(sys.argv[1])
with open(sys.argv[2], encoding="utf-8") as f:
    fresh = json.load(f)
thr = float(sys.argv[3]) if len(sys.argv) > 3 else 2.5
hits = {}
for key, words in fresh.items():
    t0, t1 = [float(x) for x in key.split("-")]
    a, b = srt_words(cues, t0, t1), fresh_words(words, t0, t1)
    sm = difflib.SequenceMatcher(None, [x["k"] for x in a], [x["k"] for x in b], autojunk=False)
    per = {}
    for tag, i0, i1, j0, j1 in sm.get_opcodes():
        if tag != "equal":
            continue
        for k in range(i1 - i0):
            per.setdefault(a[i0 + k]["cue"], []).append((a[i0 + k]["t"] - b[j0 + k]["t"], b[j0 + k]["t"]))
    for cue, dts in per.items():
        m = statistics.median(d for d, _ in dts)
        # distance of the matched words from the piece edges, to tell artifacts from real displacement
        edge = min(min(t - t0, t1 - t) for _, t in dts)
        if abs(m) > thr:
            hits.setdefault(cue, []).append((round(m, 1), len(dts), key, round(edge, 1)))
cmap = {c["n"]: c for c in cues}
print("cues whose matched words are more than %.1f s from the fresh read: %d (edge = seconds from the piece edge)" % (thr, len(hits)))
for cue in sorted(hits):
    c = cmap[cue]
    v = hits[cue]
    print("%5d %s  dt %s  | %s" % (cue, fmt(c["s"])[:11], ", ".join("%+.1f(%dw, edge %.0fs)" % (m, n, e) for m, n, _, e in v), c["text"][:70]))
