#!/usr/bin/env python3
"""Transcribe a mixed Arabic and English broadcast with Google Cloud Speech-to-Text v2 (chirp_3).

One file, three recognition jobs, then one compiled result:

  ar   the whole file recognized as Arabic (ar-XA), through the same protected pipeline the episode
       tool uses: silence-cut chunks, anomaly re-cuts, hole recovery, timing repair
  en   the same, recognized as English (en-US)
  dz   English (en-US) recognized again in overlapping windows with speaker diarization on. Only its
       speaker labels are used: chirp_3 supports diarization for English but not for any Arabic
       locale, and a speaker's voice does not depend on the language the recognizer was told to
       expect, so the labels can be laid over the words of both languages. Windows overlap so a
       speaker can be followed from one window to the next.

Compiled outputs, next to the chosen base name: <base>.words.json, <base>.events.json, <base>.srt.

usage: transcribe-broadcast.py <audio-or-video> [output-base] [work-dir]
"""
import importlib.util, json, os, re, shutil, signal, subprocess, sys, threading, time, concurrent.futures

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("episode_tool", os.path.join(HERE, "transcribe-episode.py"))
te = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(te)

LANGS = {"ar": "ar-XA", "en": "en-US"}
JOBS = ("ar", "en", "dz")
DIAR_WIN = 180.0       # audio per diarization request: long requests come back cut short (measured: a 16 minute one
DIAR_OVERLAP = 60.0    # stopped after 84 s), short ones are complete, and short windows overlap so speakers can be followed
DIAR_MIN = 60.0        # a window that still comes back cut short is split until its halves would be shorter than this
LEAK = re.compile(r"^(sp|k:\d+|speaker:?\d*|s?peaker)$", re.I)     # the labeller's own text, cut off mid-word, in the word list
ZONE_MARGIN_S = 8.0


class Engine(te.Episode):
    """The episode tool's Episode, told which language to expect. Its own work folder, upload prefix and
    response cache, so several jobs of one file never clean up or overwrite each other."""

    def __init__(self, media, work, lang, tag, diarize=False):
        super().__init__(media, None, work)
        self.lang, self.diarize, self.tag = lang, diarize, tag
        self.prefix = f"stt/{int(time.time())}-{os.getpid()}-{tag}/"

    def submit(self, folder, tag, start, end, uri, attempts=1):
        project = self.deploy()[0]
        features = {"enableWordTimeOffsets": True, "enableAutomaticPunctuation": True}
        if self.diarize:
            features["diarizationConfig"] = {}
        body = {"config": {"autoDecodingConfig": {}, "languageCodes": [self.lang], "model": self.model, "features": features},
                "files": [{"uri": uri}], "recognitionOutputConfig": {"inlineResponseConfig": {}},
                "processingStrategy": self.strategy}
        name = self.api("POST", f"https://{self.host}/v2/projects/{project}/locations/{self.region}/recognizers/_:batchRecognize", body)["name"]
        path = f"{folder}/{tag}.op.json"
        te.atomic_write(path, json.dumps({"name": name, "start_ms": te.ms(start), "end_ms": te.ms(end), "submitted": time.time(),
                                          "prefix": self.prefix, "uri": uri, "attempts": attempts}))
        self.pending.add(path)
        return name

    def setup(self):
        os.makedirs(self.raw, exist_ok=True)
        self.ffmpeg = te._tool("SE_STT_FFMPEG", "ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg")
        self.ffprobe = te._tool("SE_STT_FFPROBE", "ffprobe", "/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "/usr/bin/ffprobe")
        self.video_bytes = os.path.getsize(self.video)
        total = float(self.sh(self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", self.video).stdout.strip())
        self.full, full_dur = self.prepare_audio(total)
        self.quiet, self.silence = self.silences()
        return total, min(total, full_dur)

    # ----- ar / en: the protected pipeline up to the final words -----
    def run_engine(self):
        self.raw = f"{self.work}/raw"
        total, audio_end = self.setup()
        te.log(f"[{self.tag}] {total/60:.1f} min, {len(self.quiet)} silences")
        bounds = te.chunk_bounds(total, self.quiet)
        te.log(f"[{self.tag}] {len(bounds)-1} chunks")
        try:
            self.prefetch_chunks(bounds)
            before, self.timing_events, self.loops = [], [], []
            for i in range(len(bounds) - 1):
                s, e = bounds[i], bounds[i + 1]
                te.log(f"[{self.tag}] chunk {i+1}/{len(bounds)-1} ({s/60:.1f}-{e/60:.1f} min)")
                got, events, loops = self.transcribe_span(s, e, f"part-{i:03d}")
                before.extend((w[0] + s, w[1] + s, w[2]) for w in got)
                self.timing_events += te.shifted_events(events, s)
                self.loops += te.shifted_events(loops, s)
            used = [r for r in self.spans if r["used"]]
            google_total = sum(r["words"] for r in used)
            looped = sum(r["looped"] for r in used)
            dropped = sum(r["dropped"] for r in used)
            words, rec = before, te.empty_recovery()
            try:
                starts = sorted({r["start"] for r in self.spans if not r["tag"].endswith("t")})
                words, rec = te.recover_holes(before, audio_end, self.lang, self.fetch_pieces, self.loops, self.silence, starts, self.timing_events)
            except (Exception, SystemExit) as e:
                te.log(f"[{self.tag}] WARNING: hole recovery failed: {te.redact(e)}")
                words, rec = before, dict(te.empty_recovery(), status="failed")
            te.log(f"[{self.tag}] VERIFY: Google returned {google_total} words, {looped} looped removed, {dropped} dropped at "
                   f"tail splices, kept {len(words)} ({rec.get('inserted', 0)} recovered); billed {self.billed:.0f} s")
            recovered = [(a, b) for a, b in (rec.get("inserted_ranges") and [(r["start"], r["end"]) for r in rec["inserted_ranges"]] or [])]
            out = {"job": self.tag, "lang": self.lang, "total": total, "audio_end": audio_end, "bounds": bounds,
                   "words": [[w[0], w[1], w[2]] for w in words], "recovered": recovered,
                   "timing_events": self.timing_events, "loops": self.loops, "silence": self.silence,
                   "stats": {"google_words": google_total, "looped": looped, "dropped": dropped, "kept": len(words),
                             "recovery": {k: rec.get(k) for k in ("status", "gaps_checked", "pieces", "inserted", "already_present", "filtered")},
                             "billed_s": self.billed}}
            te.atomic_write(f"{self.work}/result.json", json.dumps(out, ensure_ascii=False))
            te.log(f"[{self.tag}] done")
        finally:
            self.cleanup()

    # ----- dz: short overlapping windows, speaker labels only -----
    def diar_windows(self, total):
        """Windows of DIAR_WIN s that share DIAR_OVERLAP s with the next one. A window's `core` is the part of it that
        is used: everything but half the overlap at each joint, so the cores tile the file exactly."""
        step, wins, a, k = DIAR_WIN - DIAR_OVERLAP, [], 0.0, 0
        while True:
            b = min(total, a + DIAR_WIN)
            if total - b < 30.0:
                b = total               # a sliver left over joins the last window
            wins.append({"tag": f"dw-{k:03d}", "span": (a, b),
                         "core": (0.0 if k == 0 else a + DIAR_OVERLAP / 2, total if b >= total else b - DIAR_OVERLAP / 2)})
            if b >= total:
                return wins
            a += step; k += 1

    def words_with_speakers(self, st):
        out = []
        for _, fr in st.get("response", {}).get("results", {}).items():
            for r in fr.get("inlineResult", {}).get("transcript", {}).get("results", []):
                for a in r.get("alternatives", [])[:1]:
                    for w in a.get("words", []):
                        out.append({"s": te.sec(w["startOffset"]) if "startOffset" in w else None,
                                    "e": te.sec(w["endOffset"]) if "endOffset" in w else None,
                                    "w": w.get("word", ""), "spk": w.get("speakerLabel")})
        return out

    @staticmethod
    def sane(words, span_len):
        """The words a response can be trusted for: not the labeller's own text, and with offsets inside the window
        (one response ended in a word timed at 14 million seconds, which made the transcript look complete)."""
        ok = []
        for x in words:
            if LEAK.match(x["w"].strip()):
                continue
            if any(v is not None and (v > span_len + 10.0 or v < -1.0) for v in (x["s"], x["e"])):
                continue
            ok.append(x)
        return ok

    def cut_short(self, w, words):
        """True when the response stops while the audio is still going: its last real word ends well before the
        window does and the rest of the window is loud, or it ends in the labeller's own text or a nonsense offset."""
        a, b = w["span"]
        real = [x for x in self.sane(words, b - a) if x["e"] is not None]
        garbled = len(real) < len(words) and any(x not in real for x in words[-3:])
        last = max([x["e"] for x in real] or [0.0])
        rest = (b - a) - last
        if rest < 25.0:
            return garbled
        rms = self.loud[0]
        i0, i1 = int((a + last + 1.0) / 0.1), int((b - 1.0) / 0.1)
        frames = rms[i0:i1]
        loud = bool(frames) and sum(1 for x in frames if x > te_active_db()) / len(frames) > 0.5
        return garbled or loud

    def halves(self, w, overlap=40.0):
        """Two windows that together stand in for one that came back cut short or never answered; they share `overlap`
        seconds. None when they would be too short to be worth it."""
        a, b = w["span"]
        if b - a < 2 * DIAR_MIN + overlap:
            return None
        mid = te.quiet_midpoint(self.quiet, a + DIAR_MIN, b - DIAR_MIN)
        h = overlap / 2
        return [{"tag": w["tag"] + "a", "core": (w["core"][0], mid), "span": (a, min(b, mid + h))},
                {"tag": w["tag"] + "b", "core": (mid, w["core"][1]), "span": (max(a, mid - h), b)}]

    def window_response(self, w):
        return (w["tag"], w["span"], self.cached(self.raw, w["tag"], *w["span"]))

    def fetch_window(self, w):
        """The windows that stand in for w, in order: [w] itself, or its halves (recursively) when Google never answers
        it or answers with a transcript cut short. A window too short to split is kept as it is, marked cut_short."""
        marker = f"{self.raw}/{w['tag']}.split"
        if not os.path.exists(marker):
            st = self.window_response(w)[2]
            if st is None:
                try:
                    st = self.recognize(w["span"][0], w["span"][1], w["tag"])
                except te.Stalled:
                    te.log(f"[dz] {w['tag']}: Google never answered")
                    st = None
            if st is not None:
                if not self.cut_short(w, self.words_with_speakers(st)):
                    return [w]
                te.log(f"[dz] {w['tag']}: the transcript stops while the audio goes on")
            halves = self.halves(w)
            if halves is None:
                if st is None:
                    raise SystemExit(f"{w['tag']}: Google never answered and the window is too short to split")
                return [dict(w, cut_short=True)]
            open(marker, "w").close()
        halves = self.halves(w)
        return [x for h in halves for x in self.fetch_window(h)]

    def run_diarize(self):
        self.raw = f"{self.work}/raw"
        total, audio_end = self.setup()
        self.loud = loudness(self.ffmpeg, self.full)
        wins = self.diar_windows(total)
        te.log(f"[dz] {len(wins)} windows of {DIAR_WIN:.0f} s, {DIAR_OVERLAP:.0f} s overlap")
        try:
            self.deploy(); self.claim_prefix()
            with concurrent.futures.ThreadPoolExecutor(max_workers=te.PREFETCH_WORKERS) as pool:
                resolved = [f.result() for f in [pool.submit(self.fetch_window, w) for w in wins]]
            out = {"job": "dz", "total": total, "windows": []}
            for w in [x for group in resolved for x in group]:
                st = self.window_response(w)[2]
                if st is None:
                    te.log(f"[dz] WARNING: {w['tag']} has no response")
                    continue
                words = self.sane(self.words_with_speakers(st), w["span"][1] - w["span"][0])
                out["windows"].append({"tag": w["tag"], "core": w["core"], "span": w["span"], "words": words,
                                       "cut_short": bool(w.get("cut_short"))})
            n_cut = sum(1 for w in out["windows"] if w["cut_short"])
            te.log(f"[dz] {len(out['windows'])} windows, {sum(len(w['words']) for w in out['windows'])} words, "
                   f"{n_cut} still cut short after splitting")
            out["billed_s"] = self.billed
            te.atomic_write(f"{self.work}/result.json", json.dumps(out, ensure_ascii=False))
            te.log("[dz] done")
        finally:
            self.cleanup()


def te_active_db():
    return ACTIVE_DB


def run_job(job, media, work):
    te.load_config()
    lang = LANGS.get(job, LANGS["en"])
    eng = Engine(media, work, lang, job, diarize=(job == "dz"))
    try:
        eng.run_diarize() if job == "dz" else eng.run_engine()
    except te.Stopped as stop:
        raise SystemExit(stop.code)


def prepare_master_audio(media, work):
    """One 16 kHz mono flac of the whole file, shared (copied) into each job's work folder."""
    te.load_config()
    os.environ.setdefault("SE_STT_LANGUAGE", LANGS["en"])
    e = Engine(media, work, LANGS["en"], "master")
    e.ffmpeg = te._tool("SE_STT_FFMPEG", "ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg")
    e.ffprobe = te._tool("SE_STT_FFPROBE", "ffprobe", "/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "/usr/bin/ffprobe")
    e.video_bytes = os.path.getsize(media)
    total = float(e.sh(e.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", media).stdout.strip())
    os.makedirs(work, exist_ok=True)
    full, _ = e.prepare_audio(total)
    return full, total


def spawn_jobs(media, work, only=None):
    full, total = prepare_master_audio(media, f"{work}/master")
    procs = {}
    for job in (only or JOBS):
        jw = f"{work}/job-{job}"
        os.makedirs(jw, exist_ok=True)
        if os.path.exists(f"{jw}/result.json"):
            te.log(f"{job}: result already there, skipping (delete {jw}/result.json to run it again)")
            continue
        for name in ("full.flac", "audio.json"):
            if not os.path.exists(f"{jw}/{name}"):
                shutil.copy(f"{work}/master/{name}", f"{jw}/{name}")
        log = open(f"{jw}/run.log", "w", encoding="utf-8")
        # English requests of 18 minutes were the ones Google never answered on this kind of audio (the same audio
        # asked for as Arabic was fine, and 9 minute halves of the English ones were answered in about a minute),
        # so the engines cut at 10 minutes unless told otherwise.
        env = dict(os.environ, SE_STT_LANGUAGE=LANGS.get(job, LANGS["en"]))
        env.setdefault("SE_STT_CHUNK", "600")
        procs[job] = (subprocess.Popen([sys.executable, os.path.abspath(__file__), "--job", job, media, jw], stdout=log, stderr=subprocess.STDOUT, env=env), log)
        te.log(f"{job}: started, log at {jw}/run.log")
    for job, (p, log) in procs.items():
        p.wait(); log.close()
        te.log(f"{job}: finished with exit code {p.returncode}")
    return {job: p.returncode for job, (p, _) in procs.items()}


# ---------------------------------------------------------------------------------------------
# Which language is being spoken, decided per stretch of speech from both runs of the same audio.
# Each run writes the language it was not told to expect in its own script (Arabic speech comes back
# romanized from the English run, English speech transliterated from the Arabic run), so the two texts
# together identify the language far better than either alone. The words that tell them apart are
# learned from this file's own confident stretches, then applied to every stretch, sentence by sentence.
# ---------------------------------------------------------------------------------------------
import bisect, collections, math, re

AR_LET = re.compile("[\u0600-\u06FF]")
EN_FN = set("the of and to a in is that it for was on are as with his they be at have this from or had by not but what all were we when your can said there an each which she do how their if will up other about out many then them these so some her would make like him into has two more go no way could my than been who its now did get may part i you he me our us just very also because going think know yes well right".split())
AR_FN = set("في من على إلى الى عن هذا هذه ذلك التي الذي الذين لم لن كان كانت حتى بعد قبل بين لكن هناك ايضا أيضا عندما لان لأن".split())


def norm_ar(t):
    """The letters of an Arabic-script token: no punctuation, harakat or tatweel, alef and yeh forms folded."""
    t = re.sub("[^\u0621-\u064a\u0671-\u06d3]", "", t)
    return t.translate(str.maketrans("\u0623\u0625\u0622", "\u0627\u0627\u0627")).replace("\u0649", "\u064a")


def norm_en(t):
    return re.sub(r"[^a-z0-9']", "", t.lower())


def has_def_article(t):
    return len(t) > 3 and t.startswith(("ال", "وال", "بال", "لل", "فال", "كال"))


def seed_windows(ar, en, size=20.0, total=None):
    """window index -> 'en' | 'ar' for windows the simple word rules are sure about."""
    total = total or max(ar[-1][1], en[-1][1])
    labels = {}
    for k in range(int(total // size) + 1):
        a0, a1 = k * size, (k + 1) * size
        A = [norm_ar(w[2]) for w in ar if a0 <= w[0] < a1]
        Er = [w[2] for w in en if a0 <= w[0] < a1]
        E = [norm_en(x) for x in Er]
        A, E = [x for x in A if x], [x for x in E if x]
        if len(Er) >= 12 and sum(1 for x in Er if AR_LET.search(x)) >= 0.5 * len(Er):
            labels[k] = "ar"        # the English run wrote Arabic letters for most of it
            continue
        if len(A) < 12 or len(E) < 12:
            continue
        al = sum(1 for x in A if has_def_article(x)) / len(A)
        afn = sum(1 for x in A if x in AR_FN) / len(A)
        efn = sum(1 for x in E if x in EN_FN) / len(E)
        if efn >= 0.42 and al <= 0.04 and afn <= 0.06:
            labels[k] = "en"
        elif al >= 0.14 and efn <= 0.28:
            labels[k] = "ar"
    return labels


def train(ar, en, labels, size=20.0, alpha=0.5, min_count=1):
    """Token log-likelihood ratios, log P(token | English) - log P(token | Arabic), learned from the sure windows,
    one table per run (each run writes the same speech in its own script)."""
    tabs = {"ar": {"en": collections.Counter(), "ar": collections.Counter()}, "en": {"en": collections.Counter(), "ar": collections.Counter()}}
    for w in ar:
        k = int(w[0] // size)
        if k in labels:
            t = norm_ar(w[2])
            if t: tabs["ar"][labels[k]][t] += 1
    for w in en:
        k = int(w[0] // size)
        if k in labels:
            t = norm_en(w[2])
            if t: tabs["en"][labels[k]][t] += 1
    llr = {}
    for run in ("ar", "en"):
        ce, ca = tabs[run]["en"], tabs[run]["ar"]
        ne, na = sum(ce.values()), sum(ca.values())
        vocab = set(ce) | set(ca)
        V = len(vocab) + 1
        # a word met only a few times says little about a language (an English "forward" heard once in an Arabic-labelled
        # window would otherwise count as Arabic evidence), so it says nothing until it has been met min_count times
        llr[run] = {t: math.log((ce[t] + alpha) / (ne + alpha * V)) - math.log((ca[t] + alpha) / (na + alpha * V))
                    for t in vocab if ce[t] + ca[t] >= min_count}
        llr[run]["__unk__"] = 0.0   # a word neither class has met says nothing; the class sizes must not bias it
    return llr


FILLERS = {"uh", "um", "umm", "hmm", "mm", "mmm", "ah", "oh", "erm", "er", "huh", "ااا", "اا", "امم", "ام", "اه", "اح", "ااااا", "هم", "همم"}


ARTICLE_LLR, AR_FN_LLR, EN_FN_LLR = -2.5, -1.5, 1.5    # what a word feature says when the tables have not met the word


def event_score(run, word, llr, clip=3.0, en_weight=0.5):
    """What one recognized word says about the language: positive English, negative Arabic, 0 when it says nothing
    (a filler, a number, a word nothing is known about).

    A word the token tables have met often enough is judged by them; any other word by what its kind says: a
    definite article or an Arabic function word is Arabic, an English function word is English. The Arabic run is
    the reliable witness: it never translates, real Arabic stays Arabic and English comes back transliterated. The
    English run sometimes translates the host's Arabic into fluent English and sometimes writes the guest's English in
    Arabic letters, so its fluent English counts for `en_weight` of an Arabic-run word, and an Arabic-letter word from
    it is judged like any Arabic-run word."""
    raw = word[2]

    def arabic_token(t):
        if t in llr["ar"]:
            return max(-clip, min(clip, llr["ar"][t]))
        if has_def_article(t):
            return ARTICLE_LLR
        return AR_FN_LLR if t in AR_FN else 0.0

    if run == "en":
        if AR_LET.search(raw):
            t = norm_ar(raw)
            return en_weight * arabic_token(t) if t and t not in FILLERS else 0.0
        t = norm_en(raw)
        if not t or t.isdigit() or t in FILLERS:
            return 0.0
        v = max(-clip, min(clip, llr["en"][t])) if t in llr["en"] else (EN_FN_LLR if t in EN_FN else 0.0)
        return v * en_weight if v > 0 else v
    t = norm_ar(raw)
    if not t or t in FILLERS:
        return 0.0
    return arabic_token(t)


def smooth(events, pause_cost=2.5, run_cost=6.0, pause=0.3):
    """Language of each event (start, end, score), positive score = English. Speech rarely changes language in the
    middle of a run of words, so a switch costs `run_cost` nats there and only `pause_cost` after a pause."""
    if not events:
        return []
    best = {"en": events[0][2] / 2, "ar": -events[0][2] / 2}
    back, reach = [], events[0][1]
    for start, end, sc in events[1:]:
        cost = pause_cost if start - reach >= pause else run_cost
        reach = max(reach, end)
        nb, bk = {}, {}
        for lang, e in (("en", sc / 2), ("ar", -sc / 2)):
            other = "ar" if lang == "en" else "en"
            if best[lang] >= best[other] - cost:
                nb[lang], bk[lang] = best[lang] + e, lang
            else:
                nb[lang], bk[lang] = best[other] - cost + e, other
        best = nb; back.append(bk)
    lang = max(best, key=best.get)
    seq = [lang]
    for bk in reversed(back):
        lang = bk[lang]; seq.append(lang)
    return list(reversed(seq))


def decide(ar, en, size=20.0, weak=4.0, pause_cost=2.5, run_cost=6.0, alpha=3.0, min_count=10, en_weight=0.5):
    """Stretches of speech with one language each, from both runs of the same audio: [{s, e, lang, score, weak, n,
    ar, en}] where ar / en list the indices of each run's words inside the stretch. Returns (stretches, llr, seeds)."""
    seeds = seed_windows(ar, en, size=size)
    llr = train(ar, en, seeds, size=size, alpha=alpha, min_count=min_count)
    events = sorted([((w[0] + w[1]) / 2, w[0], w[1], event_score("ar", w, llr, en_weight=en_weight), "ar", i) for i, w in enumerate(ar)] +
                    [((w[0] + w[1]) / 2, w[0], w[1], event_score("en", w, llr, en_weight=en_weight), "en", i) for i, w in enumerate(en)])
    seq = smooth([(e[1], e[2], e[3]) for e in events], pause_cost=pause_cost, run_cost=run_cost)
    stretches, cur = [], None
    for ev, lang in zip(events, seq):
        if cur is None or cur["lang"] != lang:
            cur = {"lang": lang, "s": ev[1], "e": ev[2], "score": 0.0, "n": 0, "ar": [], "en": [], "mids": (ev[0], ev[0])}
            stretches.append(cur)
        cur["e"] = max(cur["e"], ev[2]); cur["mids"] = (cur["mids"][0], ev[0])
        cur["score"] += ev[3]; cur["n"] += 1 if ev[3] else 0
        cur[ev[4]].append(ev[5])
    for i, c in enumerate(stretches):
        c["weak"] = abs(c["score"]) < weak
    merged = []
    for c in stretches:
        if merged and merged[-1]["lang"] == c["lang"]:
            m = merged[-1]
            m["e"] = max(m["e"], c["e"]); m["score"] += c["score"]; m["n"] += c["n"]
            m["mids"] = (m["mids"][0], c["mids"][1])
        else:
            merged.append(c)
    # Words go to a stretch by where they sit in time, not by which event they were: the two runs time the same
    # spoken word a little apart, and a switch of language between them must not lose the word.
    cuts = [(a["mids"][1] + b["mids"][0]) / 2 for a, b in zip(merged, merged[1:])]
    for c in merged:
        c["ar"], c["en"] = [], []
        c["weak"] = abs(c["score"]) < weak
    for run, words in (("ar", ar), ("en", en)):
        for i, w in enumerate(words):
            merged[bisect.bisect_right(cuts, (w[0] + w[1]) / 2)][run].append(i)
    for c in merged:
        c.pop("mids", None)
    return merged, llr, seeds


# ---------------------------------------------------------------------------------------------
# Speakers: overlapping diarized windows stitched into one timeline.
# ---------------------------------------------------------------------------------------------

GAP = 1.5          # words of one speaker closer than this are one segment
ZONE_MARGIN = ZONE_MARGIN_S  # ignore the edges of the shared audio, where a window's own recognition is weakest
MIN_MATCH_S = 3.0  # least shared speaking time to call two labels one speaker
MIN_MATCH_SHARE = 0.4


def window_segments(win):
    """[[start, end, label]] in file time for one window, from words whose offsets make sense."""
    off = win["span"][0]
    ws = []
    for w in win["words"]:
        s, e, spk = w["s"], w["e"], w["spk"]
        if s is None or e is None or spk is None or e < s or e - s > 8:
            continue
        ws.append((s + off, e + off, spk))
    ws.sort()
    labs = [x[2] for x in ws]
    sm = list(labs)
    for i in range(1, len(ws) - 1):
        if labs[i - 1] == labs[i + 1] and labs[i] != labs[i - 1]:
            sm[i] = labs[i - 1]
    segs = []
    for (s, e, _), lab in zip(ws, sm):
        if segs and segs[-1][2] == lab and s - segs[-1][1] < GAP:
            segs[-1][1] = max(segs[-1][1], e)
        else:
            segs.append([s, e, lab])
    return segs


BIN = 0.5


def _active_bins(segs, lo, hi):
    """The half-second bins in which a speaker is active inside [lo, hi], each widened by one bin either side,
    so two windows that time the same speech a fraction of a second apart still count as sharing it."""
    out = set()
    for s, e, _ in segs:
        a, z = max(s, lo), min(e, hi)
        if z > a:
            out.update(range(int(a / BIN) - 1, int(z / BIN) + 2))
    return out


def stitch(windows, with_conflicts=False):
    """Global speaker timeline [[start, end, 'S#']] for the whole file, and a report on the matching.
    A window's labels mean nothing outside it, so each is matched to the earlier window's speaker it shares
    the most speaking time with in the audio the two windows both heard; a label that shares too little is a
    new speaker."""
    segs = [window_segments(w) for w in windows]
    gid, nxt, report = {}, 1, []
    for k, win in enumerate(windows):
        labels = []
        for s in segs[k]:
            if s[2] not in labels:
                labels.append(s[2])
        if k == 0:
            for lab in labels:
                gid[(k, lab)] = nxt; nxt += 1
            continue
        lo, hi = win["span"][0] + ZONE_MARGIN, windows[k - 1]["span"][1] - ZONE_MARGIN
        earlier = {}
        for s in segs[k - 1]:
            g = gid.get((k - 1, s[2]))
            if g is not None:
                earlier.setdefault(g, []).append(s)
        earlier = {g: _active_bins(v, lo, hi) for g, v in earlier.items()}
        for lab in labels:
            mine = _active_bins([s for s in segs[k] if s[2] == lab], lo, hi)
            zone_time = len(mine) * BIN
            ov = {g: len(mine & bins) * BIN for g, bins in earlier.items()}
            best = max(ov, key=ov.get) if ov else None
            if best is not None and ov[best] >= max(MIN_MATCH_S, MIN_MATCH_SHARE * zone_time):
                gid[(k, lab)] = best
                report.append({"window": k, "label": lab, "matched": f"S{best}", "shared_s": round(ov[best], 1), "zone_s": round(zone_time, 1)})
            else:
                gid[(k, lab)] = nxt
                report.append({"window": k, "label": lab, "matched": None, "new": f"S{nxt}", "zone_s": round(zone_time, 1),
                               "best_shared_s": round(ov[best], 1) if best is not None else 0.0})
                nxt += 1
    timeline = []
    for k, win in enumerate(windows):
        a, b = win["core"]
        for s in segs[k]:
            s0, e0 = max(s[0], a), min(s[1], b)
            if e0 > s0:
                timeline.append([s0, e0, f"S{gid[(k, s[2])]}"])
    timeline.sort()
    if not with_conflicts:
        return timeline, report
    # two labels that both speak in one window are two people; how long they were heard together is the weight
    conflicts = collections.Counter()
    for k, win in enumerate(windows):
        a, b = win["core"]
        spoken = collections.Counter()
        for s_ in segs[k]:
            d = min(s_[1], b) - max(s_[0], a)
            if d > 0:
                spoken[gid[(k, s_[2])]] += d
        heard = sorted(g for g, t in spoken.items() if t >= MIN_MATCH_S)
        for i, g in enumerate(heard):
            for h in heard[i + 1:]:
                conflicts[(g, h)] += min(spoken[g], spoken[h])
    return timeline, report, conflicts


class Timeline:
    def __init__(self, timeline, reach=1.0):
        self.t, self.starts, self.reach = timeline, [x[0] for x in timeline], reach

    def at(self, t):
        i = bisect.bisect_right(self.starts, t) - 1
        best, dist = None, self.reach
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(self.t):
                s, e, lab = self.t[j]
                d = 0.0 if s <= t <= e else min(abs(t - s), abs(t - e))
                if d < dist or (best is None and d <= dist):
                    best, dist = lab, d
        return best


# ---------------------------------------------------------------------------------------------
# Compile: words, speakers, sound events, foreign stretches, cues.
# ---------------------------------------------------------------------------------------------
import difflib, statistics, uuid

ACTIVE_DB = -42.0        # a 0.1 s frame louder than this counts as sound
GAP_S = 1.5              # silence between words shorter than this is not looked at
SUSTAINED_S = 3.0
LANG_CODE = {"ar": "ar-XA", "en": "en-US"}
SOUND_LABEL = {"music": "[موسيقى]", "sound": "[صوت]"}


def read_result(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def loudness(ffmpeg, flac):
    """(rms, peak) in dBFS for every 0.1 s of the audio."""
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-i", flac, "-af",
           "asetnsamples=n=1600:p=0,astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level+Peak_level,"
           "ametadata=print:file=-", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    rms, peak = [], []
    for line in out.splitlines():
        if line.startswith("lavfi.astats.Overall.RMS_level="):
            v = line.split("=")[1]; rms.append(-120.0 if v == "-inf" else float(v))
        elif line.startswith("lavfi.astats.Overall.Peak_level="):
            v = line.split("=")[1]; peak.append(-120.0 if v == "-inf" else float(v))
    return rms, peak


def sound_gaps(words, loud, total):
    """Stretches between words (and before the first, after the last) of GAP_S or more, with what the audio
    does there. Speech that no run heard would show up as a loud gap, so the ones that are audibly not
    silent are kept, each with a guess at what kind of sound it is."""
    rms, peak = loud
    edges = [(0.0, words[0]["s"])] if words else []
    edges += [(a["e"], b["s"]) for a, b in zip(words, words[1:])]
    if words:
        edges.append((words[-1]["e"], total))
    out = []
    for a, b in edges:
        if b - a < GAP_S:
            continue
        i0, i1 = max(0, int((a + 0.1) / 0.1)), min(len(rms), int((b - 0.1) / 0.1))
        if i1 <= i0:
            continue
        fr = rms[i0:i1]
        share = sum(1 for x in fr if x > ACTIVE_DB) / len(fr)
        pk, med = max(peak[i0:i1]), statistics.median(fr)
        audible = share >= 0.25 and pk >= -40.0
        if not audible:
            continue
        sustained = (b - a) >= SUSTAINED_S and share >= 0.5
        out.append({"s": round(a, 2), "e": round(b, 2), "kind": "sound", "sustained": sustained,
                    "activeShare": round(share, 3), "peakDb": round(pk, 1), "medianDb": round(med, 1),
                    "audiblyNonSilent": True})
    return out


def foreign_segments(words, base):
    """Runs of speech in the language that is not the programme's, merged across short pauses and fillers."""
    segs, cur = [], None
    for w in words:
        if w["lang"] == base:
            if cur and norm_en(w["w"]) not in FILLERS and norm_ar(w["w"]) not in FILLERS:
                segs.append(cur); cur = None
            continue
        if cur and w["s"] - cur["e"] <= 2.5:
            cur["e"] = w["e"]; cur["words"].append(w["w"])
        else:
            if cur:
                segs.append(cur)
            cur = {"s": w["s"], "e": w["e"], "language": w["lang"], "words": [w["w"]]}
    if cur:
        segs.append(cur)
    return [{"s": round(x["s"], 2), "e": round(x["e"], 2), "language": x["language"], "outcome": "transcribed",
             "original": " ".join(x["words"])} for x in segs if x["e"] - x["s"] >= 1.0]


def cross_check(isl, ar, en):
    """Where the English run also wrote Arabic letters, its reading and the Arabic run's can be compared word for word."""
    out = []
    for i in isl:
        if i["lang"] != "ar":
            continue
        a = [(ar[k][0], ar[k][1], norm_ar(ar[k][2])) for k in i["ar"] if norm_ar(ar[k][2])]
        e = [(en[k][0], en[k][1], norm_ar(en[k][2])) for k in i["en"] if AR_LET.search(en[k][2]) and norm_ar(en[k][2])]
        if len(e) < 3 or len(a) < 3:
            continue
        sm = difflib.SequenceMatcher(None, [x[2] for x in a], [x[2] for x in e], autojunk=False)
        for op, a0, a1, b0, b1 in sm.get_opcodes():
            if op == "replace" and a1 - a0 <= 3 and b1 - b0 <= 3:
                out.append({"s": round(a[a0][0], 2), "e": round(a[a1 - 1][1], 2),
                            "heard": " ".join(x[2] for x in a[a0:a1]), "other": " ".join(x[2] for x in e[b0:b1])})
    return out


def smooth_speakers(words):
    sp = [w.get("speaker") for w in words]
    for i in range(1, len(words) - 1):
        if sp[i - 1] is not None and sp[i - 1] == sp[i + 1] != sp[i] and words[i]["e"] - words[i]["s"] < 0.6:
            sp[i] = sp[i - 1]
    for w, x in zip(words, sp):
        if x is None:
            w.pop("speaker", None)
        else:
            w["speaker"] = x


def mark_fillers(words):
    for w in words:
        if norm_en(w["w"]) in FILLERS or norm_ar(w["w"]) in FILLERS:
            w["filler"] = True


def fill_from_short_windows(words, dz_windows, stretches, gap=1.8):
    """English the long recognition skipped (or wrote in Arabic letters) but the short windows heard. Only where no word
    of either language sits for `gap` seconds or more inside a stretch decided to be English, so a word both runs
    heard, timed a little apart, is never added twice, and the short windows' English translation of an Arabic
    stretch never gets in. Returns how many words were added."""
    heard = []
    for win in dz_windows:
        c0, c1 = win["core"]
        off = win["span"][0]
        for x in win["words"]:
            if x["s"] is None or x["e"] is None or not re.search("[A-Za-z]", x["w"]):
                continue
            t = x["s"] + off
            if c0 <= t < c1:
                heard.append((t, x["e"] + off, x["w"]))
    heard.sort()
    starts = [h[0] for h in heard]
    ordered = sorted(words, key=lambda w: w["s"])
    w_starts = [w["s"] for w in ordered]
    added = []
    for c in stretches:
        if c["lang"] != "en":
            continue
        inside = ordered[bisect.bisect_left(w_starts, c["s"] - 0.01):bisect.bisect_right(w_starts, c["e"] + 0.01)]
        edges = [c["s"]] + [x for w in inside for x in (w["s"], w["e"])] + [c["e"]]
        for lo, hi in zip(edges[0::2], edges[1::2]):
            if hi - lo < gap:
                continue
            got = [h for h in heard[bisect.bisect_left(starts, lo + 0.15):bisect.bisect_right(starts, hi - 0.15)] if h[1] <= hi + 0.3]
            if len(got) >= 2:
                added += [{"w": w_, "s": round(t, 3), "e": round(e, 3), "lang": "en", "recalled": True, "src": "short-window"}
                          for t, e, w_ in got]
    words.extend(added)
    words.sort(key=lambda w: (w["s"], w["e"]))
    return len(added)


def merge_speakers(words, conflicts, n):
    """Read the stitched speaker labels as n people. Labels heard together in one window are different people
    (`conflicts`, weighted by how long); labels that mostly carry English go with the English speaker and
    labels that mostly carry Arabic with the Arabic one. Only n = 2 is supported; the words are relabelled S1..Sn
    in order of first appearance. Returns {old label: new label}."""
    stat = {}
    for w in words:
        g = w.get("speaker")
        if g is None:
            continue
        d = max(0.0, w["e"] - w["s"])
        t, e = stat.get(g, (0.0, 0.0))
        stat[g] = (t + d, e + (d if w["lang"] == "en" else 0.0))
    labels = sorted(stat, key=lambda g: int(g[1:]))
    if n != 2 or len(labels) < 2:
        return {g: g for g in labels}
    pair = lambda a, b: conflicts.get(tuple(sorted((int(a[1:]), int(b[1:])))), 0.0)
    share = {g: stat[g][1] / stat[g][0] if stat[g][0] else 0.5 for g in labels}
    side = {g: 1 if share[g] >= 0.5 else 0 for g in labels}

    def gain(g, to):
        v = stat[g][0] * (share[g] - 0.5) * (1 if to == 1 else -1)
        return v + sum(pair(g, h) for h in labels if h != g and side[h] != to)

    for _ in range(100):
        changed = False
        for g in labels:
            if gain(g, 1 - side[g]) > gain(g, side[g]):
                side[g] = 1 - side[g]; changed = True
        if not changed:
            break
    first = {}
    for w in sorted(words, key=lambda w: w["s"]):
        if w.get("speaker") in side:
            first.setdefault(side[w["speaker"]], w["s"])
    names = {c: f"S{i + 1}" for i, c in enumerate(sorted(first, key=first.get))}
    mapping = {g: names[side[g]] for g in labels if side[g] in names}
    for w in words:
        if w.get("speaker") in mapping:
            w["speaker"] = mapping[w["speaker"]]
    return mapping


def arabic_words_take_the_arabic_speaker(words):
    """The diarization was asked for as English, so its labels are trustworthy for English speech and much less so for
    Arabic (measured against a reference with two speakers: English words labelled as the guest were the guest 99% of
    the time, Arabic words labelled as the guest were the host 90% of the time). Words in the other language therefore
    take the speaker who mostly speaks it. Returns that speaker, or None when nobody speaks it."""
    seconds = collections.Counter()
    for w in words:
        if w["lang"] != "en" and w.get("speaker"):
            seconds[w["speaker"]] += max(0.0, w["e"] - w["s"])
    if not seconds:
        return None
    speaker = seconds.most_common(1)[0][0]
    for w in words:
        if w["lang"] != "en":
            w["speaker"] = speaker
            w.pop("speakerGuess", None)
    return speaker


def fill_speakers(words, reach=2.5):
    """A word without a label takes the label of the nearest labelled word of the same language within `reach`
    seconds; failing that, when only two speakers exist, the one that mostly speaks its language (marked)."""
    lab = [w for w in words if w.get("speaker")]
    starts = [w["s"] for w in lab]
    by_lang = collections.defaultdict(collections.Counter)
    for w in lab:
        by_lang[w["lang"]][w["speaker"]] += max(0.0, w["e"] - w["s"])
    people = {w["speaker"] for w in lab}
    guessed = 0
    for w in words:
        if w.get("speaker"):
            continue
        i = bisect.bisect_left(starts, w["s"])
        near = [c for c in lab[max(0, i - 3):i + 3] if c["lang"] == w["lang"] and abs(c["s"] - w["s"]) <= reach]
        if near:
            w["speaker"] = min(near, key=lambda c: abs(c["s"] - w["s"]))["speaker"]
        elif len(people) == 2 and by_lang[w["lang"]]:
            w["speaker"] = by_lang[w["lang"]].most_common(1)[0][0]
            w["speakerGuess"] = True; guessed += 1
    return guessed


def build_cues(words, events, total):
    """Cues on sentence boundaries, one language and one speaker per cue, sound events between them.
    Returns (cues [[start, end, text]], word index lists per cue)."""
    runs, cur = [], None
    for k, w in enumerate(words):
        key = (w["lang"], w.get("speaker"))
        if cur is None or cur[0] != key or w["s"] - words[cur[1][-1]]["e"] > 6.0:
            cur = (key, []); runs.append(cur)
        cur[1].append(k)
    raw = []
    for (lang, _), idxs in runs:
        triples = [[words[k]["s"], words[k]["e"], words[k]["w"]] for k in idxs]
        for a, b in te.cue_groups(triples, LANG_CODE[lang]):
            raw.append((triples[a][0], triples[b - 1][1], " ".join(t[2] for t in triples[a:b]), triples[b - 1][1], idxs[a:b]))
    for ev in events:
        raw.append((ev["s"], ev["e"], ev["label"], ev["e"], []))
    raw.sort(key=lambda c: (c[0], c[1]))
    cues, stats = te.timing_pass([list(c[:4]) for c in raw], total)
    return cues, [c[4] for c in raw]


def clock(t):
    t = max(0.0, t)
    return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}"


def render_notes(words, cues, events_json, summary, isl, ar_res, en_res, dz_res, total):
    """A short review list for the editor: what was decided, and where to listen."""
    lines = [f"# Transcription notes", "",
             f"{len(cues)} cues, {len(words)} words, {total/60:.0f} min. Words carry the language they were recognized in and, "
             f"where the speaker could be followed, a speaker label.", ""]
    dur = collections.Counter()
    for w in words:
        dur[w["lang"]] += max(0.0, w["e"] - w["s"])
    lines += ["## Languages", "",
              f"English {dur['en']/60:.1f} min of speech, Arabic {dur['ar']/60:.1f} min. Decided sentence by sentence from both "
              f"recognition runs; {summary['weak']} words sit in stretches where the evidence was weak (`lowConfidence`).", ""]
    weak = [i for i in isl if i["weak"] and i["n"] >= 6]
    if weak:
        lines += ["Weak stretches with several words, worth a listen:", ""]
        for i in weak[:25]:
            lines.append(f"- {clock(i['s'])} to {clock(i['e'])}: read as {'English' if i['lang'] == 'en' else 'Arabic'}")
        lines.append("")
    lines += ["## Speakers", ""]
    per, english = collections.Counter(), collections.Counter()
    for w in words:
        if "speaker" in w:
            d = max(0.0, w["e"] - w["s"])
            per[w["speaker"]] += d
            if w["lang"] == "en":
                english[w["speaker"]] += d
    if per:
        for k, v in sorted(per.items(), key=lambda kv: int(kv[0][1:])):
            lines.append(f"- {k}: {v/60:.1f} min of speech, {english[k] / v * 100:.0f}% of it English"
                         + (" (the English speaker)" if english[k] / v > 0.7 else " (the Arabic speaker)" if english[k] / v < 0.3 else ""))
        lines.append("")
        sp = summary.get("speaker_matching") or {}
        merged = sp.get("merged_into")
        lines.append("chirp_3 can label speakers only when it is asked for English, so the labels come from overlapping three minute "
                     "windows recognized as English and stitched together by who speaks in the shared audio. "
                     + (f"{sp.get('labels_before_merge')} raw identities came out of that (a speaker silent through a shared stretch is given "
                        f"a new one), and they were merged into {merged} people, using which of them were heard together and which language "
                        f"they mostly speak. " if merged else f"{sp.get('labels_before_merge')} identities came out of that; a speaker silent "
                        "through a shared stretch is given a new one, so one person can appear under several labels (ask for the number of "
                        "speakers to merge them). ")
                     + (f"Arabic words take {sp['other_language_speaker']}, the speaker who mostly speaks Arabic, because the labels are only "
                        "reliable for the language they were asked for. " if sp.get("other_language_speaker") else "")
                     + f"{sum(1 for w in words if 'speaker' not in w)} words have no label and {sp.get('guessed', 0)} have one guessed from their language "
                     "(`speakerGuess`).")
        lines.append("")
    else:
        lines += ["No speaker labels were produced.", ""]
    cut = [w for w in (dz_res or {}).get("windows", []) if w.get("cut_short")]
    if cut:
        lines += [f"{len(cut)} speaker window(s) still came back cut short after splitting: " +
                  ", ".join(f"{clock(w['core'][0])} to {clock(w['core'][1])}" for w in cut) + ". Words there may lack a label.", ""]
    lines += ["## Sound events", ""]
    if events_json["events"]:
        for ev in events_json["events"]:
            lines.append(f"- {clock(ev['s'])} to {clock(ev['e'])}: {ev['label']} (a guess from the audio: "
                         f"{'sustained sound' if ev['label'] == SOUND_LABEL['music'] else 'short sound'})")
    else:
        lines.append("None.")
    lines += ["", "## Engines", ""]
    for name, res in (("Arabic run", ar_res), ("English run", en_res)):
        st = res["stats"]
        lines.append(f"- {name}: Google returned {st['google_words']} words, {st['looped']} looped words removed, "
                     f"{st['recovery'].get('inserted') or 0} recovered from skipped stretches, billed {st['billed_s']:.0f} s.")
    lines.append("")
    return "\n".join(lines)


def compile_outputs(work, media, base, base_lang="ar", n_speakers=None, patch=None):
    ar_res = read_result(f"{work}/job-ar/result.json")
    en_res = read_result(f"{work}/job-en/result.json")
    total = ar_res["total"]
    ar_w, en_w = sorted(ar_res["words"]), sorted(en_res["words"])
    te.log(f"language decision over {len(ar_w)} Arabic-run and {len(en_w)} English-run words")
    isl, llr, seeds = decide(ar_w, en_w)

    def recovered(res):
        return [(a, b) for a, b in res.get("recovered", [])]
    rec_ranges = {"ar": recovered(ar_res), "en": recovered(en_res)}

    words = []
    for i in isl:
        run = i["lang"]
        src = ar_w if run == "ar" else en_w
        for k in i[run]:
            s_, e_, t_ = src[k]
            if not str(t_).strip():
                continue
            if run == "en" and AR_LET.search(t_):
                continue        # the English run wrote English speech in Arabic letters; the short windows may have the words
            w = {"w": t_, "s": round(s_, 3), "e": round(e_, 3), "lang": run}
            if i["weak"]:
                w["lowConfidence"] = True
            if any(a <= s_ <= b for a, b in rec_ranges[run]):
                w["recalled"] = True
            words.append(w)
    words.sort(key=lambda w: (w["s"], w["e"]))

    dz_path = f"{work}/job-dz/result.json"
    speakers, added = None, 0
    if os.path.exists(dz_path):
        dz_res = read_result(dz_path)
        added = fill_from_short_windows(words, dz_res["windows"], isl)
        te.log(f"{added} English words the long recognition skipped were taken from the short windows")
        timeline, match_report, conflicts = stitch(dz_res["windows"], with_conflicts=True)
        tl = Timeline(timeline)
        for w in words:
            if "speaker" not in w:
                sp = tl.at((w["s"] + w["e"]) / 2)
                if sp:
                    w["speaker"] = sp
        n_labels = len({w["speaker"] for w in words if "speaker" in w})
        mapping = merge_speakers(words, conflicts, n_speakers) if n_speakers else {}
        smooth_speakers(words)
        guessed = fill_speakers(words)
        by_language = arabic_words_take_the_arabic_speaker(words) if n_speakers == 2 else None
        speakers = {"labels_before_merge": n_labels, "merged_into": n_speakers or None, "guessed": guessed,
                    "other_language_speaker": by_language, "segments": len(timeline), "matching": match_report}
    else:
        te.log("WARNING: no diarization result, the words carry no speaker labels")
    mark_fillers(words)
    if patch:
        patch(words)            # a caller's own corrections to the word list, before cues, events and notes are built

    # a sound is only an event where no speech was heard, so look at the gaps of the compiled words
    ffmpeg = te._tool("SE_STT_FFMPEG", "ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg")
    flac = f"{work}/job-ar/full.flac"
    gaps = sound_gaps(words, loudness(ffmpeg, flac), total)
    events = [{"label": SOUND_LABEL["music" if g["sustained"] else "sound"], "s": g["s"], "e": g["e"], "labelSource": "heuristic"} for g in gaps]

    cues, cue_words = build_cues(words, events, total)
    for n, idxs in enumerate(cue_words, 1):
        for k in idxs:
            words[k]["line"] = n
    for ev in events:
        ev["line"] = next((n for n, (a, b, t) in enumerate(cues, 1) if t == ev["label"] and abs(a - ev["s"]) < 1.0), None)

    item = os.path.basename(base)
    words_json = {"requestId": str(uuid.uuid4()), "itemId": item, "language": "+".join(LANG_CODE[k] for k in ("ar", "en")),
                  "wordCount": len(words), "words": words}
    events_json = {"requestId": words_json["requestId"], "itemId": item, "language": words_json["language"],
                   "baseLanguage": LANG_CODE[base_lang], "events": events, "gaps": gaps,
                   "foreign": foreign_segments(words, base_lang), "crossCheck": cross_check(isl, ar_w, en_w)}
    te.atomic_write(base + ".words.json", json.dumps(words_json, ensure_ascii=False, separators=(",", ":")))
    te.atomic_write(base + ".events.json", json.dumps(events_json, ensure_ascii=False, separators=(",", ":")))
    te.atomic_write(base + ".srt", te.srt_text(cues))
    dz_res = read_result(dz_path) if os.path.exists(dz_path) else None
    summary = {"words": len(words), "cues": len(cues), "islands": len(isl), "languages": dict(collections.Counter(w["lang"] for w in words)),
               "weak": sum(1 for w in words if w.get("lowConfidence")), "events": len(events), "foreign": len(events_json["foreign"]),
               "speakers": sorted({w["speaker"] for w in words if "speaker" in w}, key=lambda x: int(x[1:])),
               "speaker_matching": speakers}
    te.atomic_write(base + " - transcription notes.md", render_notes(words, cues, events_json, summary, isl, ar_res, en_res, dz_res, total))
    te.log(f"compiled: {summary['words']} words, {summary['cues']} cues, languages {summary['languages']}, "
           f"speakers {len(summary['speakers'])}, {summary['events']} sound events, {summary['weak']} low-confidence words")
    return words, cues, events_json, summary, isl


USAGE = "usage: transcribe-broadcast.py <audio-or-video> [output-base] [work-dir]"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), te._stop)
            except (ValueError, OSError):
                pass
    if argv[:1] == ["--job"]:
        return run_job(argv[1], argv[2], argv[3])
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return
    media = argv[0]
    if not os.path.isfile(media):
        raise SystemExit(f"file not found: {media}")
    base = argv[1] if len(argv) > 1 else os.path.splitext(media)[0]
    work = argv[2] if len(argv) > 2 else base + "-work"
    only = os.environ.get("BC_JOBS", "").split(",") if os.environ.get("BC_JOBS") else None
    if os.environ.get("BC_COMPILE_ONLY", "").strip() != "1":
        codes = spawn_jobs(media, work, only)
        if any(codes.values()):
            raise SystemExit(f"a job failed: {codes}")
    if only is None or set(JOBS[:2]) <= set(only):
        te.load_config()
        n = os.environ.get("BC_SPEAKERS", "").strip()
        compile_outputs(work, media, base, base_lang=os.environ.get("BC_BASE_LANG", "ar"), n_speakers=int(n) if n else None)


if __name__ == "__main__":
    main()
