#!/usr/bin/env python3
"""Write evidence files for the review step.

usage: make_evidence.py SRT FRESH_A.json FRESH_B.json CONSENSUS.json OUTDIR [SPECIALS.json]

OUTDIR/batch_NN.md    one per group of about 20 proposals, no cue shared between two batches
OUTDIR/special_*.md   hand-picked trouble spots (SPECIALS.json) plus an automatic one for cues under 0.3 s
OUTDIR/index.json     the file list for the reviewers

SPECIALS.json: [{"key": "head", "title": "...", "task": "what to settle",
                 "windows": [[t0, t1, first_cue, last_cue]]}            # a stretch of the episode
                or {"key": "names", "title": "...", "task": "...", "cues": [12, 340]}]   # cues with padding around each
"""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cues import parse_srt, fmt
from compare import fresh_words, norm

if len(sys.argv) < 6:
    sys.exit(__doc__)
srt_path, pa, pb, cons_path, outdir = sys.argv[1:6]
spec_path = sys.argv[6] if len(sys.argv) > 6 else None
EDGE = 6.0
cues = parse_srt(srt_path)
cmap = {c["n"]: c for c in cues}
def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


A, B = load(pa), load(pb)
TOTAL = max(float(k.split("-")[1]) for k in list(A) + list(B))
os.makedirs(outdir, exist_ok=True)


def lines(fresh, t0, t1):
    """Fresh words in [t0, t1], edge words dropped, duplicates from overlapping pieces merged, grouped at pauses."""
    seen, words = set(), []
    for key in sorted(fresh, key=lambda k: float(k.split("-")[0])):
        a, b = [float(x) for x in key.split("-")]
        if b < t0 - 1 or a > t1 + 1:
            continue
        for w in fresh_words(fresh[key], a, b):
            if not (t0 <= w["t"] <= t1):
                continue
            if a > 0.5 and w["t"] < a + EDGE or b < TOTAL - 0.5 and w["t"] > b - EDGE:
                continue
            sig = (norm(w["w"]), round(w["t"] / 0.4))
            if sig in seen:
                continue
            seen.add(sig)
            words.append(w)
    words.sort(key=lambda w: w["t"])
    out, cur, last = [], [], None
    for w in words:
        if cur and w["t"] - last > 0.6:
            out.append(cur); cur = []
        cur.append(w); last = w["te"] or w["t"]
    if cur:
        out.append(cur)
    return ["%s to %s  %s" % (fmt(l[0]["t"])[:11], fmt(l[-1]["te"] or l[-1]["t"])[:11], " ".join(x["w"] for x in l)) for l in out] or ["(no words from this read in this range; the read may have a hole, or the edge filter removed them)"]


def cue_block(a, b):
    return "\n".join("%5d %s --> %s | %s" % (c["n"], fmt(c["s"])[:12], fmt(c["e"])[:12], c["text"]) for c in cues if a <= c["n"] <= b)


def section(title, t0, t1, ca, cb):
    return ("## %s\nTime range %s to %s.\n\n### Current subtitle cues (exact text, this is what an edit's `old` must match)\n```\n%s\n```\n\n"
            "### Fresh read A (independent Chirp read, cut at other places)\n```\n%s\n```\n\n### Fresh read B (another independent Chirp read)\n```\n%s\n```\n"
            % (title, fmt(t0)[:11], fmt(t1)[:11], cue_block(ca, cb), "\n".join(lines(A, t0, t1)), "\n".join(lines(B, t0, t1))))


P = load(cons_path)["proposals"]
batches, cur = [], []
for p in P:
    if cur and (len(cur) >= 20 and p["cues"][0] > max(x["cues"][-1] for x in cur) + 2):
        batches.append(cur); cur = []
    cur.append(p)
if cur:
    batches.append(cur)
index = {"batches": [], "specials": []}
for n, b in enumerate(batches, 1):
    ca, cb = max(1, min(p["cues"][0] for p in b) - 3), min(len(cues), max(p["cues"][-1] for p in b) + 3)
    t0, t1 = cmap[ca]["s"] - 4, cmap[cb]["e"] + 4
    txt = "# Batch %02d: proposals %d to %d\n\n" % (n, b[0]["id"], b[-1]["id"])
    txt += section("Context", t0, t1, ca, cb)
    txt += "\n## Proposals (both fresh reads disagree with the subtitle in the same place and agree with each other)\n\n"
    for p in b:
        where = ""
        if p["kind"] == "insert" and "after_cue" in p:
            where = " (insert after cue %d)" % p["after_cue"]
        elif p["kind"] == "insert" and "inside_cue" in p:
            where = " (words missing inside cue %d: change that cue's text, do not add a cue)" % p["inside_cue"]
        when = lambda span: " (%s to %s)" % (fmt(span[0])[:11], fmt(span[1])[:11]) if span else ""
        txt += "- #%d [%s] time %s, cue(s) %s%s\n    subtitle words: `%s`\n    read A says:   `%s`%s\n    read B says:   `%s`%s\n" % (
            p["id"], p["kind"], fmt(p["t"])[:11], ", ".join(str(x) for x in p["cues"]), where, p["srt"],
            p["a"], when(p.get("a_span")), p["b"], when(p.get("b_span")))
    path = os.path.join(outdir, "batch_%02d.md" % n)
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    index["batches"].append({"n": n, "file": os.path.abspath(path), "ids": [p["id"] for p in b], "cues": [ca, cb]})


def multi(title, nums, pad_t=9, pad_c=2):
    txt = ""
    done = []
    for n in nums:
        c = cmap[n]
        if any(a <= n <= b for a, b in done):
            continue
        ca, cb = max(1, n - pad_c), min(len(cues), n + pad_c)
        done.append((ca, cb))
        txt += section("Around cue %d" % n, c["s"] - pad_t, c["e"] + pad_t, ca, cb) + "\n"
    return txt


specials = load(spec_path) if spec_path else []
nz = [c["n"] for c in cues if c["e"] - c["s"] < 0.30]
if nz:
    specials.append({"key": "nearzero", "title": "Cues shorter than 0.30 s",
                     "task": "Each of these cues lasts under 0.3 s, which cannot be read. Delete a cue whose words neither read hears; keep one whose words both reads hear (it is real speech squeezed by tight timing, there is usually no room to lengthen it) and say so in a flag.",
                     "cues": nz})
for s in specials:
    txt = "# %s\n\nTask: %s\n\n" % (s["title"], s["task"])
    if "windows" in s:
        for (t0, t1, ca, cb) in s["windows"]:
            txt += section(s["title"], t0, t1, ca, cb)
    else:
        txt += multi(s["title"], s["cues"])
    path = os.path.join(outdir, "special_%s.md" % s["key"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    index["specials"].append({"key": s["key"], "file": os.path.abspath(path), "title": s["title"]})
with open(os.path.join(outdir, "index.json"), "w", encoding="utf-8") as f:
    json.dump(index, f, indent=1)
print("batches", len(index["batches"]), [len(b["ids"]) for b in index["batches"]], "| specials", [s["key"] for s in index["specials"]])
