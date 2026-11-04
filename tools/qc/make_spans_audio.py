#!/usr/bin/env python3
"""Cut the episode into fresh-read pieces at silences found in the AUDIO, so the reads can start before the first pass is done.

usage: make_spans_audio.py FULL.flac TOTAL_SECONDS A|B OUT.json [A_SPANS.json]

Same pieces as make_spans.py (A: about 150 s from 0; B: every cut shifted by 75 s and kept at least 30 s from any cut of A,
pass A's file as the 5th argument), but the cut points come from the sound itself (ffmpeg silencedetect, -35 dB, 0.4 s) and
not from the pauses of the first pass subtitle. The tool's own work folder holds full.flac a few seconds after the first pass
starts (~/.cache/se-stt/<video name>/full.flac), so this needs nothing from the first pass: run it at once, send the reads
with recheck.py, and let them run beside the first pass. Set SE_STT_FFMPEG to use another ffmpeg.
"""
import json, os, re, subprocess, sys

if len(sys.argv) < 5:
    sys.exit(__doc__)
audio, total, mode, out = sys.argv[1], float(sys.argv[2]), sys.argv[3].upper(), sys.argv[4]
a_path = sys.argv[5] if len(sys.argv) > 5 else None
if mode not in ("A", "B"):
    sys.exit("mode must be A or B")
if mode == "B" and not a_path:
    sys.exit("mode B needs the span file written by mode A as the fifth argument")
if not os.path.isfile(audio):
    sys.exit("no audio file at %s" % audio)


def silences(path):
    p = subprocess.run([os.environ.get("SE_STT_FFMPEG") or "ffmpeg", "-nostdin", "-hide_banner", "-i", path,
                        "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"], capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        sys.exit("ffmpeg failed on %s:\n%s" % (path, p.stderr[-500:]))
    gaps, start = [], None
    for line in p.stderr.splitlines():
        m = re.search(r"silence_start: (-?[0-9.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end: ([0-9.]+)", line)
        if m and start is not None:
            gaps.append((start, float(m.group(1))))
            start = None
    return gaps


gaps = silences(audio)
if not gaps:
    sys.exit("no silence found in %s: cannot choose cut points (use make_spans.py with the first pass instead)" % audio)


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
print("%s pieces: %d, mean %.1f s, min %.1f, max %.1f (%d silences in the audio)" % (
    mode, len(spans), sum(y - x for x, y in spans) / len(spans), min(y - x for x, y in spans), max(y - x for x, y in spans), len(gaps)))
