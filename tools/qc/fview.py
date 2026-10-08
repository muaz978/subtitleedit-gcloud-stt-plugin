#!/usr/bin/env python3
"""Show the fresh read of a time range as spoken lines (grouped at pauses), and the SRT cues beside it.

usage: fview.py FRESH.json SRT T0 T1
Words from every fresh piece that overlaps the range are shown, tagged with the piece they came from, so two
independent cuts of the same audio can be compared. Pipe through `awk '/^=== SRT/{exit}'` to see only the reads.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cues import parse_srt, fmt
from compare import fresh_words

if len(sys.argv) < 5:
    sys.exit(__doc__)
with open(sys.argv[1], encoding="utf-8") as f:
    fresh = json.load(f)
cues = parse_srt(sys.argv[2])
t0, t1 = float(sys.argv[3]), float(sys.argv[4])
for key in sorted(fresh, key=lambda k: float(k.split("-")[0])):
    a, b = [float(x) for x in key.split("-")]
    if b < t0 or a > t1:
        continue
    ws = [w for w in fresh_words(fresh[key], a, b) if t0 <= w["t"] <= t1]
    if not ws:
        print("--- piece %s (%s-%s): no words in range" % (key, fmt(a)[:8], fmt(b)[:8])); continue
    print("--- fresh piece %s-%s" % (fmt(a)[:8], fmt(b)[:8]))
    line, last = [], None
    for w in ws:
        if line and w["t"] - last > 0.6:
            print("  %s  %s" % (fmt(line[0]["t"])[:11], " ".join(x["w"] for x in line))); line = []
        line.append(w); last = w["te"] if w["te"] else w["t"]
    if line:
        print("  %s  %s" % (fmt(line[0]["t"])[:11], " ".join(x["w"] for x in line)))
print("=== SRT cues in range")
for c in cues:
    if c["e"] >= t0 and c["s"] <= t1:
        print("  %5d %s-%s  %s" % (c["n"], fmt(c["s"])[:11], fmt(c["e"])[6:11], c["text"]))
