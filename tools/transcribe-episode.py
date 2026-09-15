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

Around that core (tools/README.md has the measurements): saved responses are checked and reused
before anything is cut or uploaded, timings are rebuilt from the end offsets Google gets right,
long stretches without words are transcribed again in short pieces, cue times go through a pass
that cannot invert or overlap a cue, and each run ends with checks, a report and editor notes.
"""
import bisect, concurrent.futures, difflib, errno, glob, http.client, json, math, os, re, shutil, signal, socket, statistics
import subprocess, sys, threading, time, unicodedata, urllib.error, urllib.request

CONFIG_FILE = os.path.expanduser("~/.config/se-stt/config.env")
CHUNK, MAX_SNAP = float(os.environ.get("SE_STT_CHUNK", "") or 1080.0), 40.0
# Google caps word timestamps at 20 minutes per file, and the boundary loop can stretch a chunk
# by the 60 s it refuses to leave as a tail plus MAX_SNAP.
assert CHUNK + 60 + MAX_SNAP <= 1200
MAX_WORD, MAX_REPEATS = 5.0, 2
CHUNK_POLL = 10.0
MAX_RECUT_DEPTH = 3   # a troubled truncation tail may re-cut itself again this many times over;
                      # dur > 240 halving each round already bounds it, this is a second guard
PREFETCH_WORKERS = 4      # matches the recovery-piece upload pool; no evidence a higher number helps
STALL_WARN_AFTER = 8 * 60.0     # a chunk pending this long with no answer gets one WARNING, not silence
# A live incident (2026-09-16) found batchRecognize operations that simply never answered, at any
# processing strategy, on both an 18-minute and a 3-minute span of the same episode - not a failed
# response, no response ever, indefinitely. A fresh resubmission of the exact same span stalled the
# same way twice; a smaller re-cut span of the same audio succeeded. STALL_GIVE_UP_AFTER is how long
# recognize() waits before treating that as the same kind of problem an anomalous response is: re-cut
# and try smaller, down to STALL_RECUT_FLOOR, below which a still-stalled span is reported as an
# unrecovered gap rather than re-cut forever or guessed at from another source.
STALL_GIVE_UP_AFTER = 12 * 60.0
STALL_RECUT_FLOOR = 45.0

def log(m):
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # A redirected console on Windows writes in the ANSI code page, which has no ğ or ı, and the
        # closing lines print file names: a name must not turn a finished run into a crash.
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            print(line.encode(enc, "backslashreplace").decode(enc, "replace"), flush=True)
        except (OSError, ValueError):
            pass
    except (OSError, ValueError):
        pass    # the terminal is gone; the run still finishes and cleans up

def sec(v):
    try: return float(str(v).rstrip("s"))
    except Exception: return None

def ms(t):
    return int(round(t * 1000))

def load_config(path=CONFIG_FILE):
    """Deployment settings come from the environment, so no project, bucket or account name
    belongs to this file. Set them in your shell, or as KEY=VALUE lines in the config file,
    which is read first and never overrides a variable that is already set."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

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

def _tool(env_var, name, *fallbacks):
    """Locate a helper binary. An explicit override wins, then PATH, then the usual install
    locations. shutil.which is what makes this work on Windows, where gcloud is gcloud.cmd and
    Python's process launcher will not resolve that through PATHEXT on its own."""
    override = os.environ.get(env_var, "").strip()
    if override:
        if os.path.exists(override) or shutil.which(override):
            return override
        raise SystemExit(f"{env_var} is set to {override!r}, which does not exist.")
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        if os.path.exists(candidate):
            return candidate
    raise SystemExit(f"{name} not found. Install it and put it on PATH, "
                     f"or set {env_var} to its full path.")

def redact(text):
    """Error text can carry the bucket, the project or the account; none of them belong in a log.
    gcloud and Google also name them in plain words ("access b instance [name]", "project 1234...",
    "consumer 'project_number:1234...'"), and without a service account gcloud names the user's own
    email address, company domain included."""
    text = str(text)
    for name in ("SE_STT_PROJECT", "SE_STT_BUCKET", "SE_STT_SERVICE_ACCOUNT"):
        value = os.environ.get(name, "").strip()
        if len(value) >= 4:
            text = text.replace(value, "...")
    text = re.sub(r"gs://[^\s'\"]+", "gs://...", text)
    text = re.sub(r"projects/[^/\s'\"]+", "projects/...", text)
    text = re.sub(r"(?i)(project\w*[\s=:/'\"]+)\d{5,}", r"\1...", text)
    text = re.sub(r"operations/[^\s'\"]+", "operations/...", text)
    text = re.sub(r"--account=\S+", "--account=...", text)
    return re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "...", text)

RETRY_STATUS = (429, 500, 502, 503, 504)
UNSENT_ERRNO = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ECONNREFUSED}   # the request never left
GONE_STATUS = (400, 403, 404)   # Google's answer that an operation cannot be read, ever: not a blip

class RequestFailed(SystemExit):
    """A Google request that failed for good. A SystemExit, so an unhandled one ends the run with its
    message while a caller that can do without the answer catches it; status is the HTTP code, or
    None when no answer arrived."""
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

class Stopped(BaseException):
    """A termination signal (SIGTERM, SIGHUP, SIGBREAK). Deliberately neither an Exception nor a
    SystemExit: the best-effort handlers catch SystemExit so a failed Google request cannot end the
    run, and they swallowed a kill, which then kept polling for up to 20 minutes and exited 0.
    code is the exit status, 128 plus the signal number."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code

class Stalled(Exception):
    """recognize() gave up: Google never answered within STALL_GIVE_UP_AFTER, at any processing
    strategy, however many times the operation was resumed - not a failure response, no response at
    all. An ordinary Exception, unlike Stopped: transcribe_span catches this and re-cuts the span,
    the same recovery an anomalous response gets, rather than letting it end the run."""
    def __init__(self, start, end, tag):
        super().__init__(f"{tag} ({start:.0f}-{end:.0f}s) never answered within {STALL_GIVE_UP_AFTER/60:.0f} min")
        self.start, self.end, self.tag = start, end, tag

# The OS delivers a signal to the main thread only, where _stop() raises Stopped directly and
# interrupts whatever the main thread is blocked on (e.g. time.sleep), same as always. A worker
# thread prefetching a chunk concurrently never receives that signal at all, so without this it
# would just keep polling Google on its own for as long as recognize()'s loop runs, however long
# that is - exactly the kind of stall this pipeline should never silently sit through. _STOP_EVENT
# lets any thread notice a stop within one poll interval regardless of which thread the OS signalled.
_STOP_EVENT = threading.Event()
_STOP_CODE = [0]

def lost_answer(e):
    """Why a response could not be read, without the partial body an IncompleteRead carries."""
    if isinstance(e, http.client.IncompleteRead):
        return "the response was cut off"
    if isinstance(e, ValueError):
        return "the response was not the expected JSON"
    return redact(getattr(e, "reason", None) or e)

def replace_retry(src, dst):
    """os.replace, retried: Windows antivirus briefly locks fresh files, a 100 MB flac included."""
    for attempt in range(4):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.5)

def remove_quietly(path):
    """A temporary file that cannot be removed (locked on Windows) must not stop the run."""
    try:
        os.remove(path)
    except OSError:
        pass

def atomic_write(path, text):
    """Write then rename, so a crash mid-write cannot leave a truncated file that a later run
    trusts. Windows antivirus briefly locks fresh files, so the rename is retried."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    replace_retry(tmp, path)

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default

# ---------- responses ----------
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

def words_from_raw(st):
    """The same words with the offsets exactly as Google sent them, None where a field is missing."""
    out = []
    for _, fr in st.get("response",{}).get("results",{}).items():
        for r in fr.get("inlineResult",{}).get("transcript",{}).get("results",[]):
            for a in r.get("alternatives",[])[:1]:
                for w in a.get("words",[]):
                    out.append([sec(w["startOffset"]) if "startOffset" in w else None,
                                sec(w["endOffset"]) if "endOffset" in w else None, w.get("word","")])
    return out

def billed_seconds(st):
    """Audio Google billed for one response, or None when the response does not say."""
    resp = st.get("response") or {}
    billed = sec(resp.get("totalBilledDuration"))
    if billed is None:
        parts = [sec((fr.get("metadata") or {}).get("totalBilledDuration")) for fr in (resp.get("results") or {}).values()]
        parts = [p for p in parts if p is not None]
        billed = sum(parts) if parts else None
    return billed

def response_error(st):
    """The error a finished operation carries, redacted, or None."""
    if st.get("error"):
        err = st["error"]
        return redact(err.get("message") or err if isinstance(err, dict) else err)[:300]
    for fr in ((st.get("response") or {}).get("results") or {}).values():
        if (fr.get("error") or {}).get("message"):
            return redact(fr["error"]["message"])[:300]
    return None

def cache_problem(st, start_ms, end_ms, lang, model, video_bytes):
    """Why a saved response cannot stand in for this span, or None when it can.

    A response is only as good as the audio it was made from. Tail and re-cut spans move when
    upstream decisions change, and "1.mp4" is not a unique file name, so every response carries
    the span it answers. Responses saved before that record existed are checked by language,
    model and billed duration, which Google rounds up to the whole second."""
    if not isinstance(st, dict) or not isinstance(st.get("response"), dict):
        return "it is not a finished response"
    if response_error(st):
        return "it carries a recognition error"
    span = st.get("_span")
    if span is not None:
        try:
            if abs(int(span["start_ms"]) - start_ms) > 2 or abs(int(span["end_ms"]) - end_ms) > 2:
                return "it was recognized for different span bounds"
        except (KeyError, TypeError, ValueError):
            return "its span record is unreadable"
        if span.get("language") != lang or span.get("model") != model:
            return f"it was recognized as {span.get('language')} {span.get('model')}"
        if span.get("video_bytes") != video_bytes:
            return "it belongs to a different video file"
        return None
    cfg = ((st.get("metadata") or {}).get("batchRecognizeRequest") or {}).get("config") or {}
    if "languageCodes" in cfg and cfg["languageCodes"] != [lang]:
        return f"it was recognized as {cfg['languageCodes']}"
    if "model" in cfg and cfg["model"] != model:
        return f"it was recognized with {cfg['model']}"
    billed = billed_seconds(st)
    span_s = (end_ms - start_ms) / 1000.0
    if billed is None:
        return "it does not say how much audio it covers"
    if not -0.05 <= billed - span_s <= 1.05:
        return f"it covers {billed:.0f} s, not the {span_s:.2f} s span"
    return None

def quiet_midpoint(quiet, start, end):
    """Where to split a span that needs re-cutting, anomalous or stalled alike: the nearest detected
    silence within 60 s of the middle, or the exact middle when nothing that quiet is nearby.
    Recognition is deterministic, so resending the same audio changes nothing; cutting somewhere
    else does."""
    mid_t = start + (end - start) / 2
    near = [q for q in quiet if abs(q - mid_t) <= 60 and start + 60 < q < end - 60]
    return min(near, key=lambda q: abs(q - mid_t)) if near else mid_t

# ---------- words ----------
def collapse(ws, loops=None):
    """Trim a phrase repeated 3 or more times back to back to 2 copies. When `loops` is a list,
    each removal is recorded with the time the repeats covered, so recovery can tell a removed
    loop from missing speech."""
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
            if loops is not None:
                block = ws[i:i+n*L]
                loops.append({"start": min(w[0] for w in block), "end": max(w[1] for w in block),
                              "phrase": [w[2] for w in ws[i:i+L]], "repeats": n, "removed": (n-MAX_REPEATS)*L})
            out.extend(ws[i:i+MAX_REPEATS*L]); removed += (n-MAX_REPEATS)*L; i += n*L
        else:
            out.append(ws[i]); i += 1
    return out, removed

def repair_legacy(ws, dur):
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

# Timing repair. Measured on cached responses: END offsets are accurate (median 0.10 s against an
# independent aligner) while a START is usually just the previous word's end, so a pause gets
# swallowed into the next word. Word ORDER held where the numbers did not (262 of 263 checked
# words). So ends anchor the placement, order is never changed, and a word whose end cannot be
# trusted is packed right BEFORE the next trustworthy word, which is where every checked
# misplaced word belonged.
TYP_A, TYP_B, TYP_MAX = 0.08, 0.045, 0.65  # typical spoken duration: 0.08 s + 45 ms per letter, capped
MAXDUR_A, MAXDUR_B    = 0.25, 0.08         # longest believable word: 0.25 s + 80 ms per letter
EOF_TOL   = 5.0    # ends up to 5 s past the audio stay anchors and are moved back inside
JITTER    = 0.30   # tolerated backward wobble between consecutive ends
LEAD_ZONE = 4.0    # a compressed chunk start ends within 4 s of the chunk start...
LEAD_JUMP = 2.0    # ...and is followed by a jump of at least 2 s
LONG_SYNC = 5.0    # a swallowed pause this long after compressed start words re-synchronises
SHIFT_MIN = 5      # blocks of 5+ words are moved rigidly; smaller conflicts are re-packed
MIN_W     = 0.04   # shortest duration a placed word may get
GAP_R     = 0.05   # breath kept before the anchor a moved run is packed against
TRIM_LONG, TRIM_CAP = 2.0, 1.2   # a swallowed pause is only trimmed from a word "lasting" over 2 s

def _letters(w): return max(1, len(re.sub(r"[\W_]", "", w)))
def _typ(w):     return min(TYP_A + TYP_B * _letters(w), TYP_MAX)
def _maxdur(w):  return MAXDUR_A + MAXDUR_B * _letters(w)

def _spread(need, room):
    """Durations that fill `room` exactly, as close to `need` as the MIN_W floor allows."""
    if not need:
        return []
    if room < MIN_W * len(need):
        return [max(0.0, room) / len(need)] * len(need)
    extra = room - MIN_W * len(need)
    weight = [max(0.0, x - MIN_W) for x in need]
    total = sum(weight)
    if total <= 0:
        return [room / len(need)] * len(need)
    return [MIN_W + extra * w / total for w in weight]

def repair(ws, dur, head=True, unanchor=()):
    """End-anchored, order-trusting timing repair of one span.

    ws: [start, end, word] with the offsets as Google sent them (None where missing), after
    collapse. Returns (kept, events): kept[i] = (start, end, word) in the same order, one entry
    per input word, inside [0, dur]; events are the structural moves as dicts with the word
    indices they touched. Per-word placements are not events. head=False skips the opening
    repair (step 4) for a span that starts in the middle of speech. unanchor: word indices whose
    end is not trusted whatever it says, so they are packed right before the next anchor (a
    recovery piece's compressed opening, see piece_words); reported as a "head" move."""
    n = len(ws)
    S = [w[0] for w in ws]; E = [w[1] for w in ws]; T = [w[2] for w in ws]
    S0 = list(S)
    end = [e if (e is not None and 0.0 < e <= dur + EOF_TOL) else None for e in E]
    found, packed = [], {}
    anchors = lambda: [i for i in range(n) if end[i] is not None]
    def start_of(j):
        return S[j] if (S[j] is not None and end[j] - _maxdur(T[j]) <= S[j] <= end[j]) else end[j] - _typ(T[j])
    def shift(block, k):
        for j in block:
            end[j] += k
            if S[j] is not None: S[j] += k

    # 1. lone outliers: one end that breaks an otherwise ordered sequence is discarded
    changed = True
    while changed:
        changed = False; A = anchors()
        for k in range(len(A) - 1):
            i, q = A[k], A[k + 1]
            if end[q] >= end[i] - JITTER: continue
            fut = sorted(end[x] for x in A[k + 2:k + 7])
            med = fut[len(fut) // 2] if fut else None
            if med is not None and med >= end[i] - JITTER:
                end[q] = None; found.append(("outlier", [q])); changed = True; break
            if (k == 0 or end[A[k - 1]] <= end[q] + JITTER) and (med is None or med >= end[q] - JITTER):
                end[i] = None; found.append(("outlier", [i])); changed = True; break

    # 2. blocks moved in time: order is trusted, so after a backward jump either the block after
    #    it is too early or the block before it is too late; move whichever fits rigidly, packed
    #    against the anchor that follows it, else free the smaller one for re-packing
    for _ in range(10000):
        A = anchors()
        k = next((k for k in range(1, len(A)) if end[A[k]] < end[A[k - 1]] - JITTER), None)
        if k is None: break
        m = k
        while m < len(A) and end[A[m]] < end[A[k - 1]] - JITTER: m += 1
        B, U = A[k:m], (A[m] if m < len(A) else None)
        p0 = k
        while p0 > 0 and end[A[p0 - 1]] > end[A[k]] + JITTER: p0 -= 1
        P, V = A[p0:k], (A[p0 - 1] if p0 > 0 else None)
        mono = lambda b: all(end[b[j]] >= end[b[j - 1]] - JITTER for j in range(1, len(b)))
        span = lambda b: end[b[-1]] - start_of(b[0])
        room_b = (start_of(U) if U is not None else dur) - GAP_R - end[P[-1]]
        room_p = start_of(B[0]) - GAP_R - (end[V] if V is not None else 0.0)
        h1 = len(B) >= SHIFT_MIN and mono(B) and span(B) <= room_b
        h2 = len(P) >= SHIFT_MIN and mono(P) and span(P) <= room_p
        if h1 and (not h2 or len(B) <= len(P)):
            shift(B, (start_of(U) if U is not None else dur) - GAP_R - end[B[-1]]); found.append(("block", B))
        elif h2:
            shift(P, start_of(B[0]) - GAP_R - end[P[-1]]); found.append(("block", P))
        else:
            victim = B if len(B) <= len(P) else P
            for j in victim: end[j] = None
            found.append(("repacked", victim))

    # 3. ends past the audio: only the trailing run that overruns moves. Shifting everything that
    #    would collide squeezed correct speech into slivers when a chunk was cut mid-sentence.
    A = anchors()
    over = next((x for x in range(len(A)) if end[A[x]] > dur), None)
    if over is not None:
        run = A[over:]
        before = end[A[over - 1]] if over > 0 else 0.0
        delta = max(end[j] for j in run) - dur
        if start_of(run[0]) - delta >= before:
            shift(run, -delta)
        else:
            need = [_typ(T[j]) for j in run]
            room = max(0.0, dur - before)
            if sum(need) > room:
                need = [max(MIN_W, x * room / sum(need)) for x in need]
            t = dur
            for j, x in zip(reversed(run), reversed(need)):
                packed[j] = (t - x, t); end[j] = t; S[j] = t - x; t -= x
        found.append(("eof", run))

    # 4. first speech of the span, compressed toward 0 s: chirp re-synchronises at a later word
    #    whose start equals the previous end and whose duration swallows the lost time. Only a span
    #    that opens on silence or music does that. A truncation tail starts on words that really are
    #    at its start (so can a recovery piece, see piece_words): re-synchronising one moved a
    #    chunk's last line of dialogue 185 s into the end credits, where a laughter token held the
    #    "swallowed" time.
    def evidence(upto):
        upto = min(upto, 12)
        return (any(E[j] is None or E[j] <= 0 for j in range(upto)) or
                any(E[j] is not None and E[j - 1] is not None and E[j] < E[j - 1] - 0.01 for j in range(1, upto)) or
                any(S[j] is not None and E[j] is not None and E[j] < S[j] for j in range(upto)))
    def swallowed(j):
        return E[j] is not None and (S[j] is None or (j > 0 and E[j - 1] is not None and abs(S[j] - E[j - 1]) < 0.01))
    # laughter and interjections come back with broken offsets in runs, so one cannot be the re-sync word
    L = next((j for j in range(n) if swallowed(j) and E[j] - (S[j] if S[j] is not None else 0.0) > LEAD_JUMP
              and not is_interjection(T[j])), None) if head else None
    sync = None
    if L is not None and end[L] is not None:
        before = max([end[j] for j in range(L) if end[j] is not None], default=0.0)
        if before <= LEAD_ZONE or (E[L] - (S[L] if S[L] is not None else 0.0) > LONG_SYNC and evidence(L)):
            sync = L
    # An explicit None test: word #0 can be the re-sync word, and 0 is falsy.
    if sync is not None:
        prev = [j for j in range(sync) if end[j] is not None]
        if prev:
            k = end[sync] - _typ(T[sync]) - GAP_R - end[prev[-1]]
            if k > 0:
                shift(prev, k); found.append(("head", prev))
        A = [j for j in range(sync + 1) if end[j] is not None]
        for kk in range(1, len(A)):
            i, p = A[kk], A[kk - 1]
            if end[i] - end[p] > LEAD_JUMP or i == sync:
                if i == sync or evidence(i):
                    for j in range(i): end[j] = None
                    found.append(("head", list(range(i))))
                break
    elif head:
        A = anchors()
        for kk in range(1, min(len(A), 40)):
            i, p = A[kk], A[kk - 1]
            if end[p] > LEAD_ZONE: break
            if end[i] - end[p] > LEAD_JUMP:
                if evidence(i) or swallowed(i):
                    for j in range(i): end[j] = None
                    found.append(("head", list(range(i))))
                break

    freed = [j for j in unanchor if 0 <= j < n and end[j] is not None]
    for j in freed:
        end[j] = None
    if freed:
        found.append(("head", freed))

    # 5. place: anchored words keep their end and their own start; only the swallowed-pause
    #    signature has its start trimmed (trimming more made measured onsets late, and digits
    #    are longer than their letter count suggests), plus any word "lasting" longer than
    #    MAX_WORD, which no speaker produces: one broken response had words spanning 6 to 21 s.
    #    The first word of a span has no previous end to compare with, so a lead-in it "lasts" over
    #    2 s counts as swallowed too when it starts within LEAD_JUMP of the span start: a recovery
    #    piece's first word took 1.6 s of the words before it, started before the piece's share of
    #    the audio, and was dropped by both pieces. A first word that starts later, after silence,
    #    keeps its raw start like every other word.
    #    Runs without an end are packed right before the next anchor, or after the last one but
    #    never past the audio.
    out, prev_end, i = [None] * n, 0.0, 0
    while i < n:
        if end[i] is not None:
            e, lo = end[i], prev_end
            if i in packed:
                s2 = max(lo, packed[i][0])
            else:
                basis = S0[i] if S0[i] is not None else (E[i - 1] if i > 0 and E[i - 1] is not None else 0.0)
                gulped = (S0[i] is None or (i == 0 and S0[i] < LEAD_JUMP) or
                          (i > 0 and E[i - 1] is not None and abs(S0[i] - E[i - 1]) < 0.01))
                if ((gulped and E[i] - basis > TRIM_LONG) or E[i] - basis > MAX_WORD) and not re.search(r"\d", T[i]):
                    s2 = max(lo, e - min(3 * _typ(T[i]), TRIM_CAP))
                elif S[i] is not None and S[i] <= e - MIN_W:
                    s2 = max(lo, S[i])
                else:
                    s2 = max(lo, e - _typ(T[i]))
            if s2 > e - MIN_W: s2 = max(lo, e - MIN_W)
            e = max(e, s2 + MIN_W)
            out[i] = (s2, e); prev_end = e; i += 1
            continue
        j = i
        while j < n and end[j] is None: j += 1
        run = list(range(i, j)); need = [_typ(T[r]) for r in run]
        if j < n:
            hi = max(prev_end, start_of(j) - GAP_R)
            if hi - prev_end < MIN_W * len(run):
                hi = min(prev_end + MIN_W * len(run), end[j] - MIN_W)
            room = max(0.0, hi - prev_end); tot = sum(need)
            if tot > room: need = [max(MIN_W, x * room / tot) for x in need]; tot = sum(need)
            t = max(prev_end, hi - tot)
        else:
            room = max(0.0, dur - prev_end)
            if sum(need) > room:
                need = _spread(need, room)
            t = prev_end
        for r, x in zip(run, need):
            out[r] = (t, t + x); t += x
        prev_end = t; i = j

    kept = [(min(max(out[k][0], 0.0), dur), min(max(out[k][1], 0.0), dur), T[k]) for k in range(n)]
    # One incident often trips several steps (stray ends inside a compressed opening), so moves
    # whose words touch are reported once, under the most telling kind.
    groups = []
    for kind, idx in sorted(((k, sorted(set(i))) for k, i in found if i), key=lambda x: x[1][0]):
        if groups and idx[0] <= groups[-1]["hi"] + 2:
            g = groups[-1]
            g["hi"] = max(g["hi"], idx[-1]); g["idx"].update(idx); g["kinds"].add(kind)
        else:
            groups.append({"hi": idx[-1], "idx": set(idx), "kinds": {kind}})
    events = []
    for g in groups:
        idx = sorted(g["idx"])
        if g["kinds"] == {"outlier"}:
            # A discarded outlier is usually a broken END with a good start, and the word is placed at
            # its start: measured against the broken end, one word "moved 327 s" that had not moved.
            # So each word's move is measured against whichever raw offset it kept.
            moved = sorted(min((d for d in (kept[j][1] - E[j] if E[j] is not None else None,
                                            kept[j][0] - S0[j] if S0[j] is not None else None) if d is not None), key=abs)
                           for j in idx if E[j] is not None or S0[j] is not None)
        else:
            moved = sorted(kept[j][1] - E[j] for j in idx if E[j] is not None) or \
                    sorted(kept[j][0] - S0[j] for j in idx if S0[j] is not None)
        kind = next(k for k in ("block", "head", "eof", "repacked", "outlier") if k in g["kinds"])
        events.append({"kind": kind, "idx": idx, "shift_s": round(moved[len(moved) // 2], 2) if moved else 0.0})
    return kept, events

def placement_ok(kept, dur):
    return (all(kept[k][0] >= kept[k - 1][0] for k in range(1, len(kept))) and
            all(0.0 <= s <= e <= dur for s, e, _ in kept))

def place_words(col, legacy, dur, tag, head=True, unanchor=()):
    """New placement for a collapsed span ([s, e, word, raw_s, raw_e] rows), or the previous
    placement when the new one breaks its own guarantees. Returns (words, events)."""
    try:
        kept, events = repair([[c[3], c[4], c[2]] for c in col], dur, head, unanchor)
        if placement_ok(kept, dur):
            return kept, events
    except Exception:
        pass
    log(f"WARNING: timing repair fell back to the previous method for {tag}")
    return [tuple(w) for w in legacy], []

def shifted(words, off):
    return [(w[0] + off, w[1] + off) + tuple(w[2:]) for w in words]

def shifted_events(items, off):
    return [dict(x, start=x["start"] + off, end=x["end"] + off) for x in items]

def chunk_bounds(total, quiet):
    bounds, t = [0.0], CHUNK
    while t < total - 60:
        near = [q for q in quiet if abs(q - t) <= MAX_SNAP and q > bounds[-1] + 60]
        bounds.append(min(near, key=lambda q: abs(q - t)) if near else t)
        t += CHUNK
    bounds.append(total)
    return bounds

# ---------- cues ----------
MAX_CHARS, MAX_SPAN, PAUSE, BRIDGE = 84, 7.0, 0.7, 1.5
SENT_END = (".", "!", "?", "…", "؟")        # the Arabic question mark ends a sentence too
CLAUSE_END = (",", ";", ":", "،", "؛")
_EDGE_PUNCT = ".,!?;:\"'()[]…،؛؟«»“”‘’-\u2013\u2014"
# Words a cue should not end on, because the phrase they open would dangle. Chosen by the
# language setting only: the Turkish list must not fire on the English "her" of a mixed file.
NO_BREAK_TR = {"ve", "veya", "ya", "ama", "fakat", "ancak", "çünkü", "eğer", "hem", "ile",
               "bir", "bu", "şu", "her", "en", "daha", "çok", "hiç", "ne"}
# Subtitle Edit's Dictionaries/ar_NoBreakAfterList.xml, plus the hamza-less spellings chirp
# writes and the conjunctions it sometimes emits as separate tokens.
NO_BREAK_AR = {"أن", "أو", "أي", "أياً", "أيًا",
               "إلى", "إن", "التي", "الذي",
               "بين", "ثم", "حتى", "دون", "على",
               "عما", "عن", "عندما", "في",
               "لأن", "لكن", "لم", "لن", "مثل",
               "من", "يا", "مع", "ان", "او", "اي",
               "الى", "لان", "و", "ف"}

def no_break_after(lang):
    """A predicate for words a cue should not end on: the Turkish list for tr, the Arabic list for
    ar, and for any other setting only the Arabic list, which can only match Arabic-script tokens."""
    turkish = lang.split("-")[0].lower() == "tr"
    words = NO_BREAK_TR if turkish else NO_BREAK_AR
    def check(w):
        if w.endswith(SENT_END + CLAUSE_END):
            return False
        bare = w.strip(_EDGE_PUNCT)
        if turkish:
            bare = bare.replace("I", "ı").replace("İ", "i")
        return bare.lower() in words
    return check

def cue_groups(words, lang):
    """Word index ranges, one per cue. The production rules (sentence end, 84 characters, span,
    a pause of 0.7 s) plus three refinements that kept phrases whole on measured episodes: a
    short pause right after a word like "ve" or "في" does not split, a short pause does not
    strand a sentence that ends within two words, and at the character or span limit the cut backs
    off to the best clause mark or pause among the last six words instead of cutting hard.

    A limit is checked before a word joins the cue. Checked after it, a word that also ended a
    sentence or crossed 7 s skipped the back-off, and cues of 85 to 90 characters came out on
    Arabic episodes, which rarely have punctuation to split on."""
    function_end = no_break_after(lang)
    groups, i0, n, i = [], 0, len(words), 0
    while i < n:
        e, w = words[i][1], words[i][2]
        text_len = sum(len(x[2]) + 1 for x in words[i0:i + 1]) - 1
        if i > i0 and (text_len > MAX_CHARS or e - words[i0][0] > MAX_SPAN):
            best = bk = None
            for k in range(max(i0, i - 6), i):
                if function_end(words[k][2]):
                    continue
                score = (2 if words[k][2].endswith(CLAUSE_END) else 0) + (words[k + 1][0] - words[k][1])
                if best is None or score > best:
                    best, bk = score, k
            groups.append((i0, (i if bk is None else bk + 1)))
            i0 = groups[-1][1]
            continue        # the same word is looked at again, now as part of the next cue
        gap = words[i + 1][0] - e if i + 1 < n else 999
        flush = w.endswith(SENT_END)
        if not flush and gap >= PAUSE:
            bridge = gap < BRIDGE and (function_end(w) or any(
                words[k][2].endswith(SENT_END) and all(words[m + 1][0] - words[m][1] < PAUSE for m in range(i + 1, k))
                for k in range(i + 1, min(i + 3, n))))
            flush = not bridge
        if flush:
            groups.append((i0, i + 1)); i0 = i + 1
        i += 1
    if i0 < n:
        groups.append((i0, n))
    return groups

def cue_groups_legacy(words):
    """The previous builder: flush at a sentence end, 84 characters, 6 s or a 0.7 s pause."""
    groups, i0, n = [], 0, len(words)
    for i in range(n):
        e, w = words[i][1], words[i][2]
        text_len = sum(len(x[2]) + 1 for x in words[i0:i + 1]) - 1
        gap = words[i + 1][0] - e if i + 1 < n else 999
        if w.endswith((".", "!", "?", "…")) or text_len >= 84 or e - words[i0][0] >= 6.0 or gap >= 0.7:
            groups.append((i0, i + 1)); i0 = i + 1
    if i0 < n:
        groups.append((i0, n))
    return groups

def cues_from_groups(words, groups):
    """[start, end, text, last word end]: a cue starts with its first word and ends with its last."""
    return [[words[a][0], words[b - 1][1], " ".join(w[2] for w in words[a:b]), words[b - 1][1]] for a, b in groups]

MIN_GAP_MS, MIN_CUE_MS, MIN_DUR_MS, LINGER_MS = 80, 1, 1000, 100

def timing_pass(cues, total):
    """Cue times an editor can trust, in whole milliseconds so rounding cannot break them.

    The previous MIN_GAP/FLOOR loop produced an inverted cue when a later cue started before an
    earlier one. Here an in-order cue only ever loses the end of its tail to the gap; a start
    moves only when two cues are out of order (or start less than the gap plus 1 ms apart, where
    no trim can help), by pulling the earlier cue back or pushing the later one on, whichever
    moves less, and every such move is reported. Then short cues are held for Subtitle Edit's
    default minimum display time and a short linger, but only into free time, never moving a
    start. Afterwards: start < end, next start >= end + 80 ms, all inside [0, total], same order
    and text. Returns ([[start, end, text]], stats)."""
    T = int(total * 1000)
    c = [[min(max(ms(a), 0), T), min(max(ms(b), 0), T), text, min(max(ms(lw), 0), T)] for a, b, text, lw in cues]
    n, moves = len(c), []
    for i in range(n):
        if c[i][1] < c[i][0] + MIN_CUE_MS:
            c[i][1] = c[i][0] + MIN_CUE_MS
        if i == 0:
            continue
        p = c[i - 1]
        need = p[1] + MIN_GAP_MS
        if c[i][0] >= need:
            continue
        if c[i][0] >= p[0] + MIN_CUE_MS + MIN_GAP_MS:
            p[1] = c[i][0] - MIN_GAP_MS
            continue
        pull_end = c[i][0] - MIN_GAP_MS
        floor = c[i - 2][1] + MIN_GAP_MS if i >= 2 else 0
        pull_cost, push_cost = p[1] - pull_end, need - c[i][0]
        if pull_end - MIN_CUE_MS >= floor and pull_cost <= push_cost:
            p[0], p[1] = max(floor, pull_end - (p[1] - p[0])), pull_end
            moves.append(("pulled", i - 1, pull_cost))
        else:
            c[i][0] = need
            c[i][1] = max(c[i][1], need + MIN_CUE_MS)
            moves.append(("pushed", i, push_cost))
    lim = T
    for i in range(n - 1, -1, -1):
        if c[i][1] <= lim:
            break
        c[i][1] = lim
        if c[i][0] > lim - MIN_CUE_MS:
            moves.append(("pulled", i, c[i][0] - (lim - MIN_CUE_MS)))
            c[i][0] = max(0, lim - MIN_CUE_MS)
        lim = c[i][0] - MIN_GAP_MS
    extended = 0
    for i in range(n):
        room = min(c[i + 1][0] - MIN_GAP_MS, T) if i + 1 < n else T
        end = max(c[i][1], min(c[i][0] + MIN_DUR_MS, room), min(c[i][3] + LINGER_MS, room))
        if end > c[i][1]:
            extended += 1
            c[i][1] = end
    stats = {"extended": extended, "pulled": sum(1 for m in moves if m[0] == "pulled"),
             "pushed": sum(1 for m in moves if m[0] == "pushed"),
             "moves": [{"kind": k, "cue": i + 1, "start": c[i][0] / 1000.0, "shift_s": round(d / 1000.0, 3)} for k, i, d in moves]}
    return [[a / 1000.0, b / 1000.0, text] for a, b, text, _ in c], stats

def cue_violations(cues, total):
    """Inverted, out of order, overlapping or outside [0, total], in whole milliseconds."""
    bad = {"inverted": 0, "out_of_order": 0, "overlap": 0, "outside": 0}
    for i, (a, b, _) in enumerate(cues):
        A, B = ms(a), ms(b)
        if B <= A: bad["inverted"] += 1
        if A < 0 or B > ms(total): bad["outside"] += 1
        if i:
            if A < ms(cues[i - 1][0]): bad["out_of_order"] += 1
            elif A < ms(cues[i - 1][1]): bad["overlap"] += 1
    return bad

def cues_follow_words(cues, words):
    """The self-check: cue text is exactly the word stream in order, and starts never go back."""
    return (" ".join(c[2] for c in cues).split() == " ".join(w[2] for w in words).split() and
            all(cues[i][0] >= cues[i - 1][0] for i in range(1, len(cues))))

def build_subtitle(words, lang, total):
    """Cues for a word stream. Returns (cues, stats, builder)."""
    if os.environ.get("SE_STT_CUE_BUILDER", "").strip().lower() != "legacy":
        try:
            cues, stats = timing_pass(cues_from_groups(words, cue_groups(words, lang)), total)
            if cues_follow_words(cues, words):
                return cues, stats, "greedy"
        except Exception:
            pass
        log("WARNING: cue check failed, using the previous cue builder")
    cues, stats = timing_pass(cues_from_groups(words, cue_groups_legacy(words)), total)
    return cues, stats, "legacy"

def speech_seconds(words):
    """Seconds covered by at least one word. Cue durations are stretched for reading, so they
    no longer say how much of the episode is speech."""
    covered, cur_s, cur_e = 0.0, None, None
    for s, e in sorted((w[0], w[1]) for w in words):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                covered += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        covered += cur_e - cur_s
    return covered

# ---------- hole recovery ----------
# The model can skip minutes of speech inside one response (measured: a 6 minute stretch of
# dialogue under music came back as nothing), and no word count can see audio that was never
# transcribed. Long word gaps are transcribed again in short pieces, which returned that speech,
# and only words that are really missing are inserted: a gap is often made by words the model
# timed elsewhere, and inserting those duplicated dialogue on a measured episode.
GAP_MIN_DEFAULT = 15.0
PIECE, OVERLAP, PAD, GRID = 45.0, 4.0, 2.0, 0.5   # 60 s pieces lost a song; the grid keeps piece tags stable when gap edges move by milliseconds
EDGE = 0.05                 # a recovered word must lie strictly inside the gap
CTX_MIN, CTX_MAX = 30.0, 180.0
NEAR = 1.5                  # aligned to an existing copy this close in time: the same word
MISS_ABS, MISS_FRAC = 2, 0.25
RUN_MIN = 5                 # this many edge words found inside the gap: a displaced run, not a hole
FUZZY_PAD, FUZZY_RATIO = 15.0, 0.6
BUDGET_SHARE = 0.6          # never send more recovery audio than this share of the episode
MIN_SPEECH_SHARE = 0.20     # below this the whole run is suspect (wrong language, broken audio)
REC_POLL, REC_TIMEOUT = 5.0, 20 * 60.0
REC_POLL_TIMEOUT = 30.0     # one status request of a recovery piece; the 5 s rounds are the retries
REC_BAD_ROUNDS = 3          # rounds in a row where no status request got through: stop waiting
REC_RESUME_WAIT = 2 * 60.0  # an operation an earlier run left gets at least this long, however old
STRAGGLER_FACTOR = 5.0      # the last 10% of the pieces sent wait this many times the median answer time
MAX_PIECE_ATTEMPTS = 2      # a piece whose operation failed twice is reported, not sent a third time
OP_MAX_AGE = 24 * 3600.0
HEAD_ZONE = 300.0           # a chunk's compressed opening sat up to 216 s before where it is spoken
VARIANT_SPAN = 3            # words between two aligned copies that may be spelling variants
MISPLACED_MIN = 3           # recovered words matching subtitle words far from them: report the timing
EARLY_ANSWER = 5.0          # a piece of 20 s or more whose words all end this soon answered with its start only
LEAD_GAP_PIECE = 10.0       # a later piece's opening words, then a raw jump this long: a compressed opening
SEAM_NEAR = 2.0             # two pieces' copies of a word this close in time are the same word (a repaired opening moved 1.05 s)
GAP_MIN_RANGE = (5.0, 600.0)
_ARABIC_MARKS = re.compile("[\u064b-\u065f\u0670\u0640]")   # harakat, superscript alef, tatweel
_ARABIC_FOLD = str.maketrans("أإآٱىةؤئ", "اااايهوي")
# Interjections and laughter, on the normalized key: alif runs, (alif) ha runs, oh, ha-ha; ah, oh,
# eh, hi (Turkish dotless), hmm. Only whole keys match, so ya, huwa, hiya, la and "o" stay words.
_INTERJECTION = re.compile("^(?:ا+|ا*ه+|ا*و+ه+|(?:ها){2,}"
                           "|a+h*|o+h+|u+h+|e+h+|(?:h+[aıu]+)+h*|h+m+|m+h*m+)$")
# A bumper screen with generic outro music, in a stretch recovery goes looking for speech in,
# twice produced a bracket fragment of the model's own hidden instructions to itself ("MUSIC]",
# "BACKGROUND]") instead of a transcript: as of 2026-09, the request never sees this text, so it
# can only be the model's own prompt leaking through. See has_content.
_LEAKED_TOKENS = frozenset({"music", "background", "nospeech", "silence"})
_warned = set()

def warn_once(key, message):
    if key not in _warned:
        _warned.add(key)
        log(f"WARNING: {message}")

def gap_min():
    """Read when called: a default argument bound when the function was defined silently ignored changes.
    0 or a negative value made every pause a gap (hundreds of warnings and the whole budget billed),
    and nan or inf silently turned recovery off, so both are refused."""
    raw = os.environ.get("SE_STT_GAP_MIN", "").strip()
    if not raw:
        return GAP_MIN_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value):
        warn_once(("gap_min", raw), f"SE_STT_GAP_MIN={raw} is not a number of seconds, using the default {GAP_MIN_DEFAULT:.0f}")
        return GAP_MIN_DEFAULT
    lo, hi = GAP_MIN_RANGE
    if not lo <= value <= hi:
        value = min(max(value, lo), hi)
        warn_once(("gap_min", raw), f"SE_STT_GAP_MIN={raw} is outside {lo:.0f} to {hi:.0f} s, using {value:.0f}")
    return value

def clock(t):
    t = int(round(max(0.0, t)))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"

def norm_key(word, lang=""):
    """Spelling-insensitive key: case, Arabic diacritics, tatweel, hamza forms, ta marbuta and alif
    maqsura, and punctuation do not make two words different. The Turkish I and dotted I fold to
    dotless and dotted i only for Turkish: elsewhere "It" and "it" are the same word."""
    t = unicodedata.normalize("NFKC", word)
    if lang.split("-")[0].lower() == "tr":
        t = t.replace("İ", "i").replace("I", "ı")
    t = t.casefold().replace("\u0307", "")
    t = _ARABIC_MARKS.sub("", t).translate(_ARABIC_FOLD)
    return re.sub(r"[\W_]+", "", t)

def is_interjection(word, lang=""):
    key = norm_key(word, lang)
    return bool(key) and _INTERJECTION.match(key) is not None

def has_content(word, lang):
    """A letter or a digit, and not one of the model's own leaked instructions or a token mixing
    Latin and Cyrillic letters: over a bumper screen's outro music, one recovery run inserted
    "MUSIC]" and "BACKGROUND]" as if they were spoken, and another a single word half Latin half
    Cyrillic ("B\u043f\u0440\u043e\u0447\u0435\u043c"), none of them real speech in any language. The bracket has to still be
    there: an ordinary word that happens to spell "music" or "background" is real speech, only a
    stray half of one of the model's own bracket tags is not. For Arabic-script languages: Arabic
    letters and no Latin ones, or a number. Music also comes back as stray marks and Latin
    fragments (a "6Y" was inserted under a song), while a number is real speech."""
    bare = re.sub(r"[\W_]+", "", word)
    if ("[" in word or "]" in word) and bare.casefold() in _LEAKED_TOKENS:
        return False
    if any("a" <= ch.casefold() <= "z" for ch in bare) and any("\u0400" <= ch <= "\u04ff" for ch in bare):
        return False
    if lang.split("-")[0].lower() in ("ar", "fa", "ur"):
        if bare and all(unicodedata.category(ch) == "Nd" for ch in bare):
            return True
        arabic = any("\u0600" <= ch <= "\u06ff" and unicodedata.category(ch) == "Lo" for ch in word)
        latin = any(ch.isalpha() and not "\u0600" <= ch <= "\u06ff" for ch in word)
        return arabic and not latin
    return re.search(r"[^\W_]", word) is not None

def silence_lookup(silence):
    """A function giving the index of the detected silence a timed word lies fully inside, or None."""
    sil = sorted(silence)
    starts = [s for s, _ in sil]
    def which(s, e):
        k = bisect.bisect_right(starts, s + 0.05) - 1
        return k if k >= 0 and e <= sil[k][1] + 0.05 and e - s > 0.05 else None
    return which

def find_gaps(words, total, minimum=None):
    """(start, end, insert_at) for every stretch of at least `minimum` seconds without words, the
    head and tail included. The running max end keeps seam disorder from inventing gaps."""
    if minimum is None:
        minimum = gap_min()
    if not words:
        return [(0.0, total, 0)] if total >= minimum else []
    out = []
    if words[0][0] >= minimum:
        out.append((0.0, words[0][0], 0))
    run_end = words[0][1]
    for i in range(1, len(words)):
        if words[i][0] - run_end >= minimum:
            out.append((run_end, words[i][0], i))
        run_end = max(run_end, words[i][1])
    if total - run_end >= minimum:
        out.append((run_end, total, len(words)))
    return out

def plan_pieces(a, z, audio_end):
    """45 s pieces with 4 s overlap over the gap plus 2 s either side, snapped outward to a half
    second. Piece k owns [start + 2, start + 43), the first from the region start and the last to
    its end, so an overlap is never counted twice. Returns [(start, end, own_from, own_to)]."""
    lo = max(0.0, math.floor((a - PAD) / GRID) * GRID)
    hi = min(audio_end, math.ceil((z + PAD) / GRID) * GRID)
    starts = [lo]
    while starts[-1] + PIECE < hi:
        starts.append(starts[-1] + PIECE - OVERLAP)
    out = []
    for k, s in enumerate(starts):
        e = min(s + PIECE, hi)
        own_a = lo if k == 0 else s + OVERLAP / 2
        own_z = hi if k == len(starts) - 1 else s + PIECE - OVERLAP / 2
        if own_z > own_a and e - s >= 1.0 and e <= audio_end + 1e-9:
            out.append((s, e, own_a, own_z))
    return out

def piece_tag(start, end):
    return f"rec-{ms(start)}-{ms(end)}"

def edge_share(keys, context, lang):
    """Share of the recovered word keys found among the subtitle words in `context`, spelling variants
    included. Words under 3 letters match too much by chance, so they only count when nothing longer
    is there."""
    ctx = [k for k in (norm_key(x[2], lang) for x in context) if k]
    longer = [k for k in keys if len(k) >= 3]
    keys = longer or [k for k in keys if k]
    hits = sum(1 for k in keys if any(k == c or difflib.SequenceMatcher(None, k, c).ratio() >= FUZZY_RATIO for c in ctx))
    return hits / len(keys) if keys else 0.0

def piece_words(st, start, end, own_a, own_z, tag, context=None, lang="", heard=None, prior=(), all_words=False):
    """A piece's words in episode time, through the same collapse and repair as a chunk, keeping
    the ones the piece owns. `est` marks words whose timing the previous repair had to estimate or
    that came without any offset, `anchored` words that kept their own end offset, `moved` words
    the opening repair placed away from where the plain placement puts them.

    context: subtitle words around the piece. A piece that starts inside a stretch of music opens
    like a chunk, compressed toward 0 s, and on cached episodes the opening repair moved those words
    to where the subtitle or the editor has them. The 3 pieces that started on the words before
    their gap opened on those words themselves, and the same repair moved them 15 to 30 s into the
    gap, where they were inserted a second time or passed for a displaced run. So the repair is
    undone when the words it moves are, for the most part, the subtitle words already at the piece
    start.

    A piece after the first of its region can open the same way without the repair noticing, when
    the pause it swallowed follows a word with a real start: "Selim Bey." came back at 0.2 to 0.7 s
    and the next word 33 s later, where it is spoken, so both words fell before the piece's share
    and were dropped by it and by the piece before, which never heard them. Such an opening is packed
    right before the word after the jump, unless the subtitle or the previous piece (prior, (start,
    end, word) in episode time) already has those words at the piece start, or the words after the
    jump repeat them: a chant line said once came back at 0 s and again where it is spoken, and
    moving the first copy put the line in twice.

    heard, a dict, receives "until": the episode time the piece's last placed word ends, or None
    without words. all_words also returns the words the piece does not own, each marked "owned"."""
    dur = end - start
    raw, filled = words_from_raw(st), words_from(st)
    col, _ = collapse([f + r[:2] for f, r in zip(filled, raw)])
    legacy, _ = repair_legacy([c[:3] for c in col], dur)
    placed, _ = place_words(col, legacy, dur, tag)
    still, head = place_words(col, legacy, dur, tag, head=False)[0], True
    if context:
        moved = [k for k in range(len(col)) if abs(placed[k][1] - still[k][1]) > NEAR]
        if moved:
            hi = start + max(still[k][1] for k in moved) + 1.0
            near = [x for x in context if start - 1.0 <= x[0] <= hi]
            if near and edge_share([norm_key(col[k][2], lang) for k in moved], near, lang) >= 0.5:
                placed, head = still, False
    if own_a > start + 1e-9 and col:
        m = next((k for k in range(len(col)) if placed[k][0] + start >= own_a), None)
        ends = [col[k][4] for k in range(m or 0) if col[k][4] is not None]
        nxt = None if not m else (col[m][3] if col[m][3] is not None else col[m][4])
        if ends and nxt is not None and max(ends) <= LEAD_ZONE and nxt - max(ends) >= LEAD_GAP_PIECE:
            near = [x for x in (context or ()) if start - 1.0 <= x[0] <= own_a + 1.0] + [x for x in prior if x[0] >= start - 1.0]
            keys = [norm_key(col[k][2], lang) for k in range(m)]
            # exact spellings only: "Bey" and "Şey" are close enough for edge_share
            longer = [k for k in keys if len(k) >= 3] or [k for k in keys if k]
            later = {norm_key(c[2], lang) for c in col[m:2 * m + 2]}
            echoed = bool(longer) and sum(k in later for k in longer) >= 0.5 * len(longer)
            if not (near and edge_share(keys, near, lang) >= 0.5) and not echoed:
                lead, _ = place_words(col, legacy, dur, tag, head=head, unanchor=range(m))
                if lead[0][0] + start >= own_a and lead[m - 1][1] <= lead[m][0] + 1e-9:
                    log(f"  {tag}: {some(m, 'opening word')} came back {lead[0][0] - placed[0][0]:.0f} s before the words "
                        f"after {'it' if m == 1 else 'them'} and {'was' if m == 1 else 'were'} placed right before those words")
                    placed = lead
    if heard is not None:
        heard["until"] = start + max(w[1] for w in placed) if placed else None
    out = []
    for k, c in enumerate(col):
        s, e = placed[k][0] + start, placed[k][1] + start
        owned = own_a <= s < own_z
        if owned or all_words:
            x = {"s": s, "e": e, "w": c[2], "est": (legacy[k][0], legacy[k][1]) != (c[0], c[1]) or (c[3] is None and c[4] is None),
                 "anchored": c[4] is not None and abs(placed[k][1] - c[4]) <= 0.05,
                 "moved": abs(placed[k][1] - still[k][1]) > 0.05}
            if all_words:
                x["owned"] = owned
            out.append(x)
    return out

def _same_word(x, y, lang):
    kx, ky = norm_key(x["w"], lang), norm_key(y["w"], lang)
    if not kx or not ky or abs(x["s"] - y["s"]) > SEAM_NEAR:
        return False
    return kx == ky or (len(kx) >= 3 and len(ky) >= 3 and difflib.SequenceMatcher(None, kx, ky).ratio() >= FUZZY_RATIO)

def seam_merge(pieces, per, lang):
    """The recovered words of consecutive pieces, each word once. Every piece hears the few seconds
    it shares with the next one; normally each keeps the words that start in its own share. But a
    piece's opening can come back broken (no start, no end, a word 3.6 s long) and the repair moves
    those words across the share boundary: then both pieces owned "tahlil sonuçlarınızı", and the
    verb after it, which the earlier piece had timed right, was owned by neither. So the words both
    pieces heard in the overlap are aligned, and where one copy kept its own end offset and the other
    did not (no offset, estimated, or moved by the opening repair, 1.05 s there), the sound copy is
    kept whoever owns it. Otherwise each piece keeps what it owns.
    per[k]: piece_words(..., all_words=True) of pieces[k]. Returns the kept words."""
    keep = [[x["owned"] for x in ws] for ws in per]
    sound = lambda x: x["anchored"] and not x["est"] and not x["moved"]
    for k in range(len(per) - 1):
        lo, hi = pieces[k + 1][0] - 1.0, pieces[k][1] + 1.0
        A = [i for i, x in enumerate(per[k]) if lo <= x["s"] <= hi]
        B = [j for j, x in enumerate(per[k + 1]) if lo <= x["s"] <= hi]
        if not A or not B:
            continue
        # longest common subsequence of the two overlaps, words matching by spelling and time
        L = [[0] * (len(B) + 1) for _ in range(len(A) + 1)]
        for p in range(len(A) - 1, -1, -1):
            for q in range(len(B) - 1, -1, -1):
                L[p][q] = (L[p + 1][q + 1] + 1 if _same_word(per[k][A[p]], per[k + 1][B[q]], lang)
                           else max(L[p + 1][q], L[p][q + 1]))
        p = q = 0
        while p < len(A) and q < len(B):
            a, b = per[k][A[p]], per[k + 1][B[q]]
            if _same_word(a, b, lang) and L[p][q] == L[p + 1][q + 1] + 1:
                if sound(a) != sound(b):
                    keep[k][A[p]], keep[k + 1][B[q]] = sound(a), sound(b)
                p += 1; q += 1
            elif L[p + 1][q] >= L[p][q + 1]:
                p += 1
            else:
                q += 1
    return [x for ws, ks in zip(per, keep) for x, kept in zip(ws, ks) if kept]

def classify_gap(existing, rec, a, z, lang, loops=(), ctx_from=None, silence=(), events=()):
    """Split the recovered words inside the gap (a, z) into new, already present and filtered.

    Existing words around the gap are aligned with the recovered region. A recovered word is
    already present when its aligned copy is within NEAR seconds, when it belongs to a run of edge
    words the model timed outside the gap, or when it sits between aligned copies in place of a
    subtitle word it merely spells differently: a name heard two ways was inserted a second time,
    out of order. When a walk from either edge finds RUN_MIN or more words the gap is a displaced
    run and nothing is inserted: simulated holes never triggered it, while a displaced run otherwise
    leaked duplicated dialogue. The looser spelling match over a whole region only applies inside a
    detected run, since applied to any gap it dropped 9 to 16% of real words. Recovered words
    matching a loop that collapse removed in this stretch are the loop, already represented.

    Filtered: timing estimated by repair, no real content, a word that kept its own end offset yet
    lies inside a detected silence (a second copy from an overlapping piece), or a gap whose new
    words are all interjections (a song came back as 13 sighs, listed as recovered speech). A word
    the repair packed can sit in a pause next to where it is spoken, so silence does not rule it out.

    ctx_from widens the aligned stretch back to a chunk start, where a compressed opening stayed up
    to 210 s before where it is spoken. `copies` describes subtitle words matched more than
    MOVE_WARN_S away from their recovered copy: their own time range, count, offset and how many of
    them are suspect on their own (inside a detected silence, or moved over MOVE_WARN_S by the
    timing repair). Either copy can be the mistimed one, and only that evidence says which."""
    C = min(CTX_MAX, max(CTX_MIN, z - a))
    lo = a - C if ctx_from is None else min(a - C, ctx_from)
    ctx = [x for x in existing if lo <= x[0] <= z + C]
    ek = [norm_key(x[2], lang) for x in ctx]
    rk = [norm_key(x["w"], lang) for x in rec]
    inside = lambda j: a + EDGE < rec[j]["s"] and rec[j]["e"] < z - EDGE
    sm = difflib.SequenceMatcher(None, ek, rk, autojunk=False)
    pair = {}
    for b in sm.get_matching_blocks():
        for k in range(b.size):
            if rk[b.b + k]:
                pair[b.a + k] = b.b + k
    dup = {j for i, j in pair.items() if abs(ctx[i][0] - rec[j]["s"]) <= NEAR}
    before = [i for i in range(len(ctx)) if ctx[i][1] <= a + EDGE][::-1]
    after = [i for i in range(len(ctx)) if ctx[i][0] >= z - EDGE]
    walk, walk_ctx, matched, proven = set(), [], {}, set()
    for side, far, lead in ((before, after, True), (after, before, False)):
        miss = seen = 0
        hits, hit_ctx, beyond = [], [], []
        for i in side:
            if i in pair and not inside(pair[i]) and (rec[pair[i]]["e"] >= z - EDGE if lead else rec[pair[i]]["s"] <= a + EDGE) \
                    and (rec[pair[i]]["s"] > a + EDGE if lead else rec[pair[i]]["e"] < z - EDGE):
                # its copy reaches past the other edge of the gap: neither found inside nor missing. A
                # sentence the chunk cut in two by a swallowed pause ends there, and counting its last
                # words as misses stopped the walk before the words that are in the gap.
                beyond.append(i)
                continue
            seen += 1
            if i in pair and inside(pair[i]):
                hits.append(pair[i]); hit_ctx.append(i)
            else:
                miss += 1
                if miss > max(MISS_ABS, MISS_FRAC * seen):
                    break
        dup.update(hits)
        matched.update(zip(hit_ctx, hits))
        if len(hits) >= RUN_MIN:
            walk.update(hits); walk_ctx += hit_ctx
        # Evidence that the subtitle copy is the mistimed one: recovery hears the edge words, the rest of
        # their sentence past the other edge and the first subtitle word on that side, which the subtitle
        # times right, as one unbroken run. Music is not silence, so the silence test never saw it. Without
        # the rest of the sentence it is no evidence: a piece that opened late put a correctly timed line
        # right before the next one.
        ids = sorted(hit_ctx + beyond)
        if hit_ctx and beyond and far and far[0] in pair and ids == list(range(ids[0], ids[0] + len(ids))) \
                and ids[-1 if lead else 0] == side[0]:
            js = [pair[i] for i in ids]
            jn, edge_word = pair[far[0]], ctx[far[0]]
            if js == list(range(js[0], js[0] + len(js))) and jn == (js[-1] + 1 if lead else js[0] - 1) \
                    and abs(rec[jn]["s"] - edge_word[0]) <= NEAR \
                    and (rec[jn]["s"] - rec[js[-1]]["e"] if lead else rec[js[0]]["s"] - rec[jn]["e"]) <= NEAR:
                proven.update(hit_ctx)
    # Spelling variants in place of a subtitle word: between two aligned words (at least one of
    # them a confirmed copy), a few recovered words stand where the subtitle has a few others, and
    # one spells a word there differently. Unrelated words in that place are still new.
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if (op != "replace" or i1 == 0 or j1 == 0 or i2 >= len(ek) or j2 >= len(rk)
                or i2 - i1 > VARIANT_SPAN or j2 - j1 > VARIANT_SPAN or ((j1 - 1) not in dup and j2 not in dup)):
            continue
        # the aligned neighbours' time offsets; a variant sits at the same offset, which a match
        # across the whole gap (a word before it against a word after it) does not
        offs = [rec[jj]["s"] - ctx[ii][0] for ii, jj in ((i1 - 1, j1 - 1), (i2, j2)) if jj in dup]
        nxt = i1
        for j in range(j1, j2):
            if j in dup or not rk[j] or not inside(j):
                continue
            i = next((i for i in range(nxt, i2) if ek[i] and i not in matched
                      and any(abs(rec[j]["s"] - ctx[i][0] - off) <= NEAR for off in offs)
                      and difflib.SequenceMatcher(None, rk[j], ek[i]).ratio() >= FUZZY_RATIO), None)
            if i is not None:
                dup.add(j); matched[i] = j; nxt = i + 1
    if walk:
        span_keys = [k for k in ek[min(walk_ctx):max(walk_ctx) + 1] if k]
        flo = min(rec[j]["s"] for j in walk) - FUZZY_PAD
        fhi = max(rec[j]["s"] for j in walk) + FUZZY_PAD
        for j in range(len(rec)):
            if j in dup or not inside(j) or not rk[j] or not flo <= rec[j]["s"] <= fhi:
                continue
            if any(difflib.SequenceMatcher(None, rk[j], key).ratio() >= FUZZY_RATIO for key in span_keys):
                dup.add(j)
    quiet = silence_lookup(silence)
    moved = [(ev["start"], ev["end"]) for ev in events if abs(ev["shift_s"]) > MOVE_WARN_S]
    suspect = lambda x: quiet(x[0], x[1]) is not None or any(s <= x[1] and x[0] <= e for s, e in moved)
    run = walk_ctx if walk else [i for i, j in matched.items() if abs(rec[j]["s"] - ctx[i][0]) > MOVE_WARN_S]
    copies = None
    if run:
        offsets = sorted(rec[matched[i]]["s"] - ctx[i][0] for i in run)
        copies = {"words": len(run), "start": min(ctx[i][0] for i in run), "end": max(ctx[i][1] for i in run),
                  "offset_s": round(offsets[len(offsets) // 2], 1), "suspect": sum(1 for i in run if i in proven or suspect(ctx[i]))}
    loop_keys = {norm_key(t, lang) for lp in loops if lp["start"] < z and lp["end"] > a for t in lp["phrase"]}
    loop_keys.discard("")
    result = {"new": [], "present": [], "filtered": [], "noise": 0, "displaced": bool(walk),
              "run_words": len(walk_ctx), "copies": copies}
    for j, x in enumerate(rec):
        if not inside(j):
            continue
        if j in dup or rk[j] in loop_keys:
            result["present"].append(x)
        elif x["est"] or not has_content(x["w"], lang) or (x.get("anchored") and quiet(x["s"], x["e"]) is not None):
            result["filtered"].append(x)
        else:
            result["new"].append(x)
    if result["new"] and all(is_interjection(x["w"], lang) for x in result["new"]):
        result["noise"] = len(result["new"])
        result["filtered"] += result["new"]
        result["new"] = []
    return result

def empty_recovery():
    return {"status": "not run", "gaps_checked": 0, "pieces": 0, "audio_s": 0.0, "inserted": 0,
            "already_present": 0, "filtered": 0, "noise": 0, "skipped": [], "displaced_runs": [],
            "misplaced": [], "inserted_ranges": [], "still_empty": []}

def empty_stretches(ws, a, z, minimum):
    """(start, end) of every stretch of at least `minimum` seconds inside [a, z] without a word of ws."""
    out, run_end = [], a
    for s, e, _ in sorted(ws):
        if s - run_end >= minimum:
            out.append((run_end, s))
        run_end = max(run_end, e)
    if z - run_end >= minimum:
        out.append((run_end, z))
    return out

def recover_holes(words, audio_end, lang, fetch, loops=(), silence=(), span_starts=(), events=()):
    """Words of the episode with recovered words inserted before the first word after each gap.
    Existing words are never moved, removed or retimed. fetch(pieces) -> {tag: response, None when
    recognition failed, or "pending" while it still runs}. span_starts are the starts of the
    recognized chunks, events the timing repair's structural moves. Returns (words, stats); times in
    stats are not rounded."""
    stats = empty_recovery()
    if audio_end <= 0 or speech_seconds(words) < MIN_SPEECH_SHARE * audio_end:
        stats["status"] = "too little speech recognized"
        return list(words), stats
    stats["status"] = "ran"
    minimum = gap_min()
    gaps = find_gaps(words, audio_end, minimum)
    stats["gaps_checked"] = len(gaps)
    budget, spent, plan = BUDGET_SHARE * audio_end, 0.0, []
    for a, z, at in sorted(gaps, key=lambda g: g[0] - g[1]):        # longest first
        pieces = plan_pieces(a, z, audio_end)
        audio = sum(p[1] - p[0] for p in pieces)
        if not pieces:
            continue
        if spent + audio > budget:
            log(f"WARNING: recovery budget reached, the gap {clock(a)}-{clock(z)} was not checked")
            stats["skipped"].append({"start": a, "end": z, "reason": "budget"})
            continue
        spent += audio
        plan.append((a, z, at, pieces))
    unique = {}
    for _, _, _, pieces in plan:
        for p in pieces:
            unique.setdefault(piece_tag(p[0], p[1]), p)
    stats["pieces"] = len(unique)
    stats["audio_s"] = round(sum(p[1] - p[0] for p in unique.values()), 1)
    got = fetch(list(unique.values())) if unique else {}
    ordered = sorted(words, key=lambda w: w[0])       # seams can leave the stream slightly out of order
    starts = [w[0] for w in ordered]
    inserts = {}
    for a, z, at, pieces in sorted(plan):
        answers = [got.get(piece_tag(p[0], p[1])) for p in pieces]
        if any(not isinstance(st, dict) for st in answers):
            pending = any(st == "pending" for st in answers)
            log(f"WARNING: the gap {clock(a)}-{clock(z)} was not recovered, " +
                ("its recognition is still running; a later run fills it in" if pending else "its audio could not be transcribed"))
            stats["skipped"].append({"start": a, "end": z, "reason": "still running" if pending else "recognition failed"})
            continue
        per, early = [], []
        for s, e, own_a, own_z in pieces:
            context = ordered[bisect.bisect_left(starts, s - 1.0):bisect.bisect_right(starts, e)]
            heard = {}
            prior = [(x["s"], x["e"], x["w"]) for x in per[-1] if x["s"] >= s - 1.0 and not x["est"]] if per else []
            per.append(piece_words(got[piece_tag(s, e)], s, e, own_a, own_z, piece_tag(s, e), context, lang, heard,
                                   prior=prior, all_words=True))
            if e - s >= 20.0 and heard["until"] is not None and heard["until"] - s < EARLY_ANSWER:
                early.append((heard["until"], e))
        rec = sorted(seam_merge(pieces, per, lang), key=lambda x: x["s"])
        opened = [b for b in span_starts if b <= a < b + HEAD_ZONE]
        res = classify_gap(words, rec, a, z, lang, loops, ctx_from=min(opened) if opened else None,
                           silence=silence, events=events)
        stats["filtered"] += len(res["filtered"])
        stats["noise"] += res["noise"]
        copies = res["copies"]
        if res["displaced"]:
            stats["already_present"] += len(res["present"]) + len(res["new"])
            stats["displaced_runs"].append({"start": a, "end": z, "words": res["run_words"], "run_start": copies["start"],
                                            "run_end": copies["end"], "offset_s": copies["offset_s"]})
            log(f"  gap {clock(a)}-{clock(z)}: {res['run_words']} words timed next to it may belong inside it, nothing inserted")
            continue
        stats["already_present"] += len(res["present"])
        if copies and copies["words"] >= MISPLACED_MIN and 2 * copies["suspect"] >= copies["words"]:
            stats["misplaced"].append({"start": a, "end": z, "words": copies["words"], "run_start": copies["start"],
                                       "run_end": copies["end"], "offset_s": copies["offset_s"]})
        new = sorted(res["new"], key=lambda x: x["s"])
        if new:
            inserts[at] = [(x["s"], x["e"], x["w"]) for x in new]
            stats["inserted_ranges"].append({"start": new[0]["s"], "end": new[-1]["e"], "words": len(new),
                                             "gap_start": a, "gap_end": z})
            if len(new) >= MIN_LISTED_RUN:
                # A stretch recognition skipped can hide more than the pieces returned: one piece
                # answered with its first second only, and 44 s of dialogue stayed empty unnoticed.
                # Empty stretches between recovered lines are mostly music, so only a stretch that a
                # piece left unanswered like that is reported.
                for s2, e2 in empty_stretches(inserts[at], a, z, minimum):
                    if any(min(e2, pe) - max(s2, ps) >= minimum for ps, pe in early):
                        stats["still_empty"].append({"start": s2, "end": e2, "gap_start": a, "gap_end": z})
        log(f"  gap {clock(a)}-{clock(z)} ({z - a:.0f} s): {len(new)} words inserted, "
            f"{len(res['present'])} already present, {len(res['filtered'])} filtered")
    out = []
    for i, w in enumerate(words):
        out.extend(inserts.get(i, ()))
        out.append(w)
    out.extend(inserts.get(len(words), ()))
    stats["inserted"] = len(out) - len(words)
    stats["inserted_ranges"].sort(key=lambda r: r["start"])
    return out, stats

# ---------- checks and editor notes ----------
BUILDER_VERSION = "2026-09-14"
SILENT_RUN = 3        # words in a row timed inside one detected silence: 3 real misplacements found, no false alarm
MOVE_WARN_S = 5.0     # timing moves an editor should spot-check
MUSIC_GAP_S = 45.0
SLOW_CHUNK = 0.5      # a chunk under half the median words per minute
MIN_LISTED_RUN = 5    # recovered stretches shorter than this get one compact line, not an item each
DISPLACED_WARN = 20   # a displaced run this long is a warning whatever its offset
MOVE_TEXT = {"block": "in a block", "head": "at a chunk start", "outlier": "with a stray timestamp",
             "repacked": "in a conflicting run", "eof": "past the end of their chunk"}
# Findings about the whole episode. Merged by time they swallowed every stretch into one
# "00:00:00 to the end" item, and the editor lost the places to check.
EPISODE_WIDE = ("cue_integrity", "too_little_speech", "recovery_failed")
MERGE_TOL = 1.0       # findings this close are one place for the editor (two moves 5 ms apart came out as two items for one cue)
STATUS_TEXT = {"pass": "all checks passed", "warn": "stretches to check are listed below",
               "fail": "FAILED, see below", "unknown": "the checks could not run"}

def words_in_silence(words, silence, min_run=SILENT_RUN):
    """(start, end, count) for runs of consecutive words fully inside one detected silence.
    Speech cannot sit there, so the offsets are wrong even when repair found them plausible."""
    inside = silence_lookup(silence)
    which = lambda w: inside(w[0], w[1])
    out, i = [], 0
    while i < len(words):
        k = which(words[i])
        if k is None:
            i += 1
            continue
        j = i
        while j + 1 < len(words) and which(words[j + 1]) == k:
            j += 1
        if j - i + 1 >= min_run:
            out.append((words[i][0], max(w[1] for w in words[i:j + 1]), j - i + 1))
        i = j + 1
    return out

def slow_chunks(words, bounds):
    """(start, end, words per minute, median) for chunks of 4 minutes or more that carry under half
    the median rate. A skipped stretch shows up here even when no single gap is long."""
    rows = []
    starts = sorted(w[0] for w in words)
    for a, b in zip(bounds, bounds[1:]):
        if b - a >= 240:
            n = bisect.bisect_left(starts, b) - bisect.bisect_left(starts, a)
            rows.append((a, b, n / ((b - a) / 60)))
    if len(rows) < 3:
        return []
    med = statistics.median(r[2] for r in rows)
    return [(a, b, r, med) for a, b, r in rows if r < SLOW_CHUNK * med]

def some(n, noun):
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"

def finding(check, start, end, detail, **extra):
    return dict({"check": check, "start": round(start, 2), "end": round(end, 2),
                 "start_hms": clock(start), "end_hms": clock(end), "detail": detail}, **extra)

def copies_text(d, displaced, sure=True):
    """What an editor needs about subtitle words that recovery heard somewhere else. Without sure,
    the words may just be a phrase said twice, so the text does not call them mistimed."""
    n, off = d["words"], d.get("offset_s") or 0.0
    where = "later" if off > 0 else "earlier"
    were, they = ("was", "it is") if n == 1 else ("were", "they are")
    if displaced:
        return (f"{some(n, 'word')} here {were} recognized again about {abs(off):.0f} s {where}, inside "
                f"{clock(d['start'])} to {clock(d['end'])} where the subtitle has no words" +
                (f", so {they} probably timed wrong" if sure else "") +
                "; nothing was inserted there, so check that stretch by ear too")
    return (f"{some(n, 'word')} here {were} recognized again about {abs(off):.0f} s {where}, near "
            f"{clock(d['run_start'] + off)}, so {they} probably timed wrong; check the timing")

def run_checks(cues, words, total, audio_end, silence, events, rec, bounds):
    """verify block: FAIL for a broken file, WARN for stretches an editor must check, NOTE for the rest."""
    fails, warns, notes = [], [], []
    bad = cue_violations(cues, total)
    if sum(bad.values()):
        fails.append(finding("cue_integrity", 0.0, total, "the subtitle has broken cues: " +
                             ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in bad.items() if v)))
    if rec.get("status") == "too little speech recognized":
        fails.append(finding("too_little_speech", 0.0, audio_end,
                             "words cover under 20% of the audio, so recovery was skipped; check the language setting"))
    if rec.get("status") == "failed":
        warns.append(finding("recovery_failed", 0.0, audio_end, "hole recovery failed, so missing speech was not looked for"))
    # Laughter and sighs come back with broken offsets over music and end credits; a warning about
    # where one sits gives the editor nothing to check (5 of 7 warnings on one episode).
    ordered = sorted(words, key=lambda w: w[0])
    starts = [w[0] for w in ordered]
    def only_interjections(s, e):
        inside = ordered[bisect.bisect_left(starts, s - 0.01):bisect.bisect_right(starts, e + 0.01)]
        inside = [w for w in inside if w[1] <= e + 0.01]
        return bool(inside) and all(is_interjection(w[2]) for w in inside)
    for s, e, n in words_in_silence(words, silence):
        if only_interjections(s, e):
            continue
        warns.append(finding("words_in_silence", s, e, f"{some(n, 'word')} timed inside silence, so {'its' if n == 1 else 'their'} timing is probably wrong", words=n))
    for ev in events:
        if abs(ev["shift_s"]) > MOVE_WARN_S and not only_interjections(ev["start"], ev["end"]):
            # Moved, not "moved to where they are spoken": one opening still sat 51 s early after its move.
            warns.append(finding("timing_moved", ev["start"], ev["end"],
                                 f"{some(ev['words'], 'word')} {'was' if ev['words'] == 1 else 'were'} moved {abs(ev['shift_s']):.0f} s "
                                 f"{'later' if ev['shift_s'] > 0 else 'earlier'} by the timing repair, check the timing",
                                 words=ev["words"], kind=ev["kind"], shift_s=ev["shift_s"]))
    for g in rec.get("skipped", []):
        text = {"budget": "not checked for missing speech (recovery budget reached)",
                "still running": "not checked for missing speech yet (recognition still running, a later run fills it in)"}
        warns.append(finding("gap_not_checked", g["start"], g["end"],
                             text.get(g["reason"], "could not be checked for missing speech (recognition failed)")))
    for d in rec.get("displaced_runs", []):
        # A long run, or one far from its copies, is a timing error an editor must fix: 179 words sat
        # 328 s early. A few words a few seconds off are more often a phrase said twice.
        sure = d["words"] >= DISPLACED_WARN or abs(d.get("offset_s") or 0.0) > MOVE_WARN_S
        (warns if sure else notes).append(finding("displaced_run", d.get("run_start", d["start"]), d.get("run_end", d["end"]), copies_text(d, True, sure),
                             words=d["words"], gap_start=round(d["start"], 2), gap_end=round(d["end"], 2), offset_s=d.get("offset_s")))
    for d in rec.get("misplaced", []):
        warns.append(finding("misplaced_words", d["run_start"], d["run_end"], copies_text(d, False),
                             words=d["words"], gap_start=round(d["start"], 2), gap_end=round(d["end"], 2), offset_s=d["offset_s"]))
    for g in rec.get("still_empty", []):
        warns.append(finding("gap_still_empty", g["start"], g["end"],
                             f"nothing was recovered from {clock(g['start'])} to {clock(g['end'])} although recognition "
                             f"skipped speech around it, so check that part by ear"))
    for r in rec.get("inserted_ranges", []):
        notes.append(finding("recovered", r.get("gap_start", r["start"]), r.get("gap_end", r["end"]),
                             f"recognition skipped this stretch, {some(r['words'], 'word')} recovered",
                             words=r["words"], words_start=round(r["start"], 3), words_end=round(r["end"], 3)))
    unchecked = [(g["start"], g["end"]) for g in rec.get("skipped", []) + rec.get("displaced_runs", []) + rec.get("still_empty", [])]
    for a, z, _ in find_gaps(words, audio_end, MUSIC_GAP_S):
        if rec.get("status") != "ran":
            notes.append(finding("no_words", a, z, "no words, and recovery did not run, so missing speech was not looked for"))
        elif not any(a < e and z > s for s, e in unchecked):
            notes.append(finding("no_speech", a, z, "no speech found, probably music"))
    for a, b, rate, med in slow_chunks(words, bounds):
        notes.append(finding("slow_chunk", a, b, f"{rate:.0f} words per minute against a median of {med:.0f}"))
    status = "fail" if fails else "warn" if warns else "pass"
    return {"status": status, "fails": fails, "warns": warns, "notes": notes}

def sanitize(text):
    """Transcript text quoted in the notes must not bring in em or en dashes."""
    return re.sub("[\u2012\u2013\u2014\u2015]", "-", text)

def cue_word_spans(cues, words):
    """(first word start, last word end) of every cue, or None when the cues cannot be matched to
    the word stream token by token."""
    if any(len(w[2].split()) != 1 for w in words):
        return None
    spans, k = [], 0
    for c in cues:
        n = len(c[2].split())
        if n == 0 or k + n > len(words):
            return None
        spans.append((words[k][0], max(w[1] for w in words[k:k + n])))
        k += n
    return spans if k == len(words) else None

def cue_range(cues, a, b, spans=None):
    """Cue numbers for [a, b]. With the cues' word times a stretch without words is named by the
    cues around it: a cue's end is stretched for reading into the silence after its last word, so
    by cue times every such stretch seemed to belong to the cue before it."""
    spans = spans or [(c[0], c[1]) for c in cues]
    first = next((i for i, (s, e) in enumerate(spans) if e > a + 0.01), None)
    last = next((i for i in range(len(spans) - 1, -1, -1) if spans[i][0] < b - 0.01), None)
    if first is not None and last is not None and first <= last:
        return f"cue {first + 1}" if first == last else f"cues {first + 1} to {last + 1}"
    if not spans:
        return "no cues"
    if last is None:
        return "before cue 1"
    if first is None:
        return f"after cue {len(spans)}"
    return f"between cues {last + 1} and {last + 2}"

def render_notes(cues, words, minutes, verify, rec, events, cue_stats):
    """Editor notes: what to check, where, with cue numbers, and numbers that add up."""
    spans = cue_word_spans(cues, words)
    recovered = [f for f in verify["notes"] if f["check"] == "recovered"]
    short = [f for f in recovered if f["words"] < MIN_LISTED_RUN]
    wide = [f for f in verify["fails"] + verify["warns"] if f["check"] in EPISODE_WIDE]
    listed = [("recovered", f) for f in recovered if f["words"] >= MIN_LISTED_RUN]
    listed += [("", f) for f in verify["notes"] if f["check"] == "displaced_run"]
    listed += [("", f) for f in verify["warns"] + verify["fails"] if f["check"] not in EPISODE_WIDE]
    items = []
    # the longer finding first at the same start, so a detail about part of a stretch follows the stretch
    for kind, f in sorted(listed, key=lambda x: (x[1]["start"], -x[1]["end"])):
        quote = ""
        if kind == "recovered":
            lo, hi = f.get("words_start", f["start"]) - 0.01, f.get("words_end", f["end"]) + 0.01
            inside = [w[2] for w in words if lo <= w[0] and w[1] <= hi]
            quote = " ".join(inside[:8]) + (" ..." if len(inside) > 8 else "")
        if items and f["start"] <= items[-1]["end"] + MERGE_TOL:
            items[-1]["end"] = max(items[-1]["end"], f["end"])
            if f["detail"] not in items[-1]["text"]:
                items[-1]["text"].append(f["detail"])
            if quote: items[-1]["quotes"].append(quote)
        else:
            items.append({"start": f["start"], "end": f["end"], "text": [f["detail"]], "quotes": [quote] if quote else []})
    L = ["# Transcription notes", "",
         f"{len(cues)} cues, {len(words)} words, {minutes:.0f} min. Automatic checks: {STATUS_TEXT.get(verify['status'], verify['status'])}.", ""]
    if items or wide:
        L += ["## Please check", ""]
        for f in wide:
            L.append(f"- Whole episode: {f['detail']}")
        for it in items:
            L.append(f"- {clock(it['start'])} to {clock(it['end'])} ({cue_range(cues, it['start'], it['end'], spans)}): {'; '.join(it['text'])}")
            for q in it["quotes"]:
                L.append(f"  \"{q}\"")      # on its own line, so right-to-left text does not scramble the line
        L.append("")
    if short:
        L += ["## Short recovered words", "",
              "Recognition skipped these stretches and only a few words came back; check they are speech: " +
              ", ".join(f"{clock(f.get('words_start', f['start']))} "
                        f"({cue_range(cues, f.get('words_start', f['start']), f.get('words_end', f['end']), spans)})" for f in short) + ".", ""]
    quiet = [f for f in verify["notes"] if f["check"] == "no_speech"]
    if quiet:
        L += ["## No speech found", "",
              "Stretches of 45 s or more with no words, probably music: " + ", ".join(f"{f['start_hms']} to {f['end_hms']}" for f in quiet) + ".", ""]
    unchecked = [f for f in verify["notes"] if f["check"] == "no_words"]
    if unchecked:
        L += ["## Stretches without words", "",
              "Recovery did not run, so these stretches of 45 s or more were not checked for missing speech: "
              + ", ".join(f"{f['start_hms']} to {f['end_hms']}" for f in unchecked) + ".", ""]
    # Not merged into "Please check": a chunk runs 4 to 18 minutes and would swallow every item in it.
    slow = [f for f in verify["notes"] if f["check"] == "slow_chunk"]
    if slow:
        L += ["## Slow chunks", "",
              "These chunks carry under half the usual words per minute, so recognition may have skipped speech "
              "in pauses too short to be recovered; check them by ear: " +
              ", ".join(f"{f['start_hms']} to {f['end_hms']} ({cue_range(cues, f['start'], f['end'], spans)}, {f['detail']})" for f in slow) + ".", ""]
    warns = verify["warns"]
    count = lambda check: len([f for f in warns if f["check"] == check])
    elsewhere = [f for f in warns + verify["notes"] if f["check"] in ("displaced_run", "misplaced_words")]
    big_moves = [f for f in warns if f["check"] == "timing_moved"]
    L += ["## Numbers", "", "| | |", "|---|---|",
          f"| Cues | {len(cues)} |",
          f"| Words in the subtitle | {len(words)} |",
          f"| Words from recognition | {len(words) - rec.get('inserted', 0)} |",
          f"| Words recovered from skipped stretches | {rec.get('inserted', 0)} in {len(recovered)} {'stretch' if len(recovered) == 1 else 'stretches'} "
          f"({len(recovered) - len(short)} listed above, {len(short)} as short words) |",
          f"| Recovered words already in the subtitle, and filtered ({rec.get('noise') or 0} of them as noise) | {rec.get('already_present', 0)} and {rec.get('filtered', 0)} |",
          f"| Subtitle words recognized again somewhere else (listed above) | {sum(f['words'] for f in elsewhere)} in {len(elsewhere)} places |",
          f"| Timing moves over 5 s (listed above) | {len(big_moves)}, {sum(f['words'] for f in big_moves)} words |",
          f"| Runs of words inside silence (listed above) | {count('words_in_silence')} |",
          f"| Stretches not checked for missing speech (listed above) | {count('gap_not_checked') + len(unchecked)} |",
          f"| Stretches still without words after recovery (listed above) | {count('gap_still_empty')} |",
          f"| Stretches with no speech found | {len(quiet)} |",
          f"| Slow chunks (listed above) | {len(slow)} |",
          f"| Cue starts moved to keep cues in order | {cue_stats.get('pulled', 0) + cue_stats.get('pushed', 0)} |", ""]
    return sanitize("\n".join(L))

def gap_pass_legacy(cues):
    MIN_GAP, FLOOR = 0.08, 0.3
    for i in range(len(cues)-1):
        if cues[i][1] > cues[i+1][0]-MIN_GAP:
            t2 = cues[i+1][0]-MIN_GAP
            if t2 < cues[i][0]+FLOOR:
                t2 = min(max(cues[i][0]+FLOOR, cues[i+1][0]-0.001), cues[i+1][0]-0.001)
            if t2 < cues[i][1]: cues[i][1] = t2
    return cues

def ts(t):
    h, rest = divmod(max(0, ms(t)), 3600000)
    m, rest = divmod(rest, 60000)
    s, milli = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"

def srt_text(cues):
    return "".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t2}\n\n" for i,(a,b,t2) in enumerate(cues,1))

# ---------- one run ----------
class Episode:
    """Everything that touches the disk, ffmpeg or Google for one video."""

    def __init__(self, video, out_srt, work):
        self.video, self.out_srt, self.work = video, out_srt, work
        self.raw = f"{work}/raw"
        self.lang = _need("SE_STT_LANGUAGE")
        self.region = os.environ.get("SE_STT_REGION", "us").strip() or "us"
        self.model = os.environ.get("SE_STT_MODEL", "chirp_3").strip() or "chirp_3"
        # DYNAMIC_BATCHING is cheaper but queues behind Google's own load, sometimes for a long
        # time. Override to PROCESSING_STRATEGY_UNSPECIFIED (or omit the field) for immediate
        # processing at standard pricing when a run cannot wait out a busy queue.
        self.strategy = os.environ.get("SE_STT_PROCESSING_STRATEGY", "DYNAMIC_BATCHING").strip() or "DYNAMIC_BATCHING"
        # Optional. When set, every gcloud call is pinned to it, so a run never depends on whichever
        # account happens to be active. Register the key once with:
        #   gcloud auth activate-service-account --key-file=/path/to/key.json
        sa = os.environ.get("SE_STT_SERVICE_ACCOUNT", "").strip()
        self.account = [f"--account={sa}"] if sa else []
        # The pid keeps two runs started in the same second from sharing an upload path and
        # silently overwriting each other's audio.
        self.prefix = f"stt/{int(time.time())}-{os.getpid()}/"
        self.host = f"{self.region}-speech.googleapis.com"
        self.env = dict(os.environ, CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED="False")
        self.ffmpeg = self.ffprobe = self.full = None
        self.video_bytes = None
        self._deploy = None
        self._token, self._token_at = None, 0.0
        self.uploaded = False       # this run put audio under its prefix
        self.uploaded_uris = set()  # every object this run uploaded, so audio no operation needs can go at once
        self.claimed = False        # this run's prefix is listed in uploads.json
        self.pending = set()        # op files this run wrote or resumed that still await a response
        self.spans = []             # one record per recognized span, for the word accounting
        self.billed, self.fetched, self.reused = 0.0, 0, 0
        # Only prefetch_chunks() runs recognize() from more than one thread at a time; every other
        # caller is single-threaded and never contends on this. Guards the read-modify-write spots
        # a concurrent chunk fetch actually touches: the token cache, claim_prefix()'s check-and-set,
        # and the billed/fetched/reused counters.
        self._lock = threading.Lock()

    # ----- processes -----
    def run_proc(self, args):
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env)

    def sh(self, *a):
        """Run a command, and on failure report what it actually said rather than only its exit code."""
        proc = self.run_proc(a)
        if proc.returncode != 0:
            raise SystemExit(f"command failed ({proc.returncode}): {redact(' '.join(str(x) for x in a))}\n"
                             f"{redact((proc.stderr or proc.stdout).strip())[:800]}")
        return proc

    def duration(self, path):
        """Container duration in seconds, or None when ffprobe cannot tell (a half-written file says N/A)."""
        proc = self.run_proc([self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
        try: return float(proc.stdout.strip())
        except ValueError: return None

    def deploy(self):
        """Project, bucket and gcloud. Only a span that is not cached needs them, so a replay runs without."""
        if self._deploy is None:
            self._deploy = (_need("SE_STT_PROJECT"), _need("SE_STT_BUCKET"), _tool("SE_STT_GCLOUD", "gcloud"))
        return self._deploy

    # ----- audio -----
    def prepare_audio(self, total=None):
        """16 kHz mono flac of the whole episode, re-extracted when it cannot be trusted. A killed
        extraction used to leave a half-length full.flac that the next run silently reused. Work
        directories from before audio.json existed have no record to check against, so there a
        full.flac clearly shorter than the video is extracted again, once."""
        full, meta_path = f"{self.work}/full.flac", f"{self.work}/audio.json"
        meta = read_json(meta_path)
        have = self.duration(full) if os.path.exists(full) else None
        why = None
        if not os.path.exists(full):
            why = "none yet"
        elif isinstance(meta, dict) and meta.get("video_bytes") != self.video_bytes:
            why = "the video file changed"
        elif have is None:
            why = "the saved audio is incomplete"
        elif isinstance(meta, dict) and isinstance(meta.get("duration_s"), (int, float)) and abs(have - meta["duration_s"]) > 2.0:
            why = "the saved audio has the wrong length"
        elif not isinstance(meta, dict) and total is not None and have < total - 2.0:
            why = "the saved audio is shorter than the video"
        if why:
            log(f"extracting 16 kHz mono 16-bit flac ({why})")
            tmp = f"{self.work}/full.tmp.flac"
            self.sh(self.ffmpeg,"-y","-hide_banner","-loglevel","error","-i",self.video,"-vn","-ac","1","-ar","16000",
                    "-c:a","flac","-sample_fmt","s16","-compression_level","8",tmp)
            replace_retry(tmp, full)
            have = self.duration(full)
            if have is None:
                raise SystemExit("the extracted audio cannot be read back")
        if why or not isinstance(meta, dict):
            atomic_write(meta_path, json.dumps({"video_bytes": self.video_bytes, "duration_s": round(have, 3)}))
        return full, have

    def silences(self):
        """Every detected silence, and their midpoints as candidate cut points."""
        p = self.run_proc([self.ffmpeg,"-hide_banner","-i",self.full,"-af","silencedetect=noise=-30dB:d=0.35","-f","null","-"])
        starts = [float(m) for m in re.findall(r"silence_start: (-?[0-9.]+)", p.stderr)]
        ends   = [float(m) for m in re.findall(r"silence_end: (-?[0-9.]+)", p.stderr)]
        pairs = list(zip(starts, ends))
        return sorted((s + e) / 2 for s, e in pairs), pairs

    def cut(self, start, end, path):
        base = [self.ffmpeg,"-y","-hide_banner","-loglevel","error","-ss",f"{start:.3f}","-to",f"{end:.3f}",
                "-i",self.full,"-vn","-ac","1","-ar","16000","-c:a","flac","-sample_fmt","s16"]
        # ffmpeg's flac encoder rejects some cut offsets with "invalid block size" (about 1 in 20
        # random offsets); pinning the frame size fixes it and decoded samples are identical.
        if self.run_proc(base + ["-frame_size", "4096", path]).returncode != 0:
            self.sh(*base, path)
        return path

    # ----- Google -----
    def token(self, refresh=False):
        """One token per half hour instead of one gcloud process per request. A token gcloud cannot
        give is a RequestFailed without a status, like an unreachable Google: a resumed operation
        must not be given up, and recovery must not submit piece after piece, because of it."""
        with self._lock:
            if refresh or self._token is None or time.time() - self._token_at > 1800:
                gcloud = self.deploy()[2]
                try:
                    self._token = self.sh(gcloud, "auth", "print-access-token", *self.account).stdout.strip()
                except RequestFailed:
                    raise
                except SystemExit as e:
                    raise RequestFailed(f"could not get an access token: {e.code}")
                self._token_at = time.time()
            return self._token

    def api(self, method, url, body=None, attempts=5, timeout=120):
        """Retries rate limits, server errors and dropped connections, including a response lost on
        the way in (a TLS drop, a truncated body, a proxy's HTML page): one blip while polling used to
        end an hour-long run with a traceback. A POST is only sent again when it cannot have reached
        Google (the name did not resolve, the connection was refused) or Google answered 429 or 5xx:
        after a connection that closed, reset or timed out once the request was out, Google may
        already have started that operation, and a second one would be billed too. Errors never
        print the URL, which contains the project. Raises RequestFailed."""
        data = json.dumps(body).encode() if body is not None else None
        refreshed, attempt = False, 0
        while True:
            attempt += 1
            last = attempt >= attempts
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", "Bearer " + self.token())
            if data: req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    try:
                        return json.loads(r.read())
                    except (OSError, http.client.HTTPException, ValueError) as e:
                        if method != "GET" or last:
                            raise RequestFailed(f"{method} request failed: {lost_answer(e)}")
                time.sleep(5 * attempt)
                continue
            except urllib.error.HTTPError as e:
                try:
                    detail = redact(e.read().decode("utf-8", "replace"))[:300]
                except (OSError, http.client.HTTPException, ValueError):
                    detail = "(the error body could not be read)"
                if e.code == 401 and not refreshed:
                    self.token(refresh=True); refreshed = True; attempt -= 1; continue
                if e.code in RETRY_STATUS and not last:
                    time.sleep(5 * attempt); continue
                raise RequestFailed(f"{method} request failed with HTTP {e.code}: {detail}", e.code)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                unsent = isinstance(e, urllib.error.URLError) and (isinstance(e.reason, (socket.gaierror, ConnectionRefusedError)) or
                                                                   getattr(e.reason, "errno", None) in UNSENT_ERRNO)
                if not last and (method == "GET" or unsent):
                    time.sleep(5 * attempt); continue
                raise RequestFailed(f"{method} request failed: {lost_answer(e)}")

    def claim_prefix(self):
        """List this run's upload prefix in uploads.json BEFORE anything is uploaded under it.
        Closing the terminal or killing the process skips every cleanup, and audio that no list
        names stays in the bucket for good. Main thread only."""
        if self.claimed:
            return
        listed = self.uploads_list()
        if self.prefix not in listed:
            atomic_write(f"{self.work}/uploads.json", json.dumps(listed + [self.prefix]))
        self.claimed = True

    def upload(self, local, tag):
        bucket, gcloud = self.deploy()[1:]
        uri = f"gs://{bucket}/{self.prefix}{tag}.flac"
        self.uploaded = True    # before copying: a copy that dies halfway still gets cleaned up
        self.uploaded_uris.add(uri)
        self.sh(gcloud, "storage", "cp", "-q", *self.account, local, uri)
        return uri

    def submit(self, folder, tag, start, end, uri, attempts=1):
        project = self.deploy()[0]
        # As of 2026-09: the denoiser made no difference, speech adaptation together with word
        # offsets returned an internal error, and a custom prompt mis-timed words.
        # Re-test if the model changes.
        body = {"config":{"autoDecodingConfig":{},"languageCodes":[self.lang],"model":self.model,
                "features":{"enableWordTimeOffsets":True,"enableAutomaticPunctuation":True}},
                "files":[{"uri":uri}],"recognitionOutputConfig":{"inlineResponseConfig":{}},
                "processingStrategy":self.strategy}
        name = self.api("POST", f"https://{self.host}/v2/projects/{project}/locations/{self.region}/recognizers/_:batchRecognize", body)["name"]
        # Written right after submitting, so a crash or Ctrl+C resumes this operation instead of
        # paying for the same audio again. The prefix and uri say which uploaded audio it still needs.
        path = f"{folder}/{tag}.op.json"
        atomic_write(path, json.dumps({"name": name, "start_ms": ms(start), "end_ms": ms(end), "submitted": time.time(),
                                       "prefix": self.prefix, "uri": uri, "attempts": attempts}))
        self.pending.add(path)
        return name

    def poll(self, name, attempts=5, timeout=120):
        """The finished operation, or None while it is still running."""
        st = self.api("GET", f"https://{self.host}/v2/{name}", attempts=attempts, timeout=timeout)
        return st if st.get("done") else None

    def drop_op(self, path):
        self.pending.discard(path)
        if os.path.exists(path):
            remove_quietly(path)

    def resumable(self, folder, tag, start, end):
        """The operation an interrupted run left for exactly this span, if it is recent enough to poll:
        {name, submitted, attempts}, or None."""
        path = f"{folder}/{tag}.op.json"
        if not os.path.exists(path):
            return None
        op = read_json(path, {})
        try:
            ok = (isinstance(op.get("name"), str) and op["name"] and time.time() - float(op["submitted"]) < OP_MAX_AGE
                  and abs(int(op["start_ms"]) - ms(start)) <= 2 and abs(int(op["end_ms"]) - ms(end)) <= 2)
            attempts = max(1, int(op.get("attempts", 1)))
        except (AttributeError, KeyError, TypeError, ValueError):
            ok = False
        if not ok:
            self.drop_op(path)
            return None
        self.pending.add(path)
        return {"name": op["name"], "submitted": float(op["submitted"]), "attempts": attempts}

    def cached(self, folder, tag, start, end):
        """A saved response for exactly this span, checked BEFORE anything is cut or uploaded."""
        path = f"{folder}/{tag}.json"
        if os.path.exists(path):
            st = read_json(path)
            why = "it is unreadable" if st is None else cache_problem(st, ms(start), ms(end), self.lang, self.model, self.video_bytes)
            if why is None:
                self.reused += 1
                return st
            stale = f"{self.raw}/stale"
            os.makedirs(stale, exist_ok=True)
            n = 1
            while os.path.exists(f"{stale}/{tag}-{n}.json"): n += 1
            replace_retry(path, f"{stale}/{tag}-{n}.json")
            log(f"WARNING: the saved response for {tag} does not fit this run ({why}); it was moved to raw/stale")
        return self.set_aside(path, tag, start, end)

    def set_aside(self, path, tag, start, end):
        """A response an earlier run moved to raw/stale, used again when it fits this run: one run
        with the wrong language setting moved every good response aside, and the corrected run paid
        for all of them a second time."""
        def number(p):
            try:
                return int(p[:-5].rsplit("-", 1)[1])
            except (IndexError, ValueError):
                return 0
        for old in sorted(glob.glob(f"{self.raw}/stale/{glob.escape(tag)}-*.json"), key=number, reverse=True):
            st = read_json(old)
            if st is None or cache_problem(st, ms(start), ms(end), self.lang, self.model, self.video_bytes) is not None:
                continue
            try:
                replace_retry(old, path)
            except OSError:
                pass
            log(f"  {tag}: reusing a saved response that an earlier run had set aside")
            self.reused += 1
            return st
        return None

    def save_response(self, folder, tag, start, end, st):
        st["_span"] = {"start_ms": ms(start), "end_ms": ms(end), "language": self.lang,
                       "model": self.model, "video_bytes": self.video_bytes}
        atomic_write(f"{folder}/{tag}.json", json.dumps(st, ensure_ascii=False))
        with self._lock:
            self.fetched += 1
            self.billed += billed_seconds(st) or 0.0
        self.drop_op(f"{folder}/{tag}.op.json")

    def recognize(self, start, end, tag):
        """One span, cut, uploaded, submitted and polled until done, resuming an interrupted run.
        Gives up and raises Stalled after STALL_GIVE_UP_AFTER of the operation - fresh or resumed -
        never answering at all, so a restart cannot dodge the threshold by resetting the clock."""
        folder = self.raw
        op = self.resumable(folder, tag, start, end)
        name, resumed = (op["name"], True) if op else (None, False)
        started = float(op["submitted"]) if op else None
        if resumed:
            log(f"  {tag}: resuming the operation an interrupted run left")
        while True:
            if name is None:
                local = self.cut(start, end, f"{self.work}/{tag}.flac")
                self.deploy()
                self.claim_prefix()
                name = self.submit(folder, tag, start, end, self.upload(local, tag))
                if started is None:
                    started = time.time()
            time.sleep(CHUNK_POLL)
            if _STOP_EVENT.is_set():
                # Only matters when this call is running in a prefetch worker thread: the main
                # thread's own stop already raised Stopped there directly, same as before this
                # existed. A no-op for the ordinary single-threaded call.
                raise Stopped(_STOP_CODE[0])
            if time.time() - started > STALL_GIVE_UP_AFTER:
                self.drop_op(f"{folder}/{tag}.op.json")
                raise Stalled(start, end, tag)
            try:
                st = self.poll(name)
            except RequestFailed as e:
                # Only Google saying the operation is gone gives it up. A network blip, a server error or
                # a token gcloud could not give keeps the record for the next run: submitting again then
                # paid for audio Google had already billed.
                if not resumed or e.status not in GONE_STATUS:
                    raise
                self.drop_op(f"{folder}/{tag}.op.json")
                log(f"  {tag}: Google no longer has the interrupted operation (HTTP {e.status}), submitting again")
                name, resumed = None, False
                continue
            if st is None:
                continue
            problem = response_error(st)
            if problem:
                self.drop_op(f"{folder}/{tag}.op.json")
                if resumed:
                    log(f"  {tag}: the interrupted operation failed, submitting again")
                    name, resumed = None, False
                    continue
                raise SystemExit(f"recognition failed for {tag}: {problem}")
            self.save_response(folder, tag, start, end, st)
            return st

    def fetch_pieces(self, pieces):
        """Responses for recovery pieces: {tag: response, None when it failed, or "pending" while
        Google still works on it}. Cached pieces are reused; the rest are cut one by one, uploaded
        four at a time, submitted in order and polled together, so dozens of short operations do not
        each wait out their own sleep. Nothing here stops the run, and the wait is bounded: each
        round asks once per piece, REC_BAD_ROUNDS rounds without any answer end it (an outage kept 51
        pieces polling for 43 minutes against a 20 minute limit), an operation an earlier run left
        waits only for what is left of its REC_TIMEOUT (Google once held one piece for hours), and once
        all but a few of the pieces sent now have answered, the rest wait STRAGGLER_FACTOR times the
        usual answer time, at least REC_RESUME_WAIT: one piece Google never finished held every run of
        an episode for the whole 20 minutes. A piece left waiting is resumed by a later run."""
        folder = f"{self.raw}/rec"
        os.makedirs(folder, exist_ok=True)
        got, todo = {}, []
        for s, e, _, _ in pieces:
            tag = piece_tag(s, e)
            if tag in got:
                continue
            got[tag] = self.cached(folder, tag, s, e)
            if got[tag] is None:
                todo.append((tag, s, e))
        if not todo:
            return got
        self.deploy()
        # A token that cannot be had fails the whole stage once, before any audio is cut or uploaded,
        # instead of each piece failing on its own and every gap being listed as not checked.
        self.token()
        started = time.time()
        stage_end = started + REC_TIMEOUT
        span = {tag: (s, e) for tag, s, e in todo}
        ops, until, tries, fresh = {}, {}, {}, []
        for tag, s, e in todo:
            op = self.resumable(folder, tag, s, e)
            if op:
                ops[tag], tries[tag] = op["name"], op["attempts"]
                until[tag] = started + max(REC_RESUME_WAIT, REC_TIMEOUT - max(0.0, started - op["submitted"]))
            else:
                fresh.append(tag)
        resumed = set(ops)
        local_dir = f"{self.work}/rec"
        os.makedirs(local_dir, exist_ok=True)
        local = {}
        for tag in fresh:
            try:
                local[tag] = self.cut(span[tag][0], span[tag][1], f"{local_dir}/{tag}.flac")
            except (Exception, SystemExit) as ex:
                log(f"WARNING: could not cut recovery piece {clock(span[tag][0])}-{clock(span[tag][1])}: {redact(ex)}")
        uris = {}
        if local:
            self.claim_prefix()
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
            try:
                jobs = {pool.submit(self.upload, path, tag): tag for tag, path in local.items()}
                for job in concurrent.futures.as_completed(jobs):
                    tag = jobs[job]
                    try:
                        uris[tag] = job.result()
                    except (Exception, SystemExit) as ex:
                        log(f"WARNING: could not upload recovery piece {clock(span[tag][0])}-{clock(span[tag][1])}: {redact(ex)}")
                    remove_quietly(local[tag])
            except BaseException:
                # Ctrl+C or a kill: finish the copies already running, drop the queued ones. Leaving the
                # block through a plain shutdown uploaded every queued piece first, only to delete it.
                pool.shutdown(wait=True, cancel_futures=True)
                raise
            pool.shutdown(wait=True)
        unreachable = 0
        sent_at, answered_in = {}, []
        for tag in fresh:
            if tag not in uris:
                continue
            s, e = span[tag]
            if unreachable >= 2:
                log(f"WARNING: recovery piece {clock(s)}-{clock(e)} was not submitted, Google could not be reached")
                continue
            try:
                ops[tag] = self.submit(folder, tag, s, e, uris[tag])
                tries[tag], until[tag], unreachable = 1, stage_end, 0
                sent_at[tag] = time.time()
            except (Exception, SystemExit) as ex:
                if isinstance(ex, RequestFailed) and (ex.status is None or ex.status in RETRY_STATUS):
                    unreachable += 1
                log(f"WARNING: could not submit recovery piece {clock(s)}-{clock(e)}: {redact(ex)}")
        if ops:
            log(f"  waiting for {len(ops)} recovery piece(s)")
        bad_rounds = 0
        while ops and any(until[tag] > time.time() for tag in ops):
            time.sleep(REC_POLL)
            asked = silent = 0
            for tag in sorted(ops):
                left = until[tag] - time.time()
                if left <= 0:
                    continue
                s, e = span[tag]
                asked += 1
                try:
                    st = self.poll(ops[tag], attempts=1, timeout=max(5.0, min(REC_POLL_TIMEOUT, left)))
                    problem = response_error(st) if st is not None else None
                except (Exception, SystemExit) as ex:
                    if not isinstance(ex, RequestFailed) or ex.status is None or ex.status in RETRY_STATUS:
                        silent += 1
                        if silent == asked >= REC_BAD_ROUNDS:
                            break         # a stalled connection costs each request its whole timeout
                        continue          # network or server trouble: the next round asks again
                    st, problem = None, f"its status could not be read ({redact(ex)})"
                if st is None and problem is None:
                    continue
                del ops[tag]
                if tag in sent_at:
                    answered_in.append(time.time() - sent_at.pop(tag))
                if not problem:
                    self.save_response(folder, tag, s, e, st)
                    got[tag] = st
                    continue
                self.drop_op(f"{folder}/{tag}.op.json")
                if tag not in resumed or tries[tag] >= MAX_PIECE_ATTEMPTS:
                    log(f"WARNING: recovery piece {clock(s)}-{clock(e)} failed: {problem}")
                    continue
                # an operation left by an interrupted run is gone or failed: start it again, once
                resumed.discard(tag)
                log(f"  recovery piece {clock(s)}-{clock(e)}: the earlier operation failed ({problem}), submitting again")
                try:
                    path = self.cut(s, e, f"{local_dir}/{tag}.flac")
                    self.claim_prefix()
                    try:
                        uri = self.upload(path, tag)
                    finally:
                        remove_quietly(path)
                    tries[tag] += 1
                    ops[tag] = self.submit(folder, tag, s, e, uri, attempts=tries[tag])
                    until[tag] = max(stage_end, time.time() + REC_RESUME_WAIT)
                except (Exception, SystemExit) as ex:
                    ops.pop(tag, None)
                    log(f"WARNING: could not submit recovery piece {clock(s)}-{clock(e)} again: {redact(ex)}")
            slow = [tag for tag in ops if tag in sent_at]
            if slow and len(answered_in) >= 3 and len(slow) <= max(1, (len(slow) + len(answered_in)) // 10):
                cap = time.time() + max(REC_RESUME_WAIT, STRAGGLER_FACTOR * statistics.median(answered_in))
                if any(until[tag] > cap for tag in slow):
                    log(f"  {some(len(slow), 'recovery piece')} {'is' if len(slow) == 1 else 'are'} much slower than the rest, "
                        f"waiting at most {cap - time.time():.0f} s more")
                for tag in slow:
                    until[tag] = min(until[tag], cap)
            if asked and silent == asked:
                bad_rounds += 1
                if bad_rounds >= REC_BAD_ROUNDS:
                    log(f"WARNING: Google did not answer for {bad_rounds} rounds in a row; {len(ops)} recovery piece(s) are left for a later run")
                    break
            else:
                bad_rounds = 0
        if ops:
            log(f"WARNING: {len(ops)} recovery piece(s) did not finish in time; a later run picks them up")
            for tag in ops:
                got[tag] = "pending"
        return got

    def span_response(self, start, end, tag):
        st = self.cached(self.raw, tag, start, end)
        if st is not None:
            log(f"  {tag}: reusing saved response")
            return st
        return self.recognize(start, end, tag)

    def prefetch_chunks(self, bounds):
        """Ask Google for every top-level chunk's first-pass recognition at once, before the
        sequential pass below asks for them one at a time. Each answer lands in the same on-disk
        cache span_response() already checks first, so a chunk that turns out not to need a
        re-cut is instant there; only the rare anomalous chunk still re-cuts and fetches its own
        halves one at a time, exactly as before - this only changes how the FIRST answer for each
        chunk is obtained, never what transcribe_span decides to do with it.

        Unlike a recovery piece, a chunk here is never silently abandoned: recognize() itself
        already polls until it succeeds, gives up with Stalled, or fatally fails, whichever thread
        runs it. This only runs several of those calls at once instead of one after another, and
        logs one WARNING (well before any give-up) per chunk that is taking unusually long, since
        nothing outside the process was watching for that until now. A Stalled chunk is not treated
        as fatal here - it is simply not cached yet when this returns, so the sequential pass below
        asks for it again through span_response(), and transcribe_span re-cuts it if it stalls again."""
        tags = [(f"part-{i:03d}", bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
        todo = [(tag, s, e) for tag, s, e in tags if self.cached(self.raw, tag, s, e) is None]
        if len(todo) <= 1:
            return   # nothing to gain from concurrency for zero or one chunk left to fetch
        # Claimed once, up front, on this thread: claim_prefix() is main-thread-only by contract,
        # and every worker below calls it again through recognize(), where it is now always a
        # cheap no-op read of self.claimed rather than a second, racing check-and-set.
        self.deploy()
        self.claim_prefix()
        log(f"  prefetching {len(todo)} chunk(s) concurrently")
        started = time.time()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(PREFETCH_WORKERS, len(todo)))
        try:
            jobs = {pool.submit(self.recognize, s, e, tag): (tag, time.time()) for tag, s, e in todo}
            pending, warned = set(jobs), set()
            while pending:
                done, pending = concurrent.futures.wait(pending, timeout=CHUNK_POLL,
                                                         return_when=concurrent.futures.FIRST_COMPLETED)
                for job in done:
                    try:
                        job.result()
                    except Stalled as e:
                        tag = jobs[job][0]
                        log(f"  {tag}: STALLED during prefetch, will be re-cut in the sequential pass ({e})")
                now = time.time()
                for job in pending:
                    tag, since = jobs[job]
                    elapsed = now - since
                    if elapsed > STALL_WARN_AFTER and tag not in warned:
                        log(f"WARNING: {tag} has been pending {elapsed/60:.0f} min with no result from "
                            f"Google yet, unusually slow (still waiting, nothing here is skipped)")
                        warned.add(tag)
        finally:
            # Always waited out, never cancelled: a worker thread left running after this function
            # returns would still be polling Google with no one watching it, and cancel_futures only
            # drops queued work anyway, not a call already in flight. _STOP_EVENT (set by _stop(),
            # checked inside recognize()'s own loop) is what makes an interrupt here end promptly.
            pool.shutdown(wait=True)
        log(f"  prefetch done in {(time.time() - started) / 60:.1f} min")

    # ----- chunks -----
    def transcribe_span(self, start, end, tag, depth=0, mid_speech=False):
        """Recognize one span, re-cutting it once if the result looks anomalous. mid_speech marks a
        truncation tail, which starts on speech rather than in silence.
        Returns (words, events, loops), all in span time."""
        dur = end - start
        try:
            st = self.span_response(start, end, tag)
        except Stalled:
            # Google never answered at all, not just a bad one - re-cut and try smaller regardless
            # of depth or mid_speech, down to STALL_RECUT_FLOOR. Below that, stop guessing: report
            # the gap instead of re-cutting forever or reaching for a different source for it.
            if dur <= STALL_RECUT_FLOOR:
                log(f"WARNING: {tag} ({clock(start)}-{clock(end)}) never answered even at "
                    f"{dur:.0f} s; leaving this stretch unrecovered rather than guess at it")
                return [], [], []
            mid = quiet_midpoint(self.quiet, start, end)
            log(f"  {tag}: STALLED, re-cutting at {mid/60:.1f} min")
            aw, ae, al = self.transcribe_span(start, mid, tag+"a", depth+1, mid_speech=mid_speech)
            bw, be, bl = self.transcribe_span(mid, end, tag+"b", depth+1)
            off = mid - start
            return aw + shifted(bw, off), ae + shifted_events(be, off), al + shifted_events(bl, off)
        raw, filled = words_from_raw(st), words_from(st)
        record = {"tag": tag, "start": start, "end": end, "words": len(filled), "looped": 0, "dropped": 0, "used": True}
        self.spans.append(record)
        loops = []
        # collapse compares text only, so the raw offsets ride along with the filled ones
        col, looped = collapse([f + r[:2] for f, r in zip(filled, raw)], loops)
        record["looped"] = looped
        # Decisions that spend money stay on the previous placement: the new repair moves fewer
        # words, and on a measured chunk it would have skipped a re-cut that was needed.
        legacy, fixed = repair_legacy([c[:3] for c in col], dur)
        covered = max((w[1] for w in legacy), default=0.0)

        bad_ratio = fixed / max(1, len(legacy))
        anomalous = looped > 40 or bad_ratio > 0.15
        # A truncation tail (mid_speech) can be as troubled as the chunk it continues, and re-
        # cutting it once does not always settle it: one 924 s tail (depth 1) looped 224 of 627
        # words, and its own re-cut half (depth 2) was STILL anomalous (46 of 275) and packed a
        # real 52-word passage into cues a few milliseconds long, because only depth 0 and 1 got
        # this same one-time-per-span re-cut a fresh chunk gets. A tail now gets it at any depth;
        # dur > 240 already halves away to nothing within a few rounds, and MAX_RECUT_DEPTH bounds
        # it explicitly too. A plain split half still only gets it once, at depth 0, which leaves
        # that case exactly as measured (a broader version of this once let a split half re-cut a
        # second time, and it changed an already-shipped episode's output that had never needed
        # one).
        if anomalous and (depth == 0 or (mid_speech and depth <= MAX_RECUT_DEPTH)) and dur > 240:
            mid = quiet_midpoint(self.quiet, start, end)
            log(f"  {tag}: ANOMALY ({looped} looped, {bad_ratio*100:.0f}% timings bad), re-cutting at {mid/60:.1f} min")
            record["used"] = False
            # The first half starts exactly where this span did, so a tail's still starts on
            # speech rather than silence; the second half starts fresh at a chosen quiet point.
            aw, ae, al = self.transcribe_span(start, mid, tag+"a", depth+1, mid_speech=mid_speech)
            bw, be, bl = self.transcribe_span(mid, end, tag+"b", depth+1)
            off = mid - start
            return aw + shifted(bw, off), ae + shifted_events(be, off), al + shifted_events(bl, off)

        placed, found = place_words(col, legacy, dur, tag, head=not mid_speech)
        words, keep = list(placed), range(len(placed))
        tail_events, tail_loops = [], []
        # covered > 30 means the span actually contains speech that stopped early. Without it,
        # a silent tail (end credits) resumes at 0.0, re-cuts the identical span, and loops.
        if dur > 60 and covered < dur*0.90 and dur-covered > 20 and covered > 30 and depth < 2:
            resume = max(0.0, covered-1.0)
            log(f"  {tag}: truncated at {covered:.0f}/{dur:.0f}s, recovering tail")
            tw, te_, tl = self.transcribe_span(start+resume, end, tag+"t", depth+1, mid_speech=True)
            # Which parent words survive is decided on the previous placement, so the word sequence
            # stays what it was; the survivors then take their repaired times.
            keep = [k for k, w in enumerate(legacy) if w[1] <= resume+0.5]
            record["dropped"] = len(legacy) - len(keep)
            words = [placed[k] for k in keep] + shifted(tw, resume)
            tail_events, tail_loops = shifted_events(te_, resume), shifted_events(tl, resume)

        keep = set(keep)
        events = []
        for ev in found:
            idx = [k for k in ev["idx"] if k in keep]
            if idx:
                events.append({"kind": ev["kind"], "start": min(placed[k][0] for k in idx),
                               "end": max(placed[k][1] for k in idx), "words": len(idx), "shift_s": ev["shift_s"]})
        log(f"  {tag}: {len(words)} words, {looped} looped removed, {fixed} timings repaired")
        return words, events + tail_events, loops + tail_loops

    # ----- cleanup -----
    def uploads_list(self):
        listed = read_json(f"{self.work}/uploads.json", [])
        return [p for p in listed if isinstance(p, str)] if isinstance(listed, list) else []

    def remove_prefix(self, prefix):
        """True when nothing is left under the prefix. Never raises."""
        try:
            bucket, gcloud = self.deploy()[1:]
            proc = self.run_proc([gcloud, "storage", "rm", "-q", "--recursive", *self.account, f"gs://{bucket}/{prefix}"])
        except (Exception, SystemExit) as e:
            log(f"WARNING: could not remove uploaded audio: {redact(e)}")
            return False
        said = (proc.stderr or proc.stdout or "").lower()
        if proc.returncode == 0 or "matched no objects" in said:
            return True
        log(f"WARNING: could not remove uploaded audio: {redact(said.strip().splitlines()[-1] if said.strip() else proc.returncode)}")
        return False

    def remove_objects(self, uris):
        """Remove single uploaded objects, a few dozen per gcloud call. True when none is left. Never raises."""
        ok = True
        for k in range(0, len(uris), 40):
            try:
                gcloud = self.deploy()[2]
                proc = self.run_proc([gcloud, "storage", "rm", "-q", *self.account, *uris[k:k + 40]])
            except (Exception, SystemExit) as e:
                log(f"WARNING: could not remove uploaded audio: {redact(e)}")
                return False
            said = (proc.stderr or proc.stdout or "").lower()
            if proc.returncode != 0 and "matched no objects" not in said:
                log(f"WARNING: could not remove uploaded audio: {redact(said.strip().splitlines()[-1] if said.strip() else proc.returncode)}")
                ok = False
        return ok

    def waiting_ops(self):
        """(path, record) of every operation that may still be waiting on Google. A record that can never
        be resumed is removed: older than OP_MAX_AGE, or next to a saved response for the same span (a
        rerun that reused the response never looks at the record, which then held the audio and warned
        on every run). Records this run is still waiting on are always kept. Unreadable records are
        skipped, as before."""
        mine = {os.path.normpath(p) for p in self.pending}
        out = []
        raw = glob.escape(self.raw)     # a work directory named "Show [1080p]" hid every record
        for path in glob.glob(f"{raw}/*.op.json") + glob.glob(f"{raw}/rec/*.op.json"):
            op = read_json(path, {})
            if os.path.normpath(path) in mine:
                out.append((path, op if isinstance(op, dict) else {}))
                continue
            try:
                age = time.time() - float(op["submitted"])
                span = (int(op["start_ms"]), int(op["end_ms"]))
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            if age >= OP_MAX_AGE:
                remove_quietly(path)
                continue
            saved = read_json(path[:-len(".op.json")] + ".json")
            if saved is not None and cache_problem(saved, span[0], span[1], self.lang, self.model, self.video_bytes) is None:
                remove_quietly(path)
                continue
            out.append((path, op))
        return out

    def prefixes_in_use(self):
        """Upload prefixes that operations still waiting on Google read their audio from, or None when
        a waiting operation's record does not say which. Records older than OP_MAX_AGE are never
        resumed, so they hold nothing back."""
        used = {self.prefix} if self.pending else set()
        for _, op in self.waiting_ops():
            if not isinstance(op.get("prefix"), str):
                return None
            used.add(op["prefix"])
        return used

    def cleanup(self):
        """Uploaded audio is removed once no operation needs it: every listed prefix, this run's
        included, unless a waiting operation still reads from it. Then only this run's objects that
        no waiting operation names are removed, and the prefix stays listed for a later run. Never raises."""
        try:
            listed = self.uploads_list()
            candidates = listed + ([self.prefix] if (self.uploaded or self.claimed) and self.prefix not in listed else [])
            if not candidates:
                return
            waiting = self.waiting_ops()
            used = None if any(not isinstance(op.get("prefix"), str) for _, op in waiting) else \
                {op["prefix"] for _, op in waiting} | ({self.prefix} if self.pending else set())
            keep = [p for p in candidates if used is None or p in used]
            gone, left = [], []
            for p in candidates:
                if p not in keep:
                    (gone if self.remove_prefix(p) else left).append(p)
            if self.prefix in keep and self.uploaded_uris and used is not None:
                recorded = {os.path.normpath(path) for path, _ in waiting}
                needs = [op for _, op in waiting if op.get("prefix") == self.prefix]
                if all(isinstance(op.get("uri"), str) for op in needs) and all(os.path.normpath(p) in recorded for p in self.pending):
                    spare = sorted(self.uploaded_uris - {op["uri"] for op in needs})
                    if spare and self.remove_objects(spare):
                        log(f"removed uploaded audio that no pending operation needs ({some(len(spare), 'file')})")
            if keep:
                # Operations still running read their audio from the bucket, so keep it and let
                # a later run in this work directory that finishes them remove it.
                log(f"WARNING: recognition is still pending; the audio it reads stays in the bucket until a later run "
                    f"in this work directory finishes it (records expire after {OP_MAX_AGE / 3600:.0f} h)")
            earlier = [p for p in gone if p != self.prefix]
            if earlier:
                log(f"removed audio that {len(earlier)} earlier run(s) left in the bucket")
            if self.prefix in gone and self.uploaded:
                log("removed uploaded audio")
            rest = keep + left      # a later run tries again
            if rest != listed:
                atomic_write(f"{self.work}/uploads.json", json.dumps(rest))
        except (Exception, SystemExit) as e:
            log(f"WARNING: bucket cleanup failed: {redact(e)}")

    # ----- the whole run -----
    def run(self):
        os.makedirs(self.raw, exist_ok=True)
        if not [p for p in glob.glob(f"{glob.escape(self.raw)}/*.json") if not p.endswith(".op.json")]:
            self.deploy()   # nothing cached, so fail on missing settings before any ffmpeg work
        self.ffmpeg  = _tool("SE_STT_FFMPEG",  "ffmpeg",  "/opt/homebrew/bin/ffmpeg",  "/usr/local/bin/ffmpeg",  "/usr/bin/ffmpeg")
        self.ffprobe = _tool("SE_STT_FFPROBE", "ffprobe", "/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "/usr/bin/ffprobe")
        self.video_bytes = os.path.getsize(self.video)
        total = float(self.sh(self.ffprobe,"-v","error","-show_entries","format=duration","-of","csv=p=0",self.video).stdout.strip())
        log(f"episode {total/60:.1f} min")
        self.full, full_dur = self.prepare_audio(total)

        log("detecting silence for chunk boundaries")
        self.quiet, self.silence = self.silences()
        log(f"{len(self.quiet)} silences found")
        bounds = chunk_bounds(total, self.quiet)
        snapped = sum(1 for i,b in enumerate(bounds[1:-1],1) if abs(b - i*CHUNK) > 0.01)
        log(f"{len(bounds)-1} chunks, {snapped} boundaries moved into silence")

        # full.flac can be a little shorter than the container; pieces cut past its end come back empty
        audio_end = min(total, full_dur)
        try:
            if os.environ.get("SE_STT_PREFETCH", "").strip() != "0":
                self.prefetch_chunks(bounds)
            before, self.timing_events, self.loops = [], [], []
            for i in range(len(bounds)-1):
                s, e = bounds[i], bounds[i+1]
                log(f"chunk {i+1}/{len(bounds)-1} ({s/60:.1f}-{e/60:.1f} min)")
                got, events, loops = self.transcribe_span(s, e, f"part-{i:03d}")
                before.extend((w[0]+s, w[1]+s, w[2]) for w in got)
                self.timing_events += shifted_events(events, s)
                self.loops += shifted_events(loops, s)

            # Count only the responses that actually fed the subtitle, from this run's own records:
            # a chunk that was re-cut is SUPERSEDED by its two halves, and stale files from earlier
            # runs in raw/ must not count either, or the check reports a loss that never happened.
            used = [r for r in self.spans if r["used"]]
            google_total = sum(r["words"] for r in used)
            looped = sum(r["looped"] for r in used)
            dropped = sum(r["dropped"] for r in used)
            if google_total - looped - dropped != len(before):
                log(f"WARNING: internal word accounting mismatch ({google_total} from Google - {looped} looped - "
                    f"{dropped} dropped at tail splices != {len(before)} words)")

            words, rec = before, empty_recovery()
            if os.environ.get("SE_STT_RECOVER", "").strip() == "0":
                rec["status"] = "disabled"
            else:
                try:
                    log(f"looking for stretches of {gap_min():.0f} s or more without words")
                    starts = sorted({r["start"] for r in self.spans if not r["tag"].endswith("t")})
                    words, rec = recover_holes(before, audio_end, self.lang, self.fetch_pieces, self.loops, self.silence,
                                               starts, self.timing_events)
                except (Exception, SystemExit) as e:
                    log(f"WARNING: hole recovery failed: {redact(e)}")
                    words, rec = before, dict(empty_recovery(), status="failed")

            cues, cue_stats, builder = self.subtitle(words, total)
            atomic_write(self.out_srt, srt_text(cues))
            if rec["inserted"]:
                try:
                    atomic_write(f"{self.work}/before-recovery.srt", srt_text(self.subtitle(before, total)[0]))
                except Exception as e:
                    log(f"WARNING: could not write the subtitle without recovered words: {redact(e)}")
            self.finish(cues, words, total, audio_end, bounds, snapped, rec, cue_stats, builder, google_total, looped, dropped)
        finally:
            self.cleanup()

    def subtitle(self, words, total):
        try:
            return build_subtitle(words, self.lang, total)
        except Exception as e:
            log(f"WARNING: cue timing pass failed ({redact(e)}), using the previous cue timing")
            cues = gap_pass_legacy([c[:3] for c in cues_from_groups(words, cue_groups_legacy(words))])
            return cues, {"extended": 0, "pulled": 0, "pushed": 0, "moves": []}, "legacy"

    def finish(self, cues, words, total, audio_end, bounds, snapped, rec, cue_stats, builder, google_total, looped, dropped):
        """Checks, report.json, editor notes and the closing log lines. All best-effort: the
        subtitle is already written."""
        try:
            verify = run_checks(cues, words, total, audio_end, self.silence, self.timing_events, rec, bounds)
        except Exception as e:
            log(f"WARNING: checks failed: {redact(e)}")
            verify = {"status": "unknown", "fails": [], "warns": [], "notes": []}
        speech = speech_seconds(words)
        def timed(items):
            out = []
            for x in items:
                y = dict(x, start_hms=clock(x["start"]), end_hms=clock(x["end"]))
                for k in ("start", "end", "gap_start", "gap_end", "run_start", "run_end"):
                    if isinstance(y.get(k), float):
                        y[k] = round(y[k], 2)
                out.append(y)
            return out
        notes_path = os.path.splitext(self.out_srt)[0] + " - transcription notes.md"
        report_path = f"{self.work}/report.json"
        try:
            recovery = {k: rec.get(k) for k in ("status", "gaps_checked", "pieces", "audio_s", "inserted", "already_present", "filtered", "noise")}
            recovery.update(skipped=timed(rec.get("skipped", [])), displaced_runs=timed(rec.get("displaced_runs", [])),
                            misplaced=timed(rec.get("misplaced", [])), inserted_ranges=timed(rec.get("inserted_ranges", [])),
                            still_empty=timed(rec.get("still_empty", [])))
            report = {
                # A file name can carry a series name and reports get shared; the work directory says which video.
                "video": "(name not recorded)",
                "minutes": round(total/60, 2), "chunks": len(bounds)-1, "boundaries_snapped_to_silence": snapped,
                "google_words": google_total, "subtitle_words": len(words), "cues": len(cues),
                "speech_density_pct": round(speech/total*100, 1) if total else 0.0,
                "verify": verify,
                "recovery": recovery,
                "timing_events": timed([{k: ev[k] for k in ("start", "end", "kind", "words", "shift_s")} for ev in self.timing_events]),
                "cue_pass": {"extended": cue_stats.get("extended", 0), "pulled": cue_stats.get("pulled", 0),
                             "pushed": cue_stats.get("pushed", 0), "builder": builder,
                             "moves": [dict(m, start=round(m["start"], 3), start_hms=clock(m["start"])) for m in cue_stats.get("moves", [])]},
                "billed_seconds_this_run": round(self.billed, 1),
                "responses_reused": self.reused,
                "responses_fetched": self.fetched,
                "pending_operations": len(self.pending),
                "builder_version": BUILDER_VERSION,
                "settings": {"language": self.lang, "model": self.model, "gap_min_s": gap_min()},
            }
            atomic_write(report_path, json.dumps(report, indent=2, ensure_ascii=False))
        except Exception as e:
            log(f"WARNING: could not write report.json: {redact(e)}")
        notes_written = False
        if os.environ.get("SE_STT_NOTES", "").strip() != "0":
            try:
                atomic_write(notes_path, render_notes(cues, words, total / 60, verify, rec, self.timing_events, cue_stats))
                notes_written = True
            except Exception as e:
                log(f"WARNING: could not write the transcription notes: {redact(e)}")

        log(f"VERIFY: Google returned {google_total} words, {looped} looped removed, {dropped} dropped at tail splices, "
            f"subtitle carries {len(words)} ({rec.get('inserted', 0)} recovered)")
        status = rec.get("status")
        if status == "ran":
            reasons = [g["reason"] for g in rec["skipped"]]
            why = [f"{reasons.count(r)} {text}" for r, text in (("budget", "over budget"), ("recognition failed", "failed"),
                                                               ("still running", "still running")) if r in reasons]
            log(f"RECOVERY: {rec['gaps_checked']} gaps checked, {rec['pieces']} pieces ({rec['audio_s']:.0f} s of audio), "
                f"{rec['inserted']} words inserted, {rec['already_present']} already present, {rec['filtered']} filtered, "
                f"{len(rec['skipped'])} gaps skipped" + (f" ({', '.join(why)})" if why else ""))
        elif status == "disabled":
            log("RECOVERY: disabled by SE_STT_RECOVER=0")
        elif status == "too little speech recognized":
            log("RECOVERY: skipped, too little speech recognized")
        else:
            log("RECOVERY: failed, no words inserted")
        for ev in sorted(self.timing_events, key=lambda x: x["start"]):
            if abs(ev["shift_s"]) > MOVE_WARN_S:
                log(f"TIMING: {clock(ev['start'])}-{clock(ev['end'])} {some(ev['words'], 'word')} {MOVE_TEXT.get(ev['kind'], '')} moved {ev['shift_s']:+.1f} s")
        log(f"CHECKS: {verify['status']} ({len(verify['fails'])} fail, {len(verify['warns'])} warn, {len(verify['notes'])} note)")
        # Operations still running are billed when they finish, so "nothing billed" must not hide them.
        waiting = f"{len(self.pending)} operation(s) still pending, billed when they finish" if self.pending else ""
        if self.fetched:
            log(f"billed {self.billed:.0f} s (about ${self.billed/60*0.003:.2f})" + (f"; {waiting}" if waiting else ""))
        elif waiting:
            log(f"nothing billed yet; {waiting}")
        else:
            log("all responses reused, nothing billed")
        log(f"wrote {len(cues)} cues to {self.out_srt} | speech density {speech/total*100:.1f}%" if total else f"wrote {len(cues)} cues to {self.out_srt}")
        if notes_written:
            log(f"notes: {notes_path}")
        log(f"report: {report_path}")

def _stop(signum, frame):
    _STOP_CODE[0] = 128 + signum
    _STOP_EVENT.set()
    raise Stopped(128 + signum)

USAGE = "usage: transcribe-episode.py <video> [output.srt] [work-dir]"

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass
    # Closing the terminal or a kill ends the process without unwinding; as an exception the run
    # still records what it uploaded and cleans up. The exception is Stopped, which no best-effort
    # handler catches, so the run stops instead of carrying on, and exits with 128 plus the signal.
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _stop)
            except (ValueError, OSError):
                pass
    if len(argv) < 1:
        raise SystemExit(USAGE)
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return
    video = argv[0]
    if not os.path.isfile(video):
        raise SystemExit(f"video not found: {video}")
    out_srt = argv[1] if len(argv) > 1 else os.path.splitext(video)[0] + ".srt"
    # Keyed to the video so re-running an episode reuses its cached responses and costs nothing,
    # while two different episodes never share a work directory.
    slug = "".join(c if c.isalnum() else "-" for c in os.path.basename(video))[:60]
    work = argv[2] if len(argv) > 2 else os.path.expanduser(f"~/.cache/se-stt/{slug}")
    load_config()
    try:
        Episode(video, out_srt, work).run()
    except Stopped as stop:
        raise SystemExit(stop.code)

if __name__ == "__main__":
    main()
