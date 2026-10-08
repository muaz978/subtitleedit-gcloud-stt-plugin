#!/usr/bin/env python3
"""Three-way vote: the SRT against two independent fresh reads (A and B, cut at different places).

usage: consensus.py SRT FRESH_A.json FRESH_B.json OUT.json

A fresh piece is trusted only away from its own edges (EDGE seconds), because the first and last words of a piece
can be lost, invented or placed wrongly. A proposal exists when BOTH reads disagree with the SRT in the same
place and agree with each other. A region the SRT has but neither read supports is 'delete'/'unsupported', and
speech both reads have that the SRT lacks is 'insert'. Nothing is applied here: this only lists candidates.
"""
import difflib, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cues import parse_srt, fmt
from compare import norm, fresh_words

EDGE = 6.0
MIN_SIM = 0.80


def srt_tokens(cues):
    toks = []
    for c in cues:
        raw = c["text"].split()
        for k, w in enumerate(raw):
            if norm(w):
                toks.append({"w": w, "k": norm(w), "t": c["s"] + (c["e"] - c["s"]) * (k + 0.5) / len(raw), "cue": c["n"]})
    return toks


def region_ops(toks, fresh, total):
    keys = sorted(fresh, key=lambda k: float(k.split("-")[0]))
    out = []
    for key in keys:
        a, b = [float(x) for x in key.split("-")]
        lo = a if a <= 0.5 else a + EDGE
        hi = b if b >= total - 0.5 else b - EDGE
        if hi - lo < 20:
            continue
        sidx = [i for i, t in enumerate(toks) if lo <= t["t"] <= hi]
        fw = [w for w in fresh_words(fresh[key], a, b) if lo <= w["t"] <= hi]
        sk = [toks[i]["k"] for i in sidx]
        sm = difflib.SequenceMatcher(None, sk, [w["k"] for w in fw], autojunk=False)
        for tag, i0, i1, j0, j1 in sm.get_opcodes():
            if tag == "equal":
                continue
            if i0 < 3 and lo > 1 or i1 > len(sidx) - 3 and hi < total - 1 or j0 < 3 and lo > 1 or j1 > len(fw) - 3 and hi < total - 1:
                continue
            if i1 > i0:
                g0, g1 = sidx[i0], sidx[i1 - 1] + 1
            else:
                g0 = g1 = sidx[i0] if i0 < len(sidx) else (sidx[-1] + 1 if sidx else 0)
            out.append({"piece": key, "kind": {"delete": "delete", "insert": "insert", "replace": "replace"}[tag],
                        "g0": g0, "g1": g1, "fresh": [w["w"] for w in fw[j0:j1]],
                        "fresh_t": [round(w["t"], 2) for w in fw[j0:j1]],
                        "fresh_span": [round(fw[j0]["t"], 2), round(fw[j1 - 1]["te"] or fw[j1 - 1]["t"], 2)] if j1 > j0 else None})
    return out


def sim(x, y):
    return difflib.SequenceMatcher(None, " ".join(norm(w) for w in x), " ".join(norm(w) for w in y), autojunk=False).ratio()


def overlaps(p, q, slack=1):
    return p["g0"] - slack <= q["g1"] and q["g0"] - slack <= p["g1"]


if __name__ == "__main__":
    if len(sys.argv) < 5:
        sys.exit(__doc__)
    srt_path, pa, pb, out = sys.argv[1:5]
    cues = parse_srt(srt_path)
    toks = srt_tokens(cues)
    with open(pa, encoding="utf-8") as f:
        fa = json.load(f)
    with open(pb, encoding="utf-8") as f:
        fb = json.load(f)
    total = max(float(k.split("-")[1]) for k in list(fa) + list(fb))
    A, B = region_ops(toks, fa, total), region_ops(toks, fb, total)
    proposals, used_b, split = [], set(), []

    def note_split(p, q):
        """The two reads disagree with the subtitle and with each other (or one is silent where the other adds words)."""
        g0s, g1s = min(p["g0"], q["g0"]), max(p["g1"], q["g1"])
        split.append({"t": round(toks[min(g0s, len(toks) - 1)]["t"], 1), "cues": sorted({toks[i]["cue"] for i in range(g0s, min(g1s, len(toks)))}),
                      "srt": " ".join(t["w"] for t in toks[g0s:g1s]), "a": " ".join(p["fresh"]), "b": " ".join(q["fresh"])})

    for p in A:
        for qi, q in enumerate(B):
            if qi in used_b or not overlaps(p, q):
                continue
            kinds = {p["kind"], q["kind"]}
            if p["kind"] == "delete" and q["kind"] == "delete":
                kind, same = "delete", True
            elif p["kind"] == "insert" and q["kind"] == "insert":
                kind, same = "insert", sim(p["fresh"], q["fresh"]) >= MIN_SIM
            elif kinds <= {"replace", "insert"} and p["fresh"] and q["fresh"]:
                kind, same = "replace", sim(p["fresh"], q["fresh"]) >= MIN_SIM
            elif kinds <= {"delete", "replace"}:
                kind, same = "unsupported", True
            else:
                note_split(p, q)                   # one read deletes the subtitle's words, the other adds some
                continue
            if not same:
                note_split(p, q)                   # both reads differ from the subtitle, and from each other
                continue
            used_b.add(qi)
            g0, g1 = min(p["g0"], q["g0"]), max(p["g1"], q["g1"])
            proposals.append({"kind": kind, "g0": g0, "g1": g1,
                              "cues": sorted({toks[i]["cue"] for i in range(g0, min(g1, len(toks)))}) or [toks[min(g0, len(toks) - 1)]["cue"]],
                              "srt": " ".join(t["w"] for t in toks[g0:g1]),
                              "a": " ".join(p["fresh"]), "b": " ".join(q["fresh"]),
                              "t": round(toks[g0]["t"] if g0 < len(toks) else toks[-1]["t"], 1),
                              "a_span": p.get("fresh_span"), "b_span": q.get("fresh_span")})
            if g0 == g1:
                prev_cue = toks[g0 - 1]["cue"] if g0 > 0 else 0
                if 0 < g0 < len(toks) and toks[g0]["cue"] == prev_cue:
                    proposals[-1]["inside_cue"] = prev_cue       # words missing inside a cue: an edit of its text
                else:
                    proposals[-1]["after_cue"] = prev_cue        # speech missing between cues: a new cue (0 = before the first)
            break
    only_a = [p for p in A if not any(overlaps(p, q) for q in B)]
    only_b = [q for q in B if not any(overlaps(q, p) for p in A)]
    proposals.sort(key=lambda x: x["t"])
    for n, p in enumerate(proposals, 1):
        p["id"] = n
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"proposals": proposals, "only_a": len(only_a), "only_b": len(only_b), "split": split}, f, ensure_ascii=False, indent=1)
    from collections import Counter
    print("A ops", len(A), "B ops", len(B), "| consensus proposals", len(proposals), dict(Counter(p["kind"] for p in proposals)),
          "| only A disagrees", len(only_a), "| only B disagrees", len(only_b),
          "| reads split (listed under 'split')", len(split))
