#!/usr/bin/env python3
"""Transcribe an episode with Google Cloud Speech-to-Text v2 (chirp_3).

Every failure observed so far clustered within seconds of a chunk boundary: truncation,
words with no timestamps, words timestamped past the end of their chunk, and a scrambled
opening sentence. That is one weakness rather than four, so this version attacks the cause:

  - chunks are cut in SILENCE, never mid-sentence
  - a chunk that comes back anomalous is re-cut and re-transcribed, since recognition is
    deterministic and only changing the input can change the answer

and keeps the repairs that proved necessary: loop collapsing, timing repair rather than
word deletion, truncation recovery, and a verification pass.
"""
import glob, json, os, re, subprocess, sys, time, urllib.request, urllib.error

if len(sys.argv) < 2:
    raise SystemExit("usage: transcribe-episode.py <video> [output.srt] [work-dir]")

VIDEO   = sys.argv[1]
OUT_SRT = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(VIDEO)[0] + ".srt"
# Keyed to the video so re-running an episode reuses its cached responses and costs nothing,
# while two different episodes never share a work directory.
_slug   = "".join(c if c.isalnum() else "-" for c in os.path.basename(VIDEO))[:60]
WORK    = sys.argv[3] if len(sys.argv) > 3 else os.path.expanduser(f"~/.cache/se-stt/{_slug}")
# Deployment settings come from the environment, so no project, bucket or account name
# belongs to this file. Set them in your shell, or as KEY=VALUE lines in
# ~/.config/se-stt/config.env (which is read first and never overrides an existing variable).
CONFIG_FILE = os.path.expanduser("~/.config/se-stt/config.env")
if os.path.exists(CONFIG_FILE):
    for _line in open(CONFIG_FILE, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

def _need(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"{name} is not set.\n\n"
            f"Put your deployment settings in {CONFIG_FILE}, or export them:\n"
            f"  SE_STT_PROJECT=your-gcp-project-id\n"
            f"  SE_STT_BUCKET=your-gcs-bucket\n"
            f"  SE_STT_LANGUAGE=tr-TR\n"
            f"  SE_STT_SERVICE_ACCOUNT=stt@your-project.iam.gserviceaccount.com   # optional\n"
            f"  SE_STT_REGION=us                                                  # optional\n"
            f"  SE_STT_MODEL=chirp_3                                              # optional")
    return value

PROJECT = _need("SE_STT_PROJECT")
BUCKET  = _need("SE_STT_BUCKET")
LANG    = _need("SE_STT_LANGUAGE")
REGION  = os.environ.get("SE_STT_REGION", "us").strip() or "us"
MODEL   = os.environ.get("SE_STT_MODEL", "chirp_3").strip() or "chirp_3"
# Optional. When set, every gcloud call is pinned to it, so a run never depends on whichever
# account happens to be active. Register the key once with:
#   gcloud auth activate-service-account --key-file=/path/to/key.json
SA      = os.environ.get("SE_STT_SERVICE_ACCOUNT", "").strip()
ACCOUNT = [f"--account={SA}"] if SA else []
# The pid keeps two runs started in the same second from sharing an upload path and
# silently overwriting each other's audio.
PREFIX  = f"stt/{int(time.time())}-{os.getpid()}/"
CHUNK, MAX_SNAP = 1080.0, 40.0
HOST    = f"{REGION}-speech.googleapis.com"
FFMPEG, FFPROBE = "/opt/homebrew/bin/ffmpeg", "/opt/homebrew/bin/ffprobe"
MAX_WORD, MAX_REPEATS = 5.0, 2

env = dict(os.environ, CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED="False")
def sh(*a):
    """Run a command, and on failure report what it actually said rather than only its exit code."""
    proc = subprocess.run(a, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(a)}\n{(proc.stderr or proc.stdout).strip()[:800]}")
    return proc
log = lambda m: print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)
def sec(v):
    try: return float(str(v).rstrip("s"))
    except Exception: return None

def api(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + sh("gcloud","auth","print-access-token",*ACCOUNT).stdout.strip())
        if data: req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as r: return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in (429,500,502,503,504) and attempt < 4:
                time.sleep(5*(attempt+1)); continue
            raise SystemExit(f"{method} {url} -> {e.code}: {detail}")

# ---------- 1. audio ----------
os.makedirs(f"{WORK}/raw", exist_ok=True)
total = float(sh(FFPROBE,"-v","error","-show_entries","format=duration","-of","csv=p=0",VIDEO).stdout.strip())
log(f"episode {total/60:.1f} min")
full = f"{WORK}/full.flac"
if not os.path.exists(full):
    log("extracting 16 kHz mono 16-bit flac")
    sh(FFMPEG,"-y","-hide_banner","-loglevel","error","-i",VIDEO,"-vn","-ac","1","-ar","16000",
       "-c:a","flac","-sample_fmt","s16","-compression_level","8",full)

# ---------- 2. silence-aware boundaries ----------
def silences():
    """Midpoints of every detected silence, used as candidate cut points."""
    p = subprocess.run([FFMPEG,"-hide_banner","-i",full,"-af","silencedetect=noise=-30dB:d=0.35",
                        "-f","null","-"], capture_output=True, text=True, env=env)
    starts = [float(m) for m in re.findall(r"silence_start: ([0-9.]+)", p.stderr)]
    ends   = [float(m) for m in re.findall(r"silence_end: ([0-9.]+)", p.stderr)]
    return sorted((s + e) / 2 for s, e in zip(starts, ends))

log("detecting silence for chunk boundaries")
quiet = silences()
log(f"{len(quiet)} silences found")

bounds, t = [0.0], CHUNK
while t < total - 60:
    near = [q for q in quiet if abs(q - t) <= MAX_SNAP and q > bounds[-1] + 60]
    bounds.append(min(near, key=lambda q: abs(q - t)) if near else t)
    t += CHUNK
bounds.append(total)
snapped = sum(1 for i,b in enumerate(bounds[1:-1],1) if abs(b - i*CHUNK) > 0.01)
log(f"{len(bounds)-1} chunks, {snapped} boundaries moved into silence")

def cut(start, end, path):
    sh(FFMPEG,"-y","-hide_banner","-loglevel","error","-ss",f"{start:.3f}","-to",f"{end:.3f}",
       "-i",full,"-vn","-ac","1","-ar","16000","-c:a","flac","-sample_fmt","s16",path)
    return path

# ---------- 3. recognition ----------
def recognize(uri, tag):
    cached = f"{WORK}/raw/{tag}.json"
    if os.path.exists(cached):
        log(f"  {tag}: reusing saved response")
        return words_from(json.load(open(cached)))
    body = {"config":{"autoDecodingConfig":{},"languageCodes":[LANG],"model":MODEL,
            "features":{"enableWordTimeOffsets":True,"enableAutomaticPunctuation":True}},
            "files":[{"uri":uri}],"recognitionOutputConfig":{"inlineResponseConfig":{}},
            "processingStrategy":"DYNAMIC_BATCHING"}
    op = api("POST", f"https://{HOST}/v2/projects/{PROJECT}/locations/{REGION}/recognizers/_:batchRecognize", body)
    while True:
        time.sleep(10)
        st = api("GET", f"https://{HOST}/v2/{op['name']}")
        if st.get("done"):
            if "error" in st: raise SystemExit(f"recognition failed: {st['error']}")
            json.dump(st, open(f"{WORK}/raw/{tag}.json","w"), ensure_ascii=False)
            return words_from(st)

def words_from(st):
    out = []
    for _, fr in st.get("response",{}).get("results",{}).items():
        if fr.get("error",{}).get("message"): raise SystemExit(fr["error"]["message"])
        for r in fr.get("inlineResult",{}).get("transcript",{}).get("results",[]):
            for a in r.get("alternatives",[])[:1]:
                raw = a.get("words",[])
                for i,w in enumerate(raw):
                    s = sec(w["startOffset"]) if "startOffset" in w else None
                    e = sec(w["endOffset"]) if "endOffset" in w else None
                    if s is None:
                        pv = raw[i-1] if i else None
                        s = sec(pv.get("endOffset")) if pv and "endOffset" in pv else 0.0
                    if e is None:
                        nx = raw[i+1] if i+1 < len(raw) else None
                        ns = sec(nx["startOffset"]) if nx and "startOffset" in nx else None
                        e = ns if ns is not None and ns > s else s + 0.4
                    out.append([s, e, w.get("word","")])
    return out

def collapse(ws):
    out, i, removed = [], 0, 0
    while i < len(ws):
        best = None
        for L in range(1, 9):
            if i + 2*L > len(ws): break
            ph = [w[2] for w in ws[i:i+L]]; n = 1
            while i+(n+1)*L <= len(ws) and [w[2] for w in ws[i+n*L:i+(n+1)*L]] == ph: n += 1
            if n >= 3 and (best is None or n*L > best[0]*best[1]): best = (n, L)
        if best:
            n, L = best
            out.extend(ws[i:i+MAX_REPEATS*L]); removed += (n-MAX_REPEATS)*L; i += n*L
        else:
            out.append(ws[i]); i += 1
    return out, removed

def repair(ws, dur):
    ok = lambda p,s,e: 0.0 <= s <= dur+5 and s >= p-0.5 and e >= s and e-s <= MAX_WORD
    kept, last, fixed = [], 0.0, 0
    for i,(s,e,word) in enumerate(ws):
        if ok(last,s,e): kept.append((s,e,word)); last = e; continue
        nxt = next((ws[j][0] for j in range(i+1,len(ws)) if ok(last,ws[j][0],ws[j][1])), None)
        span = nxt if nxt is not None and nxt > last else min(last+0.45, dur)
        s2 = min(last+0.01, dur)
        e2 = min(max(s2+0.12, min(s2+0.45, span-0.01 if nxt else s2+0.45)), dur)
        kept.append((s2,e2,word)); last = e2; fixed += 1
    return kept, fixed

def transcribe_span(start, end, tag, depth=0):
    """Recognize one span, re-cutting it once if the result looks anomalous."""
    dur = end - start
    cut(start, end, f"{WORK}/{tag}.flac")
    sh("gcloud","storage","cp","-q",*ACCOUNT,f"{WORK}/{tag}.flac",f"gs://{BUCKET}/{PREFIX}")
    ws = recognize(f"gs://{BUCKET}/{PREFIX}{tag}.flac", tag)
    ws, looped = collapse(ws)
    kept, fixed = repair(ws, dur)
    covered = max((w[1] for w in kept), default=0.0)

    bad_ratio = fixed / max(1, len(kept))
    anomalous = looped > 40 or bad_ratio > 0.15
    if anomalous and depth == 0 and dur > 240:
        # Recognition is deterministic, so resending the same audio changes nothing.
        # Cutting somewhere else does. Split at the quietest point near the middle.
        mid_t = start + dur/2
        near = [q for q in quiet if abs(q-mid_t) <= 60 and start+60 < q < end-60]
        mid = min(near, key=lambda q: abs(q-mid_t)) if near else mid_t
        log(f"  {tag}: ANOMALY ({looped} looped, {bad_ratio*100:.0f}% timings bad), re-cutting at {mid/60:.1f} min")
        a = transcribe_span(start, mid, tag+"a", depth+1)
        b = transcribe_span(mid, end, tag+"b", depth+1)
        return a + [(s+(mid-start), e+(mid-start), w) for s,e,w in b]

    # covered > 30 means the span actually contains speech that stopped early. Without it,
    # a silent tail (end credits) resumes at 0.0, re-cuts the identical span, and loops.
    if dur > 60 and covered < dur*0.90 and dur-covered > 20 and covered > 30 and depth < 2:
        resume = max(0.0, covered-1.0)
        log(f"  {tag}: truncated at {covered:.0f}/{dur:.0f}s, recovering tail")
        tail = transcribe_span(start+resume, end, tag+"t", depth+1)
        kept = [w for w in kept if w[1] <= resume+0.5] + [(s+resume,e+resume,w) for s,e,w in tail]

    log(f"  {tag}: {len(kept)} words, {looped} looped removed, {fixed} timings repaired")
    return kept

all_words, google_total = [], 0
for i in range(len(bounds)-1):
    s, e = bounds[i], bounds[i+1]
    log(f"chunk {i+1}/{len(bounds)-1} ({s/60:.1f}-{e/60:.1f} min)")
    got = transcribe_span(s, e, f"part-{i:03d}")
    all_words.extend((w[0]+s, w[1]+s, w[2]) for w in got)

# Count only the responses that actually fed the subtitle. A chunk that was re-cut is
# SUPERSEDED by its two halves, and counting both inflates the total by the whole chunk,
# which makes the check report a loss that never happened.
_saved = {os.path.basename(p)[:-5]: p for p in glob.glob(f"{WORK}/raw/*.json")}
_used = {k: p for k, p in _saved.items() if not (k + "a" in _saved and k + "b" in _saved)}
for p in _used.values():
    google_total += len(words_from(json.load(open(p))))

# ---------- 4. cues ----------
MAX_CHARS, MAX_DUR, PAUSE = 84, 6.0, 0.7
cues, cur = [], []
for i,(s,e,w) in enumerate(all_words):
    cur.append((s,e,w)); text = " ".join(x[2] for x in cur)
    gap = all_words[i+1][0]-e if i+1 < len(all_words) else 999
    if w.endswith((".","!","?","…")) or len(text) >= MAX_CHARS or e-cur[0][0] >= MAX_DUR or gap >= PAUSE:
        cues.append([cur[0][0], cur[-1][1], text]); cur = []
if cur: cues.append([cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)])

MIN_GAP, FLOOR = 0.08, 0.3
for i in range(len(cues)-1):
    if cues[i][1] > cues[i+1][0]-MIN_GAP:
        t2 = cues[i+1][0]-MIN_GAP
        if t2 < cues[i][0]+FLOOR:
            t2 = min(max(cues[i][0]+FLOOR, cues[i+1][0]-0.001), cues[i+1][0]-0.001)
        if t2 < cues[i][1]: cues[i][1] = t2

def ts(t):
    h=int(t//3600); m=int(t%3600//60); s=int(t%60); ms=int(round((t-int(t))*1000))
    if ms==1000: s,ms=s+1,0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
with open(OUT_SRT,"w",encoding="utf-8") as f:
    for i,(a,b,t2) in enumerate(cues,1): f.write(f"{i}\n{ts(a)} --> {ts(b)}\n{t2}\n\n")

sub_words = sum(len(t2.split()) for _,_,t2 in cues)
speech = sum(b-a for a,b,_ in cues)
log(f"VERIFY: Google returned {google_total} words, subtitle carries {sub_words}")
log(f"wrote {len(cues)} cues | speech density {speech/total*100:.1f}% | about ${total/60*0.003:.2f}")
json.dump({"video":os.path.basename(VIDEO),"minutes":round(total/60,2),"chunks":len(bounds)-1,
           "boundaries_snapped_to_silence":snapped,"google_words":google_total,
           "subtitle_words":sub_words,"cues":len(cues),
           "speech_density_pct":round(speech/total*100,1)},
          open(f"{WORK}/report.json","w"), indent=2, ensure_ascii=False)
sh("gcloud","storage","rm","-q","--recursive",*ACCOUNT,f"gs://{BUCKET}/{PREFIX}")
log("removed uploaded audio")
