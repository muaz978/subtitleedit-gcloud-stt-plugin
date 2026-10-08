#!/usr/bin/env python3
"""Cut the episode into fresh-read pieces at the widest gaps of the subtitle itself.

usage: make_spans.py SRT TOTAL_SECONDS A|B OUT.json [A_SPANS.json]

A: pieces of about 150 s starting at 0.
B: every cut shifted by 75 s, kept at least 30 s from any cut of A (pass A's span file as the 5th argument).
The subtitle's own pauses are real silences, so a cut in the widest pause near the target point rarely splits a word.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cues import parse_srt

if len(sys.argv) < 5:
    sys.exit(__doc__)
srt, total, mode, out = sys.argv[1], float(sys.argv[2]), sys.argv[3].upper(), sys.argv[4]
a_path = sys.argv[5] if len(sys.argv) > 5 else None
if mode not in ("A", "B"):
    sys.exit("mode must be A or B")
if mode == "B" and not a_path:
    sys.exit("mode B needs the span file written by mode A as the fifth argument")
cues = parse_srt(srt)
gaps = [(a["e"], b["s"]) for a, b in zip(cues, cues[1:]) if b["s"] - a["e"] >= 0.5]


def snap(t, reach=25.0):
    near = [(g1 - g0, (g0 + g1) / 2) for g0, g1 in gaps if abs((g0 + g1) / 2 - t) <= reach]
    return round(max(near)[1], 2) if near else t


a_bounds = set()
if mode == "B":
    with open(a_path, encoding="utf-8") as f:
        A = json.load(f)
    a_bounds = {round(x[0], 1) for x in A} | {round(x[1], 1) for x in A}
bounds = [0.0]
t = 150.0 if mode == "A" else 75.0
while t < total - (60 if mode == "A" else 40):
    b = snap(t)
    if mode == "B" and min(abs(b - x) for x in a_bounds) < 30:
        b = t
    bounds.append(b)
    t += 150.0
bounds.append(total)
spans = [[bounds[i], bounds[i + 1]] for i in range(len(bounds) - 1)]
with open(out, "w", encoding="utf-8") as f:
    json.dump(spans, f)
print("%s pieces: %d, mean %.1f s, min %.1f, max %.1f" % (mode, len(spans), sum(y - x for x, y in spans) / len(spans),
                                                       min(y - x for x, y in spans), max(y - x for x, y in spans)))
