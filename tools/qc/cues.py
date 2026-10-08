#!/usr/bin/env python3
"""Scan an SRT for the problems the transcription tool does not flag by itself.

usage: cues.py FILE.srt [scan]      all checks (default)
       cues.py FILE.srt show A B    print cues A..B (numbers in this file)
       cues.py FILE.srt at T0 T1    print cues overlapping seconds T0..T1
"""
import re, sys, unicodedata, collections

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")     # subtitle text is printed; do not depend on the console code page


def ts(t):
    h, m, rest = t.split(":")
    s, ms = rest.replace(",", ".").split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000.0


def fmt(t):
    ms = int(round(max(0.0, t) * 1000))              # whole milliseconds first, so 59.9996 cannot print as 60 s
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d.%03d" % (h, m, s, ms)


def parse_srt(path):
    with open(path, encoding="utf-8-sig") as f:        # a byte order mark must not break the first cue number
        raw = f.read().replace("\r\n", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = block.split("\n")
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        a, b = [x.strip() for x in lines[1].split("-->")]
        cues.append({"n": int(lines[0]), "s": ts(a), "e": ts(b), "text": " ".join(lines[2:]).strip()})
    return cues


def tokens(text):
    return [w for w in re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)?", text.lower())]


def scan(cues):
    print("cues", len(cues), "words", sum(len(tokens(c["text"])) for c in cues), "minutes", round(cues[-1]["e"] / 60, 1))
    bad = 0
    for i, c in enumerate(cues):
        if c["e"] <= c["s"]:
            bad += 1
            print("INVERTED", c["n"], fmt(c["s"]), fmt(c["e"]), c["text"][:60])
        if i and c["s"] < cues[i - 1]["e"] - 0.001:
            bad += 1
            print("OVERLAP", cues[i - 1]["n"], c["n"], fmt(c["s"]))
    print("structure problems:", bad)

    print("\n--- cues under 0.30 s")
    for c in cues:
        if c["e"] - c["s"] < 0.30:
            print(c["n"], fmt(c["s"]), round(c["e"] - c["s"], 2), c["text"][:70])

    print("\n--- cues with letters outside Latin")
    for c in cues:
        odd = {ch for ch in c["text"] if ch.isalpha() and "LATIN" not in unicodedata.name(ch, "")}
        if odd:
            print(c["n"], fmt(c["s"]), "".join(sorted(odd)), c["text"][:70])

    print("\n--- runs of the same word in 4+ consecutive cues")
    i = 0
    while i < len(cues):
        w = tokens(cues[i]["text"])
        j = i
        while j + 1 < len(cues) and tokens(cues[j + 1]["text"]) == w and len(w) <= 3:
            j += 1
        if j - i >= 3:
            print(cues[i]["n"], "to", cues[j]["n"], fmt(cues[i]["s"]), cues[i]["text"][:50], "x", j - i + 1)
        i = j + 1

    print("\n--- gaps of 30 s or more between cues")
    for a, b in zip(cues, cues[1:]):
        if b["s"] - a["e"] >= 30:
            print("after cue", a["n"], fmt(a["e"]), "gap", round(b["s"] - a["e"], 1), "s")

    repeat_scan(cues)


def repeat_scan(cues, n=6, mingap=20.0):
    """6-gram repeats more than 20 s apart. A constant offset between hits suggests a replay, but a show can repeat a clip itself."""
    grams = collections.defaultdict(list)
    words = []
    for c in cues:
        toks = tokens(c["text"])
        for k, w in enumerate(toks):
            t = c["s"] + (c["e"] - c["s"]) * (k / max(1, len(toks)))
            words.append((w, t, c["n"]))
    for i in range(len(words) - n + 1):
        grams[tuple(w[0] for w in words[i:i + n])].append((words[i][1], words[i][2]))
    hits = []
    for g, occ in grams.items():
        for a in range(len(occ)):
            for b in range(a + 1, len(occ)):
                if occ[b][0] - occ[a][0] > mingap:
                    hits.append((occ[a][0], occ[b][0], occ[a][1], occ[b][1], " ".join(g)))
    hits.sort()
    print("\n--- repeated 6-grams (gap over %.0f s): %d" % (mingap, len(hits)))
    off = collections.Counter(round(b - a) for a, b, _, _, _ in hits)
    print("most common offsets (seconds : hits):", off.most_common(6))
    for a, b, na, nb, g in hits[:80]:
        print("%s (cue %d)  ->  %s (cue %d)  off %.0f  %s" % (fmt(a), na, fmt(b), nb, b - a, g))
    if len(hits) > 80:
        print("... %d more" % (len(hits) - 80))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "scan"
    cues = parse_srt(path)
    if mode not in ("scan", "show", "at") or (mode != "scan" and len(sys.argv) < 5):
        sys.exit(__doc__)
    if mode == "scan":
        scan(cues)
    elif mode == "show":
        a, b = int(sys.argv[3]), int(sys.argv[4])
        for c in cues:
            if a <= c["n"] <= b:
                print(c["n"], fmt(c["s"]), "-->", fmt(c["e"]), "|", c["text"])
    elif mode == "at":
        t0, t1 = float(sys.argv[3]), float(sys.argv[4])
        for c in cues:
            if c["e"] >= t0 and c["s"] <= t1:
                print(c["n"], fmt(c["s"]), "-->", fmt(c["e"]), "|", c["text"])
