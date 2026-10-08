#!/usr/bin/env python3
"""Compare the SRT with fresh Chirp reads, piece by piece.

usage: compare.py SRT FRESH.json OUT.json

For every fresh piece it lists
  'srt only'   : words in the SRT that the fresh read does not have (hallucination candidates)
  'fresh only' : words in the fresh read that the SRT lacks (missing speech candidates)
  'swap'       : the two disagree on wording
and the agreement ratio of the whole piece. Differences at the very edge of a piece are marked 'edge'
because a piece cut can lose or invent a word there. NOTE: this matches words in order and ignores
time; use displace.py for words in the right order but at the wrong time.
"""
import difflib, json, os, re, sys, unicodedata
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cues import parse_srt, fmt

FOLD = str.maketrans({"ç": "c", "ğ": "g", "ı": "i", "İ": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"})


def norm(w):
    w = unicodedata.normalize("NFC", w).replace("İ", "i").replace("I", "ı").lower()
    w = re.sub(r"[^\w']+", "", w, flags=re.UNICODE).replace("'", "").replace("’", "")
    return w.translate(FOLD)


def srt_words(cues, t0, t1):
    out = []
    for c in cues:
        if c["e"] < t0 - 0.5 or c["s"] > t1 + 0.5:
            continue
        raw = c["text"].split()
        n = len(raw)
        for k, w in enumerate(raw):
            t = c["s"] + (c["e"] - c["s"]) * (k + 0.5) / max(1, n)
            if t0 - 0.5 <= t <= t1 + 0.5 and norm(w):
                out.append({"w": w, "k": norm(w), "t": t, "cue": c["n"]})
    return out


def fresh_words(words, t0, t1):
    ws = [list(w) for w in words]
    # a head-of-piece word can come back with no start: take the time of the next timed neighbour
    for i, w in enumerate(ws):
        if w[0] is None:
            nxt = next((x[0] for x in ws[i + 1:] if x[0] is not None), None)
            w[0] = nxt if nxt is not None else t0
            w[1] = w[1] if w[1] is not None else w[0]
    return [{"w": w[2], "k": norm(w[2]), "t": w[0], "te": w[1]} for w in ws if norm(w[2])]


def compare(cues, key, words):
    t0, t1 = [float(x) for x in key.split("-")]
    a = srt_words(cues, t0, t1)
    b = fresh_words(words, t0, t1)
    sm = difflib.SequenceMatcher(None, [x["k"] for x in a], [x["k"] for x in b], autojunk=False)
    ops, matched = [], 0
    for tag, i0, i1, j0, j1 in sm.get_opcodes():
        if tag == "equal":
            matched += i1 - i0
            continue
        sa, sb = a[i0:i1], b[j0:j1]
        edge = (i0 < 3 or j0 < 3 or i1 > len(a) - 3 or j1 > len(b) - 3)
        ops.append({
            "tag": {"delete": "srt only", "insert": "fresh only", "replace": "swap"}[tag],
            "edge": edge, "n_srt": len(sa), "n_fresh": len(sb),
            "srt_cues": [sa[0]["cue"], sa[-1]["cue"]] if sa else None,
            "t": round((sa[0]["t"] if sa else sb[0]["t"]), 2), "t_end": round((sa[-1]["t"] if sa else sb[-1]["t"]), 2),
            "srt": " ".join(x["w"] for x in sa), "fresh": " ".join(x["w"] for x in sb),
        })
    return {"key": key, "t0": t0, "t1": t1, "n_srt": len(a), "n_fresh": len(b),
            "agree": round(matched / max(1, max(len(a), len(b))), 3), "ops": ops}


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    srt, fresh_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
    cues = parse_srt(srt)
    with open(fresh_path, encoding="utf-8") as f:
        fresh = json.load(f)
    res = [compare(cues, k, v) for k, v in sorted(fresh.items(), key=lambda kv: float(kv[0].split("-")[0]))]
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("pieces", len(res))
    print("%-22s %6s %6s %6s  %s" % ("piece", "srt", "fresh", "agree", "big ops (>=4 words, not edge)"))
    for r in res:
        big = [o for o in r["ops"] if max(o["n_srt"], o["n_fresh"]) >= 4 and not o["edge"]]
        print("%-22s %6d %6d %6.2f  %s" % (fmt(r["t0"])[:8] + "-" + fmt(r["t1"])[:8], r["n_srt"], r["n_fresh"], r["agree"],
                                           ", ".join("%s %d/%d" % (o["tag"], o["n_srt"], o["n_fresh"]) for o in big)))
