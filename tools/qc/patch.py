#!/usr/bin/env python3
"""Apply reviewed edits to the pipeline's SRT, safely.

usage: patch.py RAW.srt ACCEPTED.json OUT.srt LOG.tsv [CUE_MAP.json]

ACCEPTED.json  {"edits":   [{"cue": n, "old": "exact current text", "new": "text or empty to delete",
                             "new_start": s|null, "new_end": e|null, "why": "..."}],
                "inserts": [{"after_cue": n, "start": s, "end": e, "text": "...", "why": "..."}]}
               Either key may be missing, and a bare list is read as a list of edits (the output of retime.py).

Cue numbers are the numbers of RAW.srt, and a cue may appear in one edit only. The old text is checked before
every edit, a new cue gets a fractional key and is renumbered at the end, the result is sorted by start time,
nothing may be inverted or overlap, and the pipeline's own cues keep their times unless an edit says otherwise.
A failed check stops everything and writes nothing. For a subtitle in the tool's own format (one text line per
cue) an empty patch reproduces RAW.srt exactly. CUE_MAP.json (default: OUT.cue_map.json beside OUT.srt) maps
original numbers to final numbers: every deletion or insertion shifts the numbers, so build the notes from the map.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cues import parse_srt, fmt

if len(sys.argv) < 5:
    sys.exit(__doc__)
raw_path, acc_path, out_path, log_path = sys.argv[1:5]
map_path = sys.argv[5] if len(sys.argv) > 5 else os.path.splitext(out_path)[0] + ".cue_map.json"


def die(msg):
    sys.exit("STOP: " + msg)        # not an assert: a safety check must survive python -O


def secs(x):
    return round(float(x), 3)       # whole milliseconds, as the subtitle can hold


with open(acc_path, encoding="utf-8") as f:
    acc = json.load(f)
if isinstance(acc, list):
    acc = {"edits": acc}
edits, inserts = acc.get("edits", []), acc.get("inserts", [])
cues = {c["n"]: dict(c, orig=c["n"], text0=c["text"], s0=c["s"], e0=c["e"], how=[], why="") for c in parse_srt(raw_path)}

seen = set()
for e in edits:
    n = e["cue"]
    if n not in cues:
        die("no cue %s" % n)
    if n in seen:
        die("two edits for cue %s (a cue may appear once: merge them into one edit that carries the new text and new_start and new_end)" % n)
    seen.add(n)
    c = cues[n]
    if c["text"] != e["old"]:
        die("cue %s text differs from the reviewed old text:\n  have %r\n  want %r" % (n, c["text"], e["old"]))
    c["why"] = e.get("why") or ""
    if e["new"] == "":
        c["deleted"] = True
        c["how"].append("deleted")
        continue
    if e["new"] != c["text"]:
        c["text"] = e["new"]; c["how"].append("text")
    has_s, has_e = e.get("new_start") is not None, e.get("new_end") is not None
    if has_s != has_e:
        die("edit for cue %s gives only one of new_start and new_end" % n)
    if has_s:
        c["s"], c["e"] = secs(e["new_start"]), secs(e["new_end"]); c["how"].append("time")

alive = [c for c in cues.values() if not c.get("deleted")]
for k, i in enumerate(sorted(inserts, key=lambda i: (i["after_cue"], i["start"]))):
    key = i["after_cue"] + 0.01 * (k + 1)
    if i["after_cue"] != 0 and i["after_cue"] not in cues:        # 0 = before the first cue
        die("no cue %s to insert after" % i["after_cue"])
    if secs(i["end"]) <= secs(i["start"]):
        die("insert has end before start: %r" % i)
    alive.append({"n": key, "orig": None, "s": secs(i["start"]), "e": secs(i["end"]), "text": i["text"], "text0": "",
                  "s0": None, "e0": None, "how": ["new"], "why": i.get("why") or ""})

alive.sort(key=lambda c: (c["s"], c["e"]))
bad = []
for a, b in zip(alive, alive[1:]):
    if b["s"] < a["e"] - 0.0005:
        bad.append("overlap: cue %s (%s-%s) and cue %s (%s)" % (a["orig"] or "new", fmt(a["s"])[:12], fmt(a["e"])[6:12], b["orig"] or "new", fmt(b["s"])[:12]))
for c in alive:
    if c["e"] <= c["s"]:
        bad.append("inverted: cue %s" % (c["orig"] or "new"))
if bad:
    print("STOP, the patch would break the structure:")
    for b in bad:
        print("  ", b)
    sys.exit(1)

with open(out_path, "w", encoding="utf-8") as f:
    for k, c in enumerate(alive, 1):
        c["final"] = k
        f.write("%d\n%s --> %s\n%s\n\n" % (k, fmt(c["s"]).replace(".", ","), fmt(c["e"]).replace(".", ","), c["text"]))


def when(t):
    return fmt(t)[:12] if t is not None else ""


with open(log_path, "w", encoding="utf-8") as f:
    f.write("final_cue\toriginal_cue\tchange\told_text\tnew_text\told_start\tnew_start\told_end\tnew_end\twhy\n")
    for c in alive:
        if c["how"]:
            f.write("%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" % (c["final"], c["orig"] or "", "+".join(c["how"]), c["text0"], c["text"],
                    when(c["s0"]), when(c["s"]), when(c["e0"]), when(c["e"]), c["why"]))
    for c in cues.values():
        if c.get("deleted"):
            f.write("\t%s\tdeleted\t%s\t\t%s\t\t%s\t\t%s\n" % (c["orig"], c["text0"], when(c["s0"]), when(c["e0"]), c["why"]))
with open(map_path, "w", encoding="utf-8") as f:
    json.dump({str(c["orig"]): c["final"] for c in alive if c["orig"] is not None}, f)
print("written %d cues (was %d): %d edited, %d new, %d deleted" % (
    len(alive), len(cues), sum(1 for c in alive if c["orig"] and c["how"]), sum(1 for c in alive if not c["orig"]),
    sum(1 for c in cues.values() if c.get("deleted"))))
