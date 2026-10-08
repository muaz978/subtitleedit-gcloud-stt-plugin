#!/usr/bin/env python3
"""Retime a run of cues from fresh reads' own word times.

usage: retime.py SRT CUE_FIRST CUE_LAST T0 T1 OUT.json FRESH_A.json [FRESH_B.json ...]

For a block of cues that sits at the wrong time (a displaced or squeezed block), find where the same words are in
the fresh reads inside [T0, T1] (seconds) and give every cue the start and end of its own words. Words are matched
in order on a folded form; fresh words within 6 s of a piece edge are ignored. A cue with at least half of its words
matched is 'ok'. A cue with no matched word is placed between its matched neighbours, and a cue with fewer than half
matched keeps the times of the words that did match; both are marked 'interp' for review.

OUT.json is a list of edits for patch.py: {"cue", "old", "new" (= old text), "new_start", "new_end", "why"}.
Cue lengths: end = last word end + 0.25 s, at least 0.6 s when there is room, never later than 0.08 s before the
next cue, and never longer than the number of words allows.
"""
import difflib, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cues import parse_srt, fmt
from compare import norm, fresh_words

EDGE = 6.0
if len(sys.argv) < 8:
    sys.exit(__doc__)
srt, ca, cb, t0, t1, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5]), sys.argv[6]
fresh_files = sys.argv[7:]
cues = parse_srt(srt)
block = [c for c in cues if ca <= c["n"] <= cb]
if not block:
    sys.exit("STOP: no cues numbered %d to %d in %s" % (ca, cb, srt))

# fresh stream from every read, edge words dropped, duplicates (the same word at about the same time in two pieces) merged
total = 0.0
reads = []
for p in fresh_files:
    with open(p, encoding="utf-8") as f:
        reads.append(json.load(f))
for r in reads:
    total = max(total, max(float(k.split("-")[1]) for k in r))
words = []
for ri, r in enumerate(reads):
    for key, ws in r.items():
        a, b = [float(x) for x in key.split("-")]
        if b < t0 or a > t1:
            continue
        for w in fresh_words(ws, a, b):
            if not (t0 <= w["t"] <= t1):
                continue
            if a > 0.5 and w["t"] < a + EDGE or b < total - 0.5 and w["t"] > b - EDGE:
                continue
            w["p"] = (ri, key)             # which piece of which read the word came from
            words.append(w)
words.sort(key=lambda w: w["t"])
merged = []
for w in words:
    if merged and merged[-1]["k"] == w["k"] and abs(merged[-1]["t"] - w["t"]) < 0.5 and merged[-1]["p"] != w["p"]:
        continue                           # the same word heard by two pieces; a repeat inside one piece is real
    merged.append(w)
fresh = merged
print("fresh words in range:", len(fresh))

stoks = []
for c in block:
    for w in c["text"].split():
        if norm(w):
            stoks.append((c["n"], norm(w)))
sm = difflib.SequenceMatcher(None, [k for _, k in stoks], [w["k"] for w in fresh], autojunk=False)
hit = {c["n"]: [] for c in block}
for tag, i0, i1, j0, j1 in sm.get_opcodes():
    if tag == "equal":
        for k in range(i1 - i0):
            hit[stoks[i0 + k][0]].append(fresh[j0 + k])

res = []
for c in block:
    h = hit[c["n"]]
    n_words = len([w for w in c["text"].split() if norm(w)])
    status = "ok" if n_words and len(h) * 2 >= n_words else "interp"
    s = h[0]["t"] if h else None
    e = (h[-1]["te"] or h[-1]["t"] + 0.3) if h else None
    res.append({"c": c, "s": s, "e": e, "status": status, "matched": len(h), "words": n_words})

# interpolate cues that have no times of their own between the nearest timed neighbours
for i, r in enumerate(res):
    if r["s"] is None or r["status"] == "interp" and r["matched"] == 0:
        lo = next((x["e"] for x in reversed(res[:i]) if x["e"] is not None), t0)
        hi = next((x["s"] for x in res[i + 1:] if x["s"] is not None), t1)
        gap = [x for x in res if x["s"] is None]
        r["s"], r["e"], r["status"] = None, None, "interp"
        r["lo"], r["hi"] = lo, hi
# second pass: spread untimed cues evenly inside their window
i = 0
while i < len(res):
    if res[i]["s"] is None:
        j = i
        while j < len(res) and res[j]["s"] is None:
            j += 1
        lo = next((x["e"] for x in reversed(res[:i]) if x["e"] is not None), t0)
        hi = next((x["s"] for x in res[j:] if x["s"] is not None), t1)
        span = max(0.4, hi - lo) / (j - i)
        for k in range(i, j):
            res[k]["s"] = lo + span * (k - i) + 0.04
            res[k]["e"] = lo + span * (k - i + 1) - 0.04
        i = j
    else:
        i += 1

edits = []
print("%5s %-12s -> %-12s %-6s %5s  %s" % ("cue", "old start", "new start", "status", "match", "text"))
for k, r in enumerate(res):
    c = r["c"]
    nxt = res[k + 1]["s"] if k + 1 < len(res) else None
    s = max(r["s"], res[k - 1]["new_e"] + 0.08) if k and "new_e" in res[k - 1] else r["s"]
    e = (r["e"] or s + 0.5) + 0.25
    if nxt is not None:
        e = min(e, nxt - 0.08)
    if e - s < 0.6 and (nxt is None or nxt - 0.08 - s >= 0.6):
        e = s + 0.6
    e = max(e, s + 0.1)
    # never run into a cue outside the block that now starts later than this one, and keep the length sensible
    # for the number of words (a garbage word end can otherwise stretch a cue over several seconds)
    later = [o["s"] for o in cues if not (ca <= o["n"] <= cb) and o["s"] > s + 0.001]
    if later:
        e = min(e, min(later) - 0.08)
    e = min(e, s + max(1.2, 0.5 * r["words"] + 1.0))
    e = max(e, s + 0.1)
    r["new_s"], r["new_e"] = round(s, 3), round(e, 3)
    print("%5d %-12s -> %-12s %-6s %2d/%-2d  %s" % (c["n"], fmt(c["s"])[:12], fmt(r["new_s"])[:12], r["status"], r["matched"], r["words"], c["text"][:60]))
    edits.append({"cue": c["n"], "old": c["text"], "new": c["text"], "new_start": r["new_s"], "new_end": r["new_e"],
                  "why": "retimed from fresh reads (%s, %d of %d words matched)" % (r["status"], r["matched"], r["words"])})
with open(out, "w", encoding="utf-8") as f:
    json.dump(edits, f, ensure_ascii=False, indent=1)
print("written", len(edits), "retime edits;", sum(1 for r in res if r["status"] == "interp"), "interpolated")
