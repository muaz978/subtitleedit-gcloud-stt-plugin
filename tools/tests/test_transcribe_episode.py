"""Offline tests for tools/transcribe-episode.py. Synthetic data only: no network, no media,
no real transcripts. Run from the repository root with:

    python3 -m unittest discover tools/tests
"""
import http.client, importlib.util, io, json, math, os, random, re, shutil, signal, socket, ssl, subprocess, sys, tempfile, threading, time, unittest
import urllib.error
from unittest import mock

sys.dont_write_bytecode = True      # no __pycache__ next to the script in the repository

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "transcribe-episode.py")

def load_script():
    spec = importlib.util.spec_from_file_location("transcribe_episode", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

te = load_script()

def response(words, billed="46s", lang="tr-TR", model="chirp_3"):
    """A batchRecognize operation in the shape Google returns. words: [(start|None, end|None, text)]."""
    ws = []
    for s, e, t in words:
        w = {"word": t}
        if s is not None: w["startOffset"] = f"{s}s"
        if e is not None: w["endOffset"] = f"{e}s"
        ws.append(w)
    result = {"inlineResult": {"transcript": {"results": [{"alternatives": [{"words": ws}]}]}},
              "metadata": {"totalBilledDuration": billed}}
    return {"done": True,
            "metadata": {"batchRecognizeRequest": {"config": {"languageCodes": [lang], "model": model}}},
            "response": {"results": {"audio-file": result}, "totalBilledDuration": billed}}


class TempWorkMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"SE_STT_LANGUAGE": "tr-TR", "SE_STT_MODEL": "chirp_3"})
        self.env.start()
        for key in ("SE_STT_PROJECT", "SE_STT_BUCKET", "SE_STT_GAP_MIN", "SE_STT_RECOVER", "SE_STT_CUE_BUILDER", "SE_STT_NOTES"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def episode(self):
        ep = te.Episode(os.path.join(self.tmp, "video.mp4"), os.path.join(self.tmp, "out.srt"), self.tmp)
        os.makedirs(ep.raw, exist_ok=True)
        ep.video_bytes = 1234
        ep.token = lambda refresh=False: "test-token"      # never run gcloud from a test
        return ep


class CacheValidation(TempWorkMixin, unittest.TestCase):
    def test_span_record_must_match(self):
        st = response([(0.0, 0.5, "bir")])
        st["_span"] = {"start_ms": 1000, "end_ms": 47000, "language": "tr-TR", "model": "chirp_3", "video_bytes": 1234}
        self.assertIsNone(te.cache_problem(st, 1001, 46999, "tr-TR", "chirp_3", 1234))
        self.assertIsNotNone(te.cache_problem(st, 1004, 47000, "tr-TR", "chirp_3", 1234))
        self.assertIsNotNone(te.cache_problem(st, 1000, 47000, "ar-XA", "chirp_3", 1234))
        self.assertIsNotNone(te.cache_problem(st, 1000, 47000, "tr-TR", "chirp_2", 1234))
        self.assertIsNotNone(te.cache_problem(st, 1000, 47000, "tr-TR", "chirp_3", 999))

    def test_legacy_response_checked_by_billed_duration(self):
        st = response([(0.0, 0.5, "bir")], billed="46s")
        self.assertIsNone(te.cache_problem(st, 0, 45390, "tr-TR", "chirp_3", 1))       # rounded up by 0.61 s
        self.assertIsNone(te.cache_problem(st, 0, 46000, "tr-TR", "chirp_3", 1))
        self.assertIsNotNone(te.cache_problem(st, 0, 44800, "tr-TR", "chirp_3", 1))    # 1.2 s short: other audio
        self.assertIsNotNone(te.cache_problem(st, 0, 46100, "tr-TR", "chirp_3", 1))    # longer than billed
        self.assertIsNotNone(te.cache_problem(st, 0, 45390, "ar-XA", "chirp_3", 1))
        del st["response"]["totalBilledDuration"]
        self.assertIsNone(te.cache_problem(st, 0, 45390, "tr-TR", "chirp_3", 1))       # per-file metadata still says 46 s

    def test_response_with_error_is_not_reused(self):
        st = response([], billed="46s")
        st["response"]["results"]["audio-file"]["error"] = {"message": "bad audio"}
        self.assertIsNotNone(te.cache_problem(st, 0, 45390, "tr-TR", "chirp_3", 1))

    def test_stale_response_is_moved_aside_not_fatal(self):
        ep = self.episode()
        te.atomic_write(f"{ep.raw}/part-000t.json", json.dumps(response([(0.0, 0.5, "bir")], billed="30s")))
        with mock.patch.object(te, "log"):
            self.assertIsNone(ep.cached(ep.raw, "part-000t", 100.0, 145.0))
        self.assertFalse(os.path.exists(f"{ep.raw}/part-000t.json"))
        self.assertTrue(os.path.exists(f"{ep.raw}/stale/part-000t-1.json"))
        te.atomic_write(f"{ep.raw}/part-000t.json", "{not json")
        with mock.patch.object(te, "log"):
            self.assertIsNone(ep.cached(ep.raw, "part-000t", 100.0, 145.0))
        self.assertTrue(os.path.exists(f"{ep.raw}/stale/part-000t-2.json"))

    def test_saved_response_carries_its_span_and_is_reused(self):
        ep = self.episode()
        ep.save_response(ep.raw, "part-001", 10.0, 55.0, response([(0.0, 0.5, "bir")], billed="45s"))
        saved = te.read_json(f"{ep.raw}/part-001.json")
        self.assertEqual(saved["_span"], {"start_ms": 10000, "end_ms": 55000, "language": "tr-TR", "model": "chirp_3", "video_bytes": 1234})
        self.assertEqual(ep.billed, 45.0)
        self.assertIsNotNone(ep.cached(ep.raw, "part-001", 10.0, 55.0))
        self.assertEqual(ep.reused, 1)


class OperationResume(TempWorkMixin, unittest.TestCase):
    def write_op(self, ep, tag, start_ms, end_ms, age=0.0):
        te.atomic_write(f"{ep.raw}/{tag}.op.json", json.dumps({"name": "operation-1", "start_ms": start_ms,
                                                              "end_ms": end_ms, "submitted": time.time() - age}))

    def test_matching_recent_operation_is_resumed(self):
        ep = self.episode()
        self.write_op(ep, "part-002", 5000, 50000)
        self.assertEqual(ep.resumable(ep.raw, "part-002", 5.0, 50.0)["name"], "operation-1")
        self.assertIn(f"{ep.raw}/part-002.op.json", ep.pending)

    def test_old_or_foreign_operation_is_dropped(self):
        ep = self.episode()
        self.write_op(ep, "part-002", 5000, 50000, age=25 * 3600)
        self.assertIsNone(ep.resumable(ep.raw, "part-002", 5.0, 50.0))
        self.assertFalse(os.path.exists(f"{ep.raw}/part-002.op.json"))
        self.write_op(ep, "part-002", 6000, 50000)
        self.assertIsNone(ep.resumable(ep.raw, "part-002", 5.0, 50.0))

    def test_resumed_operation_that_cannot_be_read_is_submitted_again(self):
        ep = self.episode()
        self.write_op(ep, "part-003", 0, 45000)
        calls = []
        ep.deploy = lambda: ("p", "b", "gcloud")
        ep.cut = lambda s, e, p: calls.append("cut") or p
        ep.upload = lambda p, tag: calls.append("upload") or "uri"
        def submit(folder, tag, start, end, uri, attempts=1):
            calls.append("submit")
            te.atomic_write(f"{folder}/{tag}.op.json", json.dumps({"name": "operation-2", "start_ms": 0, "end_ms": 45000, "submitted": time.time()}))
            ep.pending.add(f"{folder}/{tag}.op.json")
            return "operation-2"
        ep.submit = submit
        def poll(name, **kw):
            if name == "operation-1":
                raise te.RequestFailed("GET request failed with HTTP 404: not found", 404)
            return response([(0.0, 0.5, "bir")], billed="45s")
        ep.poll = poll
        with mock.patch.object(te.time, "sleep"), mock.patch.object(te, "log"):
            st = ep.recognize(0.0, 45.0, "part-003")
        self.assertEqual(calls, ["cut", "upload", "submit"])
        self.assertIn("_span", st)
        self.assertFalse(os.path.exists(f"{ep.raw}/part-003.op.json"))
        self.assertEqual(ep.pending, set())


class Robustness(TempWorkMixin, unittest.TestCase):
    def test_atomic_write_retries_a_locked_rename(self):
        path = os.path.join(self.tmp, "out.srt")
        real = os.replace
        attempts = []
        def flaky(a, b):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("locked")
            return real(a, b)
        with mock.patch.object(te.os, "replace", flaky), mock.patch.object(te.time, "sleep"):
            te.atomic_write(path, "1\n")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "1\n")
        self.assertEqual(len(attempts), 3)

    def test_cut_retries_without_frame_size(self):
        ep = self.episode()
        ep.ffmpeg, ep.full = "ffmpeg", "full.flac"
        seen = []
        def run_proc(args):
            seen.append(list(args))
            return subprocess.CompletedProcess(args, 234 if "-frame_size" in args else 0, "", "invalid block size")
        ep.run_proc = run_proc
        ep.cut(1.0, 46.0, "piece.flac")
        self.assertEqual(len(seen), 2)
        self.assertIn("-frame_size", seen[0])
        self.assertNotIn("-frame_size", seen[1])

    def test_api_retries_network_errors_and_refreshes_token_once(self):
        ep = self.episode()
        tokens = []
        ep.token = lambda refresh=False: tokens.append(refresh) or "t"
        outcomes = [urllib.error.URLError("timed out"), TimeoutError(),
                    urllib.error.HTTPError("u", 401, "unauthorized", {}, io.BytesIO(b"expired")), io.BytesIO(b'{"ok": 1}')]
        class Resp:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.data.read()
        def urlopen(req, timeout):
            item = outcomes.pop(0)
            if isinstance(item, BaseException):
                raise item
            return Resp(item)
        with mock.patch.object(te.urllib.request, "urlopen", urlopen), mock.patch.object(te.time, "sleep"):
            self.assertEqual(ep.api("GET", "https://example.invalid/v2/x"), {"ok": 1})
        self.assertIn(True, tokens)

    def test_api_error_names_method_and_status_not_url(self):
        ep = self.episode()
        ep.token = lambda refresh=False: "t"
        def urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b"denied for projects/secret-project"))
        with mock.patch.object(te.urllib.request, "urlopen", urlopen):
            with self.assertRaises(SystemExit) as ctx:
                ep.api("POST", "https://us-speech.googleapis.com/v2/projects/secret-project/locations/us")
        text = str(ctx.exception)
        self.assertIn("POST", text)
        self.assertIn("403", text)
        self.assertNotIn("secret-project", text)
        self.assertNotIn("googleapis", text)

    def test_cleanup_never_raises_and_keeps_audio_for_pending_operations(self):
        ep = self.episode()
        ep.uploaded = True
        ep.pending.add("x.op.json")
        with mock.patch.object(te, "log"):
            ep.cleanup()
        self.assertEqual(te.read_json(f"{self.tmp}/uploads.json"), [ep.prefix])
        ep.pending.clear()
        ep.deploy = mock.Mock(side_effect=SystemExit("SE_STT_BUCKET is not set"))
        with mock.patch.object(te, "log"):
            ep.cleanup()     # must not raise even though nothing can be removed
        self.assertEqual(te.read_json(f"{self.tmp}/uploads.json"), [ep.prefix])

    def test_cleanup_treats_matched_no_objects_as_removed(self):
        ep = self.episode()
        ep.uploaded = True
        te.atomic_write(f"{self.tmp}/uploads.json", json.dumps(["stt/1-2/"]))
        ep.deploy = lambda: ("p", "b", "gcloud")
        ep.run_proc = lambda args: subprocess.CompletedProcess(args, 1, "", "ERROR: One or more URLs matched no objects.")
        with mock.patch.object(te, "log"):
            ep.cleanup()
        self.assertEqual(te.read_json(f"{self.tmp}/uploads.json"), [])

    def test_chunk_limit_guard(self):
        self.assertLessEqual(te.CHUNK + 60 + te.MAX_SNAP, 1200)


def clean_words(rng, count=200, start=0.5):
    """Plausible speech: ordered, starts at or after the previous end, words under a second."""
    out, t = [], start
    for k in range(count):
        s = t + rng.choice([0.0, 0.0, 0.0, 0.25, 0.8])
        e = s + rng.uniform(0.1, 0.9)
        out.append([round(s, 2), round(e, 2), f"k{k}"])
        t = out[-1][1]
    return out


class TimingRepair(unittest.TestCase):
    def assert_placement(self, kept, ws, dur):
        self.assertEqual([k[2] for k in kept], [w[2] for w in ws])
        self.assertTrue(te.placement_ok(kept, dur), kept)

    def test_clean_input_matches_legacy_placement(self):
        rng = random.Random(3)
        for _ in range(20):
            ws = clean_words(rng)
            dur = ws[-1][1] + 5.0
            kept, events = te.repair([list(w) for w in ws], dur)
            legacy, fixed = te.repair_legacy([tuple(w) for w in ws], dur)
            self.assertEqual(fixed, 0)
            self.assertEqual(events, [])
            for a, b in zip(kept, legacy):
                self.assertAlmostEqual(a[0], b[0], places=9)
                self.assertAlmostEqual(a[1], b[1], places=9)

    def test_empty_span(self):
        self.assertEqual(te.repair([], 100.0), ([], []))
        self.assertTrue(te.placement_ok([], 100.0))

    def test_word_zero_without_start_is_placed_before_the_next_word(self):
        ws = [[None, 8.56, "Hey."], [8.56, 8.9, "gel"], [8.9, 9.3, "buraya."], [10.0, 10.4, "hadi"]]
        kept, _ = te.repair([list(w) for w in ws], 60.0)
        self.assert_placement(kept, ws, 60.0)
        self.assertAlmostEqual(kept[0][1], 8.56)
        self.assertTrue(8.56 - 1.2 <= kept[0][0] < 8.56, kept[0])
        self.assertEqual(kept[1][:2], (8.56, 8.9))

    def test_word_past_the_audio_end_does_not_move_dense_speech(self):
        dur, ws, t = 100.0, [], 88.0
        for k in range(30):
            ws.append([round(t, 2), round(t + 0.4, 2), f"w{k}"]); t += 0.4
        ws[-1][1] = 99.9
        ws.append([99.9, 104.0, "x"])
        kept, events = te.repair([list(w) for w in ws], dur)
        self.assert_placement(kept, ws, dur)
        for w, k in zip(ws[:-1], kept[:-1]):
            self.assertLessEqual(abs(k[0] - w[0]), 0.05)
            self.assertLessEqual(abs(k[1] - w[1]), 0.05)
        self.assertTrue(99.9 - 1e-9 <= kept[-1][0] and kept[-1][1] <= dur)
        self.assertIn("eof", [e["kind"] for e in events])

    def test_trailing_run_without_ends_stays_inside_the_audio(self):
        ws = [[0.0, 1.0, "a"], [1.0, 99.99, "b"]] + [[None, None, f"n{j}"] for j in range(10)]
        kept, _ = te.repair([list(w) for w in ws], 100.0)
        self.assert_placement(kept, ws, 100.0)
        ws = [[0.0, 0.5, "a"], [0.5, 1.0, "b"]] + [[None, None, f"n{j}"] for j in range(3)]
        kept, _ = te.repair([list(w) for w in ws], 3.0)
        self.assert_placement(kept, ws, 3.0)
        self.assertEqual(kept[2][0], 1.0)

    def test_misplaced_block_is_shifted_rigidly(self):
        ws, t = [], 10.0
        for k in range(40):                              # ends 10.5 .. 60.0
            ws.append([round(t, 2), round(t + 0.5, 2), f"p{k}"]); t += 1.25
        block = []
        for k in range(8):                               # 8 words timed about 45 s too early
            block.append([30.0 + 0.4 * k, 30.3 + 0.4 * k, f"b{k}"])
        after = [[75.0 + 0.5 * k, 75.4 + 0.5 * k, f"u{k}"] for k in range(10)]
        ws = ws + block + after
        kept, events = te.repair([list(w) for w in ws], 100.0)
        self.assert_placement(kept, ws, 100.0)
        moved = [kept[40 + k][1] - block[k][1] for k in range(8)]
        self.assertTrue(all(abs(m - moved[0]) < 1e-6 for m in moved), moved)
        self.assertTrue(kept[39][1] <= kept[40][0] and kept[47][1] <= 75.0)
        self.assertEqual([(e["kind"], len(e["idx"])) for e in events], [("block", 8)])
        self.assertGreater(events[0]["shift_s"], 40)

    def test_start_trim_only_for_swallowed_pauses_and_impossible_lengths(self):
        ws = [[1.0, 1.4, "bir"], [1.4, 2.6, "uzun"], [3.0, 7.9, "iki"], [7.9, 8.2, "üç"],
              [9.0, 16.0, "dört"], [16.0, 16.3, "beş"], [16.3, 19.0, "1941"]]
        kept, _ = te.repair([list(w) for w in ws], 30.0)
        self.assertEqual(kept[1][0], 1.4)                 # 1.2 s after the previous end: raw start kept
        self.assertEqual(kept[2][0], 3.0)                 # 4.9 s after a real pause: raw start kept
        self.assertAlmostEqual(kept[4][1], 16.0)          # 7 s "word": start cannot be right
        self.assertGreater(kept[4][0], 14.7)
        self.assertEqual(kept[6][0], 16.3)                # digits are never trimmed

    def test_random_input_keeps_every_word_in_order_inside_the_audio(self):
        rng = random.Random(11)
        for _ in range(400):
            dur = rng.uniform(5.0, 300.0)
            ws = []
            for k in range(rng.randint(0, 80)):
                s = rng.choice([None, rng.uniform(-5, dur + 10)])
                e = rng.choice([None, rng.uniform(-5, dur + 10), (s or 0.0) + rng.uniform(-1, 3)])
                ws.append([s, e, rng.choice(["a", "kelime", "12", "، ", "x."])])
            kept, events = te.repair([list(w) for w in ws], dur)
            self.assert_placement(kept, ws, dur)
            for ev in events:
                self.assertTrue(set(ev["idx"]) <= set(range(len(ws))))

    def test_collapse_records_removed_loops(self):
        ws = [[i * 0.5, i * 0.5 + 0.4, t] for i, t in enumerate(["x", "a", "b", "a", "b", "a", "b", "a", "b", "y"])]
        loops = []
        out, removed = te.collapse(ws, loops)
        self.assertEqual(removed, 4)
        self.assertEqual([w[2] for w in out], ["x", "a", "b", "a", "b", "y"])
        self.assertEqual(loops, [{"start": 0.5, "end": 4.4, "phrase": ["a", "b"], "repeats": 4, "removed": 4}])


def ms_invariants(testcase, cues, texts, total):
    ms = te.ms
    testcase.assertEqual([c[2] for c in cues], texts)
    for i, (a, b, _) in enumerate(cues):
        testcase.assertLess(ms(a), ms(b))
        testcase.assertGreaterEqual(ms(a), 0)
        testcase.assertLessEqual(ms(b), int(total * 1000))
        if i:
            testcase.assertGreaterEqual(ms(a), ms(cues[i - 1][1]) + te.MIN_GAP_MS)
    testcase.assertEqual(te.cue_violations(cues, total), {"inverted": 0, "out_of_order": 0, "overlap": 0, "outside": 0})


class CueTimingPass(unittest.TestCase):
    def test_random_cue_lists(self):
        rng = random.Random(5)
        for trial in range(2000):
            n = rng.randint(1, 14)
            total = max(rng.uniform(1.0, 40.0), n * 0.2)
            cues = []
            for k in range(n):
                a = rng.uniform(-1.0, total + 2.0)
                b = a + rng.uniform(-3.0, 4.0)
                cues.append([a, b, f"c{k}", b + rng.uniform(-0.5, 0.5)])
            out, stats = te.timing_pass(cues, total)
            ms_invariants(self, out, [c[2] for c in cues], total)

    def test_inverted_seam(self):
        # a chunk's last words timed past the next chunk's first word: the later cue in subtitle
        # order starts before the earlier one, which the previous gap loop turned into an inverted cue
        cues = [[100.0, 101.5, "önce.", 101.5], [104.10, 104.54, "ha ha ha ha.", 104.54],
                [103.49, 103.94, "Hadi.", 103.94], [105.0, 106.0, "sonra.", 106.0]]
        out, stats = te.timing_pass(cues, 200.0)
        ms_invariants(self, out, [c[2] for c in cues], 200.0)
        self.assertEqual(stats["pulled"] + stats["pushed"], len(stats["moves"]))
        self.assertEqual(len(stats["moves"]), 1)
        # pulling the earlier cue back and pushing the later one on cost the same here; pull wins
        self.assertEqual((stats["moves"][0]["kind"], stats["moves"][0]["cue"], stats["moves"][0]["shift_s"]), ("pulled", 2, 1.13))
        self.assertEqual(out[1][:2], [102.97, 103.41])
        self.assertEqual(out[0][0], 100.0)
        self.assertEqual(out[3][0], 105.0)

    def test_burst_of_short_sentences_does_not_drift(self):
        cues, t = [], 50.0
        for k in range(12):
            cues.append([round(t, 3), round(t + 0.08, 3), f"H{k}.", round(t + 0.08, 3)]); t += 0.15
        cues.append([52.0, 53.0, "Gel buraya.", 53.0])
        out, stats = te.timing_pass(cues, 100.0)
        ms_invariants(self, out, [c[2] for c in cues], 100.0)
        for before, after in zip(cues, out):
            self.assertEqual(te.ms(before[0]), te.ms(after[0]))
        self.assertEqual(stats["pulled"] + stats["pushed"], 0)

    def test_short_cue_held_for_reading_only_into_free_time(self):
        cues = [[10.0, 10.3, "Evet.", 10.3], [10.9, 11.2, "Hayır.", 11.2], [20.0, 20.2, "Tamam.", 20.2]]
        out, stats = te.timing_pass(cues, 30.0)
        self.assertEqual(out[0][1], 10.82)       # up to the next start minus the gap
        self.assertEqual(out[1][1], 11.9)        # the full second
        self.assertEqual(out[2][1], 21.0)
        self.assertEqual(stats["extended"], 3)
        out, _ = te.timing_pass([[28.5, 29.9, "son.", 29.9]], 30.0)
        self.assertEqual(out[0][1], 30.0)        # linger stops at the end of the episode


def words_of(text, start=1.0, step=0.3, pause_after=None):
    out, t = [], start
    for k, w in enumerate(text.split()):
        out.append((round(t, 3), round(t + step - 0.05, 3), w))
        t += step + (pause_after.get(k, 0.0) if pause_after else 0.0)
    return out


class CueBuilder(unittest.TestCase):
    def test_tokens_are_preserved_in_order(self):
        rng = random.Random(9)
        vocab = ["ve", "bir", "kelime.", "söz,", "yok?", "في", "كلمة", "نعم؟", "x" * 90, "12", "…"]
        for _ in range(200):
            words, t = [], 0.0
            for k in range(rng.randint(0, 120)):
                s = t + rng.choice([0.0, 0.1, 0.8, 1.4, 3.0])
                words.append((s, s + rng.uniform(0.05, 0.9), rng.choice(vocab)))
                t = words[-1][1]
            for lang in ("tr-TR", "ar-XA", "auto"):
                groups = te.cue_groups(words, lang)
                self.assertEqual([k for a, b in groups for k in range(a, b)], list(range(len(words))))
                cues, _, builder = te.build_subtitle(words, lang, t + 5.0)
                self.assertEqual(builder, "greedy")
                self.assertTrue(te.cues_follow_words(cues, words))

    def test_arabic_question_mark_ends_a_cue(self):
        words = words_of("من أنت؟ أنا صديق")
        self.assertEqual(te.cue_groups(words, "ar-XA"), [(0, 2), (2, 4)])

    def test_back_off_at_the_character_limit(self):
        toks = [f"kelime{k:02d}" for k in range(14)]
        toks[5] += ","
        words = words_of(" ".join(toks), step=0.3)
        groups = te.cue_groups(words, "tr-TR")
        self.assertEqual(groups[0], (0, 6))           # cut after the comma, not at the 84th character
        self.assertEqual(te.cue_groups_legacy(words)[0], (0, 10))
        self.assertTrue(all(len(" ".join(w[2] for w in words[a:b])) <= 84 for a, b in groups))

    def test_function_word_bridges_a_short_pause(self):
        words = words_of("seni çok özledim ve seni çok bekledim.", pause_after={3: 1.0})
        self.assertEqual(te.cue_groups(words, "tr-TR"), [(0, 7)])
        self.assertEqual(te.cue_groups(words, "en-US"), [(0, 4), (4, 7)])   # Turkish list only for tr
        words = words_of("ذهبت إلى السوق الكبير يوم الجمعة.", pause_after={1: 1.0})
        self.assertEqual(te.cue_groups(words, "auto"), [(0, 6)])            # Arabic list on Arabic tokens
        self.assertEqual(te.cue_groups(words, "tr-TR"), [(0, 2), (2, 6)])

    def test_orphan_sentence_tail_is_pulled_back(self):
        words = words_of("bunu sana kaç kere söyledim artık yeter.", pause_after={4: 1.2})
        self.assertEqual(te.cue_groups(words, "tr-TR"), [(0, 7)])
        words = words_of("bunu sana kaç kere söyledim artık yeter.", pause_after={3: 1.2})
        self.assertEqual(te.cue_groups(words, "tr-TR"), [(0, 4), (4, 7)])
        words = words_of("bunu sana kaç kere söyledim artık yeter.", pause_after={4: 1.6})
        self.assertEqual(te.cue_groups(words, "tr-TR"), [(0, 5), (5, 7)])   # 1.5 s or more still splits

    def test_legacy_builder_selectable(self):
        words = words_of("seni çok özledim ve bekledim.", pause_after={3: 1.0})
        with mock.patch.dict(os.environ, {"SE_STT_CUE_BUILDER": "legacy"}):
            cues, _, builder = te.build_subtitle(words, "tr-TR", 20.0)
        self.assertEqual(builder, "legacy")
        self.assertEqual([c[2] for c in cues], ["seni çok özledim ve", "bekledim."])

    def test_failed_self_check_falls_back_to_the_previous_builder(self):
        words = words_of("bir iki üç.")
        with mock.patch.object(te, "cue_groups", lambda w, lang: [(0, 1), (2, 3)]), mock.patch.object(te, "log") as logged:
            cues, _, builder = te.build_subtitle(words, "tr-TR", 20.0)
        self.assertEqual(builder, "legacy")
        self.assertIn("WARNING: cue check failed", logged.call_args[0][0])
        self.assertTrue(te.cues_follow_words(cues, words))

    def test_speech_seconds_merges_overlapping_words(self):
        self.assertAlmostEqual(te.speech_seconds([(0.0, 1.0, "a"), (0.5, 2.0, "b"), (3.0, 3.5, "c")]), 2.5)
        self.assertEqual(te.speech_seconds([]), 0.0)


VOCAB = ["ve", "bir", "bu", "ne", "sen", "ben", "gel", "git", "evet", "hayır", "tamam", "şimdi",
         "burada", "orada", "kalem", "masa", "yol", "ev", "çay", "su", "baba", "anne", "kardeş,", "hadi."]

def dialogue(rng, start, end, prefix="", step=0.4):
    out, t, k = [], start, 0
    while t + step <= end:
        word = rng.choice(VOCAB) if not prefix else f"{prefix}{k}"
        out.append((round(t, 3), round(t + step - 0.05, 3), word))
        t += step; k += 1
    return out

def recovered(words, est=False):
    return [{"s": s, "e": e, "w": w, "est": est} for s, e, w in words]


class HoleClassifier(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(21)
        self.before = dialogue(self.rng, 100.0, 160.0)
        self.after = dialogue(self.rng, 220.0, 280.0)
        self.a, self.z = self.before[-1][1], self.after[0][0]

    def edges(self):
        """recovered copies of the words either side of the gap, as the padded pieces return them"""
        return [w for w in self.before if w[0] >= self.a - 2.0] + [w for w in self.after if w[0] < self.z + 2.0]

    def test_dialogue_hole_is_all_new(self):
        hole = []
        for s, e, w in dialogue(self.rng, 161.0, 219.0):
            hole.append((s, e, w))                        # common words, including ones seen at the edges
        rec = sorted(recovered(self.edges() + hole), key=lambda x: x["s"])
        res = te.classify_gap(self.before + self.after, rec, self.a, self.z, "tr-TR")
        self.assertFalse(res["displaced"])
        self.assertEqual(len(res["new"]), len(hole))
        self.assertEqual(res["present"], [])

    def test_displaced_run_inserts_nothing(self):
        truth = dialogue(self.rng, 161.0, 200.0, prefix="söz")
        packed, t = [], self.a + 0.01
        for _, _, w in truth:                              # the model timed them right after the last word
            packed.append((round(t, 3), round(t + 0.04, 3), w)); t += 0.05
        existing = self.before + packed + self.after
        a = packed[-1][1]
        rec = sorted(recovered([x for x in existing if a - 2.0 <= x[0] < a] + truth + self.edges()[-5:]), key=lambda x: x["s"])
        res = te.classify_gap(existing, rec, a, self.z, "tr-TR")
        self.assertTrue(res["displaced"])
        self.assertGreaterEqual(res["run_words"], te.RUN_MIN)

    def test_numbers_are_kept_and_junk_filtered(self):
        hole = [(170.0, 170.5, "1941"), (171.0, 171.3, "٦٦"), (172.0, 172.3, "..."), (173.0, 173.4, "yıl")]
        rec = recovered(hole[:3]) + recovered([hole[3]], est=True)
        res = te.classify_gap(self.before + self.after, rec, self.a, self.z, "tr-TR")
        self.assertEqual([x["w"] for x in res["new"]], ["1941", "٦٦"])
        self.assertEqual([x["w"] for x in res["filtered"]], ["...", "yıl"])
        res = te.classify_gap(self.before + self.after, recovered([(170.0, 170.5, "66"), (171.0, 171.5, "x]")]),
                              self.a, self.z, "ar-XA")
        self.assertEqual([x["w"] for x in res["new"]], ["66"])

    def test_loop_removed_by_collapse_is_already_represented(self):
        chant = [(165.0 + i, 165.8 + i, w) for i, w in enumerate(["نحن", "هنا!", "نحن", "هنا", "أهلا"])]
        loops = [{"start": 150.0, "end": 215.0, "phrase": ["نحن", "هنا"], "repeats": 30, "removed": 56}]
        res = te.classify_gap(self.before + self.after, recovered(chant), self.a, self.z, "ar-XA", loops)
        self.assertEqual([x["w"] for x in res["new"]], ["أهلا"])
        self.assertEqual(len(res["present"]), 4)
        res = te.classify_gap(self.before + self.after, recovered(chant), self.a, self.z, "ar-XA",
                              [dict(loops[0], start=300.0, end=320.0)])
        self.assertEqual(len(res["new"]), 5)             # a loop elsewhere does not hide these words


class Gaps(unittest.TestCase):
    def test_gap_min_is_read_when_called(self):
        words = [(0.0, 1.0, "a"), (21.0, 22.0, "b"), (40.0, 41.0, "c")]
        with mock.patch.dict(os.environ, {"SE_STT_GAP_MIN": "19"}):
            self.assertEqual([g[2] for g in te.find_gaps(words, 41.0)], [1])
        with mock.patch.dict(os.environ, {"SE_STT_GAP_MIN": "10"}):
            self.assertEqual([g[2] for g in te.find_gaps(words, 60.0)], [1, 2, 3])
        with mock.patch.dict(os.environ, {"SE_STT_GAP_MIN": ""}):
            self.assertEqual(te.gap_min(), 15.0)

    def test_gaps_use_the_running_end_and_include_head_and_tail(self):
        words = [(20.0, 21.0, "a"), (22.0, 90.0, "long"), (30.0, 31.0, "seam"), (95.0, 96.0, "b")]
        self.assertEqual(te.find_gaps(words, 130.0, minimum=15.0), [(0.0, 20.0, 0), (96.0, 130.0, 4)])
        self.assertEqual(te.find_gaps([], 100.0, minimum=15.0), [(0.0, 100.0, 0)])

    def test_pieces_cover_the_region_with_owned_middles(self):
        pieces = te.plan_pieces(305.123, 673.48, 9000.0)
        self.assertEqual(pieces[0][0], 303.0)
        self.assertEqual(pieces[-1][1], 675.5)
        self.assertTrue(all(p[1] - p[0] <= te.PIECE for p in pieces))
        owned = [(p[2], p[3]) for p in pieces]
        self.assertEqual(owned[0][0], 303.0)
        self.assertEqual(owned[-1][1], 675.5)
        self.assertTrue(all(owned[k][1] == owned[k + 1][0] for k in range(len(owned) - 1)))
        # gap edges a few milliseconds off map to the same cached pieces
        self.assertEqual(te.plan_pieces(305.170, 673.44, 9000.0), pieces)
        self.assertTrue(all(p[1] <= 50.0 for p in te.plan_pieces(10.0, 50.0, 50.0)))


def piece_response(truth, s, e):
    return response([(round(w0 - s, 3), round(w1 - s, 3), w) for w0, w1, w in truth if w0 >= s and w1 <= e],
                    billed=f"{int(math.ceil(e - s))}s")


class Recovery(unittest.TestCase):
    def test_missing_speech_is_inserted_before_the_next_word_and_nothing_else_moves(self):
        rng = random.Random(4)
        before, after = dialogue(rng, 0.5, 120.0), dialogue(rng, 180.0, 300.0)
        hole = dialogue(rng, 121.0, 179.0, prefix="kayıp")
        truth = before + hole + after
        existing = before + after
        seen = []
        def fetch(pieces):
            seen.extend(pieces)
            return {te.piece_tag(s, e): piece_response(truth, s, e) for s, e, _, _ in pieces}
        with mock.patch.object(te, "log"):
            out, stats = te.recover_holes(existing, 300.0, "tr-TR", fetch)
        self.assertEqual([w[2] for w in out], [w[2] for w in truth])
        it = iter(out)
        self.assertTrue(all(any(x is y for y in it) for x in existing))     # same objects, same order
        self.assertEqual(stats["inserted"], len(hole))
        self.assertEqual(stats["inserted_ranges"], [{"start": hole[0][0], "end": hole[-1][1], "words": len(hole),
                                                     "gap_start": before[-1][1], "gap_end": after[0][0]}])
        self.assertGreaterEqual(stats["already_present"], 0)
        self.assertEqual(stats["pieces"], len(seen))

    def test_displaced_run_gap_inserts_nothing_and_is_reported(self):
        rng = random.Random(6)
        before, after = dialogue(rng, 0.5, 120.0), dialogue(rng, 200.0, 320.0)
        spoken = dialogue(rng, 150.0, 190.0, prefix="söz")          # really spoken inside the gap
        packed, t = [], before[-1][1] + 0.01
        for _, _, w in spoken:                                      # but timed right after the last word
            packed.append((round(t, 3), round(t + 0.04, 3), w)); t += 0.05
        existing = before + packed + after
        truth = before + spoken + after
        def fetch(pieces):
            return {te.piece_tag(s, e): piece_response(truth, s, e) for s, e, _, _ in pieces}
        with mock.patch.object(te, "log"):
            out, stats = te.recover_holes(existing, 320.0, "tr-TR", fetch)
        self.assertEqual(out, existing)
        self.assertEqual(stats["inserted"], 0)
        self.assertEqual(len(stats["displaced_runs"]), 1)
        self.assertGreaterEqual(stats["displaced_runs"][0]["words"], te.RUN_MIN)

    def test_too_little_speech_skips_recovery(self):
        words = [(0.0, 1.0, "a"), (100.0, 101.0, "b")]
        out, stats = te.recover_holes(words, 600.0, "tr-TR", lambda p: self.fail("must not fetch"))
        self.assertEqual(out, words)
        self.assertEqual(stats["status"], "too little speech recognized")

    def test_failed_piece_leaves_its_gap_unrecovered(self):
        rng = random.Random(8)
        existing = dialogue(rng, 0.5, 120.0) + dialogue(rng, 180.0, 300.0)
        with mock.patch.object(te, "log"):
            out, stats = te.recover_holes(existing, 300.0, "tr-TR", lambda pieces: {})
        self.assertEqual(out, existing)
        self.assertEqual([s["reason"] for s in stats["skipped"]], ["recognition failed"])

    def test_budget_skips_the_gaps_that_do_not_fit(self):
        rng = random.Random(9)
        existing = dialogue(rng, 0.5, 20.0) + dialogue(rng, 80.0, 100.0) + dialogue(rng, 250.0, 270.0)
        with mock.patch.object(te, "log"), mock.patch.object(te, "BUDGET_SHARE", 0.5), \
                mock.patch.object(te, "MIN_SPEECH_SHARE", 0.0):
            out, stats = te.recover_holes(existing, 270.0, "tr-TR", lambda pieces: {te.piece_tag(s, e): response([]) for s, e, _, _ in pieces})
        # longest first: the 150 s gap does not fit half of 270 s, the 60 s gap still does
        self.assertEqual([(round(s["start"]), s["reason"]) for s in stats["skipped"]], [(100, "budget")])
        self.assertEqual(stats["gaps_checked"], 2)
        self.assertGreater(stats["pieces"], 0)


class PieceFetch(TempWorkMixin, unittest.TestCase):
    def test_cached_fresh_failed_and_unfinished_pieces(self):
        ep = self.episode()
        ep.deploy = lambda: ("p", "b", "gcloud")
        os.makedirs(f"{ep.raw}/rec", exist_ok=True)
        pieces = [(100.0, 145.0, 100.0, 143.0), (141.0, 186.0, 143.0, 184.0), (182.0, 227.0, 184.0, 225.0), (223.0, 250.0, 225.0, 250.0)]
        ep.save_response(f"{ep.raw}/rec", te.piece_tag(100.0, 145.0), 100.0, 145.0, response([(1.0, 1.4, "var")]))
        ep.fetched = 0
        calls = {"cut": 0, "upload": 0, "submit": 0}
        def cut(s, e, path):
            calls["cut"] += 1
            with open(path, "w") as fh: fh.write("audio")
            return path
        def upload(path, tag):
            calls["upload"] += 1
            ep.uploaded = True
            return "uri-" + tag
        def submit(folder, tag, s, e, uri, attempts=1):
            calls["submit"] += 1
            path = f"{folder}/{tag}.op.json"
            te.atomic_write(path, json.dumps({"name": "op-" + tag, "start_ms": te.ms(s), "end_ms": te.ms(e), "submitted": time.time()}))
            ep.pending.add(path)
            return "op-" + tag
        def poll(name, **kw):
            if name.endswith(te.piece_tag(141.0, 186.0)):
                return response([(2.0, 2.5, "yeni")], billed="45s")
            if name.endswith(te.piece_tag(182.0, 227.0)):
                st = response([]); st["error"] = {"message": "internal"}; return st
            return None                                    # never finishes
        ep.cut, ep.upload, ep.submit, ep.poll = cut, upload, submit, poll
        clock = [0.0]
        def fake_time():
            return clock[0]
        def fake_sleep(sec):
            clock[0] += sec
        with mock.patch.object(te.time, "sleep", fake_sleep), mock.patch.object(te.time, "time", fake_time), \
                mock.patch.object(te, "log"):
            got = ep.fetch_pieces(pieces)
        tags = [te.piece_tag(p[0], p[1]) for p in pieces]
        self.assertIsNotNone(got[tags[0]])
        self.assertIsNotNone(got[tags[1]])
        self.assertIsNone(got[tags[2]])                     # failed
        self.assertEqual(got[tags[3]], "pending")           # still running when the wait ended
        self.assertEqual(calls, {"cut": 3, "upload": 3, "submit": 3})
        self.assertEqual((ep.reused, ep.fetched, ep.billed), (1, 1, 45.0 + 46.0))
        self.assertFalse(os.path.exists(f"{ep.raw}/rec/{tags[2]}.op.json"))
        self.assertTrue(os.path.exists(f"{ep.raw}/rec/{tags[3]}.op.json"))   # a later run resumes it
        self.assertEqual(ep.pending, {f"{ep.raw}/rec/{tags[3]}.op.json"})
        self.assertEqual(os.listdir(f"{self.tmp}/rec"), [])                  # local piece audio removed


class ChecksAndNotes(TempWorkMixin, unittest.TestCase):
    def test_sanitizer_replaces_dashes(self):
        self.assertEqual(te.sanitize("a \u2013 b \u2014 c \u2012 d \u2015 e - f"), "a - b - c - d - e - f")

    def test_words_inside_one_silence(self):
        silence = [(10.0, 20.0), (20.5, 30.0)]
        words = [(11.0, 11.5, "a"), (12.0, 12.4, "b"), (19.0, 19.9, "c"), (20.6, 21.0, "d"), (22.0, 22.3, "e"), (23.0, 23.5, "f")]
        self.assertEqual(te.words_in_silence(words, silence), [(11.0, 19.9, 3), (20.6, 23.5, 3)])
        self.assertEqual(te.words_in_silence(words[1:5], silence), [])     # runs of 2, split across two silences

    def test_checks_levels(self):
        words = [(float(t), t + 0.4, "w") for t in range(0, 120)] + [(float(t), t + 0.4, "w") for t in range(240, 300)]
        cues = [[w[0], w[1], w[2]] for w in words]
        events = [{"start": 50.0, "end": 55.0, "kind": "block", "words": 9, "shift_s": 64.0},
                  {"start": 80.0, "end": 80.5, "kind": "outlier", "words": 1, "shift_s": 1.2}]
        rec = dict(te.empty_recovery(), status="ran", inserted_ranges=[{"start": 121.0, "end": 150.0, "words": 40}],
                   skipped=[{"start": 170.0, "end": 200.0, "reason": "budget"}])
        v = te.run_checks(cues, words, 300.0, 300.0, [(130.0, 131.5)], events, rec, [0.0, 300.0])
        self.assertEqual(v["status"], "warn")
        self.assertEqual(sorted(f["check"] for f in v["warns"]), ["gap_not_checked", "timing_moved"])
        # the 120 s gap overlaps a stretch that was not checked, so it is not called music
        self.assertEqual([f["check"] for f in v["notes"]], ["recovered"])
        v = te.run_checks(cues, words, 300.0, 300.0, [], [], dict(te.empty_recovery(), status="disabled"), [0.0, 300.0])
        self.assertEqual([f["check"] for f in v["notes"]], ["no_words"])        # not called music when nobody looked
        bad = [[5.0, 4.0, "ters"]]
        v = te.run_checks(bad, [(4.0, 5.0, "ters")], 10.0, 10.0, [], [], dict(te.empty_recovery(), status="disabled"), [0.0, 10.0])
        self.assertEqual(v["status"], "fail")
        v = te.run_checks([], [], 600.0, 600.0, [], [], dict(te.empty_recovery(), status="too little speech recognized"), [0.0, 600.0])
        self.assertEqual([f["check"] for f in v["fails"]], ["too_little_speech"])

    def test_notes_merge_overlaps_and_quote_arabic_on_its_own_line(self):
        words = [(10.0 + i, 10.5 + i, w) for i, w in enumerate("من أنت يا صديقي ماذا تريد مني الآن".split())]
        cues = [[10.0, 13.5, "من أنت يا صديقي"], [14.0, 17.5, "ماذا تريد مني الآن"]]
        verify = {"status": "warn", "fails": [],
                  "warns": [te.finding("words_in_silence", 12.0, 14.5, "3 words timed inside silence", words=3)],
                  "notes": [te.finding("recovered", 10.0, 17.5, "recognition skipped this stretch, 8 words recovered", words=8),
                            te.finding("no_speech", 100.0, 160.0, "no speech found, probably music")]}
        text = te.render_notes(cues, words, 3.0, verify, dict(te.empty_recovery(), inserted=8), [], {"pulled": 0, "pushed": 0})
        lines = text.splitlines()
        item = [l for l in lines if l.startswith("- ")]
        self.assertEqual(len(item), 1)
        self.assertIn("00:00:10 to 00:00:18 (cues 1 to 2)", item[0])
        self.assertIn("recognition skipped this stretch, 8 words recovered; 3 words timed inside silence", item[0])
        quote = lines[lines.index(item[0]) + 1]
        self.assertTrue(quote.startswith('  "من أنت'))
        self.assertIn("00:01:40 to 00:02:40", text)
        self.assertNotRegex(text, "[\u2013\u2014]")

    def test_finish_writes_report_and_notes_without_identifiers(self):
        os.environ.update({"SE_STT_PROJECT": "secret-project-123", "SE_STT_BUCKET": "secret-bucket-456",
                           "SE_STT_SERVICE_ACCOUNT": "robot@secret-project-123.iam.gserviceaccount.com"})
        try:
            ep = te.Episode(os.path.join(self.tmp, "Dizi Adi 7. Bolum.mp4"), os.path.join(self.tmp, "Dizi Adi 7. Bolum.srt"), self.tmp)
        finally:
            for key in ("SE_STT_PROJECT", "SE_STT_BUCKET", "SE_STT_SERVICE_ACCOUNT"):
                os.environ.pop(key, None)
        ep.silence, ep.timing_events, ep.billed, ep.fetched, ep.reused = [], [], 0.0, 0, 3
        words = [(1.0, 1.5, "merhaba"), (1.5, 2.0, "dünya.")]
        cues, stats, builder = te.build_subtitle(words, "tr-TR", 60.0)
        rec = dict(te.empty_recovery(), status="ran")
        with mock.patch.object(te, "log") as logged:
            ep.finish(cues, words, 60.0, 60.0, [0.0, 60.0], 0, rec, stats, builder, 2, 0, 0)
        report = te.read_json(os.path.join(self.tmp, "report.json"))
        for key in ("video", "minutes", "chunks", "boundaries_snapped_to_silence", "google_words", "subtitle_words",
                    "cues", "speech_density_pct", "verify", "recovery", "timing_events", "cue_pass",
                    "billed_seconds_this_run", "responses_reused", "responses_fetched", "pending_operations",
                    "builder_version", "settings"):
            self.assertIn(key, report)
        self.assertEqual(report["builder_version"], "2026-09-14")
        with open(os.path.join(self.tmp, "Dizi Adi 7. Bolum - transcription notes.md"), encoding="utf-8") as fh:
            notes = fh.read()
        shared = json.dumps(report, ensure_ascii=False) + notes + " ".join(str(c.args[0]) for c in logged.call_args_list if "wrote" not in str(c.args[0]) and "notes:" not in str(c.args[0]) and "report:" not in str(c.args[0]))
        for secret in ("secret-project", "secret-bucket", "robot@", "gs://", "Dizi Adi"):
            self.assertNotIn(secret, shared)
        self.assertIn("all responses reused, nothing billed", " ".join(str(c.args[0]) for c in logged.call_args_list))


class SpanWiring(TempWorkMixin, unittest.TestCase):
    def run_span(self, responses, start, end, tag="part-000"):
        ep = self.episode()
        ep.quiet = []
        ep.span_response = lambda s, e, t: responses[t]
        with mock.patch.object(te, "log"):
            return ep, ep.transcribe_span(start, end, tag)

    def test_empty_chunk(self):
        ep, (words, events, loops) = self.run_span({"part-000": response([], billed="200s")}, 0.0, 200.0)
        self.assertEqual((words, events, loops), ([], [], []))
        self.assertEqual(ep.spans[0]["words"], 0)

    def test_tail_splice_keeps_the_previous_word_sequence(self):
        parent = [(round(0.5 * i, 2), round(0.5 * i + 0.5, 2), f"w{i}") for i in range(100)]
        parent += [(50.0, None, "N"), (60.0, 60.3, "M")]
        tail = [(0.5, 0.9, "t1"), (0.9, 1.3, "t2")]
        ep, (words, _, _) = self.run_span({"part-000": response(parent, billed="200s"),
                                           "part-000t": response(tail, billed="141s")}, 0.0, 200.0)
        # the previous placement put N at 50.46, before the splice point; the new one packs it
        # right before M at about 59.9, after the splice point. Membership must not change.
        self.assertEqual([w[2] for w in words], [f"w{i}" for i in range(100)] + ["N", "t1", "t2"])
        self.assertGreater(words[100][0], 59.0)
        self.assertEqual(ep.spans[0]["dropped"], 1)
        self.assertAlmostEqual(words[101][0], 59.3 + 0.5)


# ---------- review round 1: one test or more per confirmed problem ----------

class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0
    def time(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class PieceStub:
    """cut, upload and submit for an Episode, recording calls; submit writes the operation record."""
    def __init__(self, ep, clock=None):
        self.ep, self.calls = ep, {"cut": 0, "upload": 0, "submit": 0}
        ep.deploy = lambda: ("p", "b", "gcloud")
        ep.cut, ep.upload, ep.submit = self.cut, self.upload, self.submit
        self.clock = clock
    def cut(self, s, e, path):
        self.calls["cut"] += 1
        with open(path, "w") as fh:
            fh.write("audio")
        return path
    def upload(self, path, tag):
        self.calls["upload"] += 1
        self.ep.uploaded = True
        return "uri-" + tag
    def submit(self, folder, tag, s, e, uri, attempts=1):
        self.calls["submit"] += 1
        path = f"{folder}/{tag}.op.json"
        te.atomic_write(path, json.dumps({"name": f"op-new-{self.calls['submit']}", "start_ms": te.ms(s), "end_ms": te.ms(e),
                                          "submitted": self.clock.time() if self.clock else time.time(),
                                          "prefix": self.ep.prefix, "attempts": attempts}))
        self.ep.pending.add(path)
        return f"op-new-{self.calls['submit']}"


class NotesKeepStretchesApart(unittest.TestCase):
    """A whole-episode finding (recovery failed, too little speech, broken cues) must not swallow the
    stretches the editor has to find."""
    def build(self, rec, cues=None):
        words = [(float(t), t + 0.5, f"k{t}") for t in range(0, 2774, 2)]
        cues = cues or [[words[i][0], words[min(i + 2, len(words) - 1)][1], " ".join(w[2] for w in words[i:i + 3])]
                        for i in range(0, len(words), 3)]
        events = [{"start": 215.0, "end": 219.0, "kind": "head", "words": 13, "shift_s": 216.4}]
        verify = te.run_checks(cues, words, 2774.0, 2774.0, [], events, rec, [0.0, 2774.0])
        return te.render_notes(cues, words, 46.0, verify, rec, events, {"pulled": 0, "pushed": 0})

    def test_recovery_failed_and_too_little_speech(self):
        for rec in (dict(te.empty_recovery(), status="failed"), dict(te.empty_recovery(), status="too little speech recognized")):
            items = [l for l in self.build(rec).splitlines() if l.startswith("- ")]
            self.assertEqual(len(items), 2, items)
            self.assertTrue(items[0].startswith("- Whole episode: "), items)
            self.assertTrue(items[1].startswith("- 00:03:35 to 00:03:39 (cue 37): 13 words were moved 216 s later"), items)

    def test_broken_cues(self):
        words = [(float(t), t + 0.5, f"k{t}") for t in range(0, 2774, 2)]
        cues = [[words[i][0], words[min(i + 2, len(words) - 1)][1], " ".join(w[2] for w in words[i:i + 3])] for i in range(0, len(words), 3)]
        cues[5][0], cues[5][1] = cues[5][1], cues[5][0]          # one inverted cue
        items = [l for l in self.build(dict(te.empty_recovery(), status="ran"), cues).splitlines() if l.startswith("- ")]
        self.assertTrue(items[0].startswith("- Whole episode: the subtitle has broken cues"), items)
        self.assertTrue(any(l.startswith("- 00:03:35 to 00:03:39") for l in items), items)

    def test_unknown_status_still_renders(self):
        text = te.render_notes([[0.0, 1.0, "a"]], [(0.0, 1.0, "a")], 1.0, {"status": "unknown", "fails": [], "warns": [], "notes": []},
                               te.empty_recovery(), [], {})
        self.assertIn("the checks could not run", text)


class PieceSeam(unittest.TestCase):
    def test_first_word_that_swallowed_the_seam_is_kept_exactly_once(self):
        # the earlier piece's audio ends inside the word; the later piece returns it with a start 1.6 s
        # early, before its share of the audio begins
        earlier = response([(38.0, 38.4, "bir"), (38.5, 39.0, "iki"), (40.44, 40.9, "bazılar"), (42.96, 43.32, "uğrunca")])
        later = response([(1.56, 4.96, "Kabullenemesek"), (4.96, 5.08, "de"), (5.08, 5.24, "onun"), (5.24, 5.9, "gidişini")])
        got = te.piece_words(earlier, 347.0, 392.0, 349.0, 390.0, "a") + te.piece_words(later, 388.0, 433.0, 390.0, 431.0, "b")
        self.assertEqual([x["w"] for x in sorted(got, key=lambda x: x["s"])],
                         ["bir", "iki", "bazılar", "uğrunca", "Kabullenemesek", "de", "onun", "gidişini"])

    def test_word_starting_before_the_boundary_stays_with_the_earlier_piece(self):
        earlier = response([(40.0, 40.4, "bir"), (42.8, 43.3, "sınır")])       # 389.8 to 390.3
        later = response([(2.3, 2.6, "sonra")])
        got = te.piece_words(earlier, 347.0, 392.0, 349.0, 390.0, "a") + te.piece_words(later, 388.0, 433.0, 390.0, 431.0, "b")
        self.assertEqual([x["w"] for x in got], ["bir", "sınır", "sonra"])


class SpellingVariants(unittest.TestCase):
    EXISTING = ["اهلا", "اهلا", "بقيل", "شبار", "معين", "ابن", "الاكثم", "حللت", "ووطئت", "سهلا"]

    def classify(self, heard):
        existing = [(100.0 + 0.26 * i, 100.24 + 0.26 * i, w) for i, w in enumerate(self.EXISTING)]
        rec = recovered([(97.0 + 0.4 * i, 97.35 + 0.4 * i, w) for i, w in enumerate(heard)])
        # the piece runs 2 s past the gap and returns the words there too
        rec += recovered([(101.4 + 0.26 * i, 101.64 + 0.26 * i, w) for i, w in enumerate(self.EXISTING[6:])])
        return te.classify_gap(existing, rec, 70.0, 100.0, "ar-XA")

    def test_variants_between_aligned_copies_are_not_inserted(self):
        res = self.classify(["اهلا", "اهلا", "بقيل", "جبار", "معين", "بن"])
        self.assertEqual(res["new"], [])
        self.assertFalse(res["displaced"])

    def test_unrelated_words_in_the_same_place_are_still_new(self):
        res = self.classify(["اهلا", "اهلا", "بقيل", "كتاب", "معين", "طريق"])
        self.assertEqual([x["w"] for x in res["new"]], ["كتاب", "طريق"])

    def test_capital_i_outside_turkish(self):
        self.assertEqual(te.norm_key("It", "en-US"), te.norm_key("it", "en-US"))
        self.assertEqual(te.norm_key("İstanbul", "en-US"), "istanbul")
        self.assertEqual(te.norm_key("IŞIK", "tr-TR"), "ışık")
        existing = [(90.0, 90.4, "Then"), (99.0, 99.4, "yes"), (130.0, 130.3, "It"), (130.4, 130.8, "works")]
        res = te.classify_gap(existing, recovered([(129.0, 129.3, "it")]), 99.4, 130.0, "en-US")
        self.assertEqual(res["new"], [])


def arabic_dialogue(rng, start, end, prefix, step=0.4):
    return [(round(t, 3), round(t + step - 0.05, 3), f"{prefix}{k}") for k, t in enumerate(frange(start, end, step))]

def frange(start, end, step):
    t = start
    while t + step <= end:
        yield t
        t += step


class MisplacedOpening(unittest.TestCase):
    """An opening line timed well before where it is spoken must not be inserted a second time where
    recovery hears it, and the notes must point at the early copy."""
    def run_case(self, existing, truth, audio_end, events=(), silence=()):
        def fetch(pieces):
            return {te.piece_tag(s, e): piece_response(truth, s, e) for s, e, _, _ in pieces}
        with mock.patch.object(te, "log"):
            return te.recover_holes(existing, audio_end, "ar-XA", fetch, span_starts=[0.0], events=events, silence=silence)

    def test_line_timed_outside_the_aligned_stretch(self):
        rng = random.Random(31)
        before, after = arabic_dialogue(rng, 20.0, 150.0, "قبل"), arabic_dialogue(rng, 217.0, 400.0, "بعد")
        early = [(161.84, 162.1, "ما"), (162.1, 162.4, "الامر"), (162.4, 162.6, "يا"), (162.6, 162.98, "سارديه؟")]
        told = [(193.56, 194.0, "تكلم.")]
        spoken = [(213.0, 213.3, "الامر"), (213.3, 213.5, "يا"), (213.5, 214.1, "سارداي؟")]
        events = [{"start": 161.83, "end": 162.98, "kind": "head", "words": 4, "shift_s": 161.4}]
        out, stats = self.run_case(before + early + told + after, before + told + spoken + after, 400.0, events)
        self.assertEqual(stats["inserted"], 0)
        self.assertEqual(len(stats["misplaced"]), 1)
        self.assertGreaterEqual(stats["misplaced"][0]["words"], 3)
        self.assertAlmostEqual(stats["misplaced"][0]["run_start"], 162.1)
        self.assertGreater(stats["misplaced"][0]["offset_s"], 40)

    def test_name_spelled_differently_next_to_the_matched_words(self):
        rng = random.Random(32)
        early = [(1.47, 1.7, "لم"), (1.7, 2.0, "اقصد"), (2.0, 2.2, "هذا"), (2.2, 2.4, "يا"), (2.4, 2.68, "شاما")]
        after = arabic_dialogue(rng, 213.84, 400.0, "بعد")
        spoken = [(211.4, 211.6, "لم"), (211.6, 211.9, "اقصد"), (211.9, 212.1, "هذا"), (212.1, 212.22, "يا"), (212.22, 212.6, "شامه")]
        out, stats = self.run_case(early + after, spoken + after, 400.0, silence=[(0.5, 10.0)])
        self.assertEqual(stats["inserted"], 0)
        self.assertEqual([m["words"] for m in stats["misplaced"]], [5])

    def test_copies_of_correctly_timed_words_are_not_reported(self):
        rng = random.Random(33)
        before, after = arabic_dialogue(rng, 20.0, 180.0, "قبل"), arabic_dialogue(rng, 200.0, 400.0, "بعد")
        # the pieces return the last line before the gap 10 s late, inside the gap
        late = [(w[0] + 10.0, w[1] + 10.0, w[2]) for w in before[-4:]]
        out, stats = self.run_case(before + after, before + late + after, 400.0)
        self.assertEqual(stats["inserted"], 0)
        self.assertEqual(stats["misplaced"], [])


class RecoveredNoise(unittest.TestCase):
    def setUp(self):
        rng = random.Random(41)
        self.before, self.after = arabic_dialogue(rng, 100.0, 160.0, "قبل"), arabic_dialogue(rng, 220.0, 280.0, "بعد")
        self.a, self.z = self.before[-1][1], self.after[0][0]

    def classify(self, hole, lang="ar-XA", silence=()):
        return te.classify_gap(self.before + self.after, recovered(hole), self.a, self.z, lang, silence=silence)

    def test_a_gap_of_sighs_inserts_nothing(self):
        res = self.classify([(170.0 + i, 170.5 + i, w) for i, w in enumerate(["ااه", "ااه", "ا", "ااه", "ههه", "اوووه"])])
        self.assertEqual((res["new"], res["noise"]), ([], 6))
        res = self.classify([(170.0, 170.4, "Hı"), (170.5, 170.9, "hı."), (175.0, 175.4, "Ah!")], "tr-TR")
        self.assertEqual(res["new"], [])

    def test_real_short_words_are_still_inserted(self):
        res = self.classify([(170.0, 170.3, "يا"), (170.4, 170.9, "شامه")])
        self.assertEqual([x["w"] for x in res["new"]], ["يا", "شامه"])
        res = self.classify([(170.0, 170.3, "ااه"), (170.4, 170.9, "شامه")])
        self.assertEqual([x["w"] for x in res["new"]], ["ااه", "شامه"])       # a sigh inside real speech stays

    def test_latin_fragment_under_arabic_is_filtered_and_numbers_are_kept(self):
        res = self.classify([(170.0, 170.3, "6Y"), (171.0, 171.4, "100"), (172.0, 172.4, "٦٦")])
        self.assertEqual([x["w"] for x in res["new"]], ["100", "٦٦"])

    def test_leaked_model_instructions_are_filtered_in_any_language(self):
        # A bumper screen's outro music produced literal fragments of the model's own hidden
        # instructions to itself instead of a transcript, in both an Arabic and a Turkish gap.
        res = self.classify([(170.0, 170.3, "MUSIC]"), (171.0, 171.4, "شامه")])
        self.assertEqual([x["w"] for x in res["new"]], ["شامه"])
        res = self.classify([(170.0, 170.3, "BACKGROUND]"), (171.0, 171.4, "Tüh"), (171.5, 171.8, "be.")], "tr-TR")
        self.assertEqual([x["w"] for x in res["new"]], ["Tüh", "be."])

    def test_an_ordinary_word_that_spells_a_leaked_token_is_not_filtered(self):
        # Only a stray half of the model's own bracket tag is filtered: someone actually saying
        # "music" or "background", with no bracket left on it, is real speech.
        res = self.classify([(170.0, 170.3, "Music."), (171.0, 171.4, "background"), (172.0, 172.4, "Şey.")], "tr-TR")
        self.assertEqual([x["w"] for x in res["new"]], ["Music.", "background", "Şey."])

    def test_word_mixing_latin_and_cyrillic_letters_is_filtered(self):
        # Real speech over another music bumper came back as "Bip" (kept: an ordinary-looking Latin
        # token) next to "Bпрочем", one word splicing Latin B onto a Cyrillic word: no language
        # mixes scripts inside one token like that.
        res = self.classify([(170.0, 170.3, "Bip."), (171.0, 171.4, "Bпрочем."), (172.0, 172.4, "Aslanlar!")], "tr-TR")
        self.assertEqual([x["w"] for x in res["new"]], ["Bip.", "Aslanlar!"])

    def test_word_inside_a_silence_is_filtered_only_when_it_kept_its_own_timing(self):
        heard = [{"s": 171.0, "e": 171.5, "w": "كلمة", "est": False, "anchored": True},
                 {"s": 175.0, "e": 175.5, "w": "اخرى", "est": False, "anchored": False}]
        res = te.classify_gap(self.before + self.after, heard, self.a, self.z, "ar-XA", silence=[(170.5, 172.0), (174.0, 176.0)])
        self.assertEqual([x["w"] for x in res["filtered"]], ["كلمة"])
        self.assertEqual([x["w"] for x in res["new"]], ["اخرى"])


class DisplacedRunNotes(unittest.TestCase):
    def test_long_displaced_run_is_a_warning_at_the_misplaced_words(self):
        words = [(float(t), t + 0.5, f"w{t}") for t in range(0, 300)] + [(float(t), t + 0.5, f"w{t}") for t in range(500, 600)]
        cues = [[words[i][0], words[min(i + 2, len(words) - 1)][1], " ".join(w[2] for w in words[i:i + 3])] for i in range(0, len(words), 3)]
        rec = dict(te.empty_recovery(), status="ran",
                   displaced_runs=[{"start": 299.5, "end": 500.0, "words": 25, "run_start": 100.0, "run_end": 124.6, "offset_s": 327.6}])
        verify = te.run_checks(cues, words, 600.0, 600.0, [], [], rec, [0.0, 600.0])
        self.assertEqual([f["check"] for f in verify["warns"]], ["displaced_run"])
        self.assertEqual((verify["warns"][0]["start"], verify["warns"][0]["end"]), (100.0, 124.6))
        text = te.render_notes(cues, words, 10.0, verify, rec, [], {})
        item = [l for l in text.splitlines() if l.startswith("- ")]
        self.assertEqual(len(item), 1)
        self.assertTrue(item[0].startswith("- 00:01:40 to 00:02:05 (cues 34 to 42): 25 words here were recognized again about 328 s later"), item)
        self.assertIn("| Subtitle words recognized again somewhere else (listed above) | 25 in 1 places |", text)

    def test_short_nearby_run_is_a_note_and_timing_moves_do_not_claim_a_place(self):
        rec = dict(te.empty_recovery(), status="ran",
                   displaced_runs=[{"start": 781.5, "end": 812.6, "words": 5, "run_start": 778.9, "run_end": 781.5, "offset_s": 4.1}])
        events = [{"start": 50.0, "end": 55.0, "kind": "head", "words": 3, "shift_s": 161.4}]
        verify = te.run_checks([], [], 900.0, 900.0, [], events, rec, [0.0, 900.0])
        self.assertIn("displaced_run", [f["check"] for f in verify["notes"]])
        self.assertNotIn("timed wrong", next(f["detail"] for f in verify["notes"] if f["check"] == "displaced_run"))
        self.assertEqual(verify["warns"][0]["detail"], "3 words were moved 161 s later by the timing repair, check the timing")


class TailOpening(TempWorkMixin, unittest.TestCase):
    GARBAGE = [[162.88, 162.96, "ههه"], [163.04, 163.16, "ههه"], [186.52, 22.72, "ه"], [22.72, 186.64, "ههه"],
               [186.68, 186.8, "ههه"], [186.84, 23.04, "ه"], [23.04, 186.96, "ههه"]]

    def test_last_line_before_laughter_stays_in_place(self):
        ws = [[None, 0.16, "انت"], [0.16, 0.28, "يا"], [0.28, 0.56, "صقر"], [0.56, 1.04, "ديس."]] + self.GARBAGE
        for head in (False, True):
            kept, _ = te.repair([list(w) for w in ws], 203.6, head)
            self.assertTrue(all(k[1] <= 1.04 + 1e-9 for k in kept[:4]), (head, kept[:4]))

    def test_a_truncation_tail_is_placed_without_the_opening_repair(self):
        parent = [(round(0.5 * i, 2), round(0.5 * i + 0.5, 2), f"w{i}") for i in range(100)] + [(60.0, 60.3, "M")]
        tail = [(None, 0.16, "t1"), (0.16, 0.28, "t2"), (0.28, 0.56, "t3"), (0.56, 1.04, "t4"),
                (162.88, 162.96, "u"), (186.52, 22.72, "v"), (22.72, 186.64, "x"), (186.68, 186.8, "y"), (330.0, 330.4, "z")]
        # This tail's own offsets are bad enough (22% of it repaired) to be anomalous on their
        # own, so it is now re-cut into "ta" and "tb": "ta" starts where the tail did, so it
        # keeps mid_speech and this test still holds across that split.
        ta = [(None, 0.16, "t1"), (0.16, 0.28, "t2"), (0.28, 0.56, "t3"), (0.56, 1.04, "t4")]
        tb = [(0.0, 0.3, "z1")]
        ep = self.episode()
        ep.quiet = []
        responses = {"part-000": response(parent, billed="400s"), "part-000t": response(tail, billed="341s"),
                     "part-000ta": response(ta, billed="171s"), "part-000tb": response(tb, billed="171s")}
        ep.span_response = lambda s, e, t: responses[t]
        with mock.patch.object(te, "log"):
            words, _, _ = ep.transcribe_span(0.0, 400.0, "part-000")
        placed = {w[2]: w for w in words}
        resume = 60.3 - 1.0
        for k in ("t1", "t2", "t3", "t4"):
            self.assertLessEqual(placed[k][1], resume + 1.04 + 1e-6, placed[k])

    def test_an_anomalous_tail_is_re_cut_once_and_a_clean_half_is_not_re_cut_again(self):
        # A tail (depth 1) that loops or breaks as badly as a fresh chunk gets the same one-time
        # re-cut; a resulting half (depth 2) that is not itself anomalous gets no further one.
        parent = [(round(0.5 * i, 2), round(0.5 * i + 0.5, 2), f"w{i}") for i in range(100)] + [(60.0, 60.3, "M")]
        looped_phrase = [(60.3 + 0.3 * k, 60.3 + 0.3 * k + 0.2, "Ey") for k in range(60)]
        tail = looped_phrase + [(330.0, 330.4, "z")]
        ta = [(0.0, 0.3, "a1")]
        tb = [(0.0, 0.3, "b1")]
        ep = self.episode()
        ep.quiet = []
        responses = {"part-000": response(parent, billed="400s"), "part-000t": response(tail, billed="341s"),
                     "part-000ta": response(ta, billed="171s"), "part-000tb": response(tb, billed="171s")}
        ep.span_response = lambda s, e, t: responses[t]
        tags_asked = []
        real_span_response = ep.span_response
        ep.span_response = lambda s, e, t: (tags_asked.append(t), real_span_response(s, e, t))[1]
        with mock.patch.object(te, "log"):
            words, _, _ = ep.transcribe_span(0.0, 400.0, "part-000")
        self.assertIn("part-000ta", tags_asked)
        self.assertIn("part-000tb", tags_asked)
        self.assertNotIn("part-000taa", tags_asked)
        self.assertNotIn("part-000tab", tags_asked)
        self.assertEqual({w[2] for w in words} & {"a1", "b1"}, {"a1", "b1"})

    def test_a_tails_own_re_cut_half_is_re_cut_again_if_it_is_still_anomalous(self):
        # The exact shape found live: a long tail loops badly, its own re-cut first half (depth 2,
        # still mid_speech since it starts where the tail did) is STILL anomalous, and used to
        # never get a second chance because only depth 0 and 1 qualified. It now does. The episode
        # is long enough that halving twice still leaves each half over the 240 s floor.
        total = 700.0
        parent = [(round(0.5 * i, 2), round(0.5 * i + 0.5, 2), f"w{i}") for i in range(100)] + [(60.0, 60.3, "M")]
        looped_phrase = [(0.3 * k, 0.3 * k + 0.2, "Ey") for k in range(60)]
        tail = looped_phrase + [(total - 70.0, total - 69.6, "z")]
        ta_looped = [(0.3 * k, 0.3 * k + 0.2, "Oh") for k in range(60)]
        ta = ta_looped + [(300.0, 300.4, "y")]
        taa, tab = [(0.0, 0.3, "aa1")], [(0.0, 0.3, "ab1")]
        tb = [(0.0, 0.3, "b1")]
        ep = self.episode()
        ep.quiet = []
        responses = {"part-000": response(parent, billed="400s"), "part-000t": response(tail, billed="641s"),
                     "part-000ta": response(ta, billed="321s"), "part-000tb": response(tb, billed="321s"),
                     "part-000taa": response(taa, billed="161s"), "part-000tab": response(tab, billed="161s")}
        ep.span_response = lambda s, e, t: responses[t]
        tags_asked = []
        real_span_response = ep.span_response
        ep.span_response = lambda s, e, t: (tags_asked.append(t), real_span_response(s, e, t))[1]
        with mock.patch.object(te, "log"):
            words, _, _ = ep.transcribe_span(0.0, total, "part-000")
        self.assertIn("part-000ta", tags_asked)
        self.assertIn("part-000taa", tags_asked)
        self.assertIn("part-000tab", tags_asked)
        self.assertEqual({w[2] for w in words} & {"aa1", "ab1", "b1"}, {"aa1", "ab1", "b1"})

    def test_re_cutting_a_tail_stops_at_max_recut_depth_even_if_still_anomalous(self):
        # A tail that keeps coming back anomalous no matter how it is cut is a lost cause, not an
        # excuse to keep spending. Every half here stays well over the 240 s floor, so it is
        # MAX_RECUT_DEPTH, not dur, that has to be what ends it.
        total = 5000.0
        parent = [(round(0.5 * i, 2), round(0.5 * i + 0.5, 2), f"w{i}") for i in range(100)] + [(60.0, 60.3, "M")]

        def looped(width):
            return [(0.3 * k, 0.3 * k + 0.2, "Ey") for k in range(60)] + [(width - 10.0, width - 9.6, "z")]

        responses = {"part-000": response(parent, billed="400s")}
        tag, dur = "part-000t", total - 59.3
        for depth in range(1, te.MAX_RECUT_DEPTH + 3):
            self.assertGreater(dur, 240.0, "test fixture must stay above the dur floor throughout")
            responses[tag] = response(looped(dur), billed=f"{int(dur)}s")
            responses[tag + "b"] = response([(0.0, 0.3, "x")], billed="1s")
            dur = dur / 2
            tag = tag + "a"
        ep = self.episode()
        ep.quiet = []
        ep.span_response = lambda s, e, t: responses[t]
        tags_asked = []
        real_span_response = ep.span_response
        ep.span_response = lambda s, e, t: (tags_asked.append(t), real_span_response(s, e, t))[1]
        with mock.patch.object(te, "log"):
            ep.transcribe_span(0.0, total, "part-000")
        deepest = max((t for t in tags_asked if t.startswith("part-000t")), key=len)
        self.assertLessEqual(len(deepest) - len("part-000t"), te.MAX_RECUT_DEPTH, tags_asked)


class HeldOperations(TempWorkMixin, unittest.TestCase):
    PIECE = (2627.0, 2672.0, 2629.0, 2670.0)

    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.ep = self.episode()
        self.stub = PieceStub(self.ep, self.clock)
        self.tag = te.piece_tag(*self.PIECE[:2])
        os.makedirs(f"{self.ep.raw}/rec", exist_ok=True)

    def left_by_earlier_run(self, age, attempts=1):
        te.atomic_write(f"{self.ep.raw}/rec/{self.tag}.op.json", json.dumps(
            {"name": "op-old", "start_ms": te.ms(self.PIECE[0]), "end_ms": te.ms(self.PIECE[1]),
             "submitted": self.clock.time() - age, "prefix": "stt/111-1/", "attempts": attempts}))
        te.atomic_write(f"{self.tmp}/uploads.json", json.dumps(["stt/111-1/"]))

    def fetch(self, poll):
        self.ep.poll = poll
        with mock.patch.object(te.time, "sleep", self.clock.sleep), mock.patch.object(te.time, "time", self.clock.time), \
                mock.patch.object(te, "log") as logged:
            start = self.clock.time()
            got = self.ep.fetch_pieces([self.PIECE])
        return got, self.clock.time() - start, " ".join(str(c.args[0]) for c in logged.call_args_list)

    def test_failed_old_operation_is_sent_again_once_with_a_log_line_and_pending_billing(self):
        self.left_by_earlier_run(3.5 * 3600)
        def poll(name, **kw):
            if name == "op-old":
                st = response([]); st["error"] = {"message": "An internal error occurred."}; return st
            return None
        got, _, logged = self.fetch(poll)
        self.assertIn("the earlier operation failed", logged)
        self.assertIn("submitting again", logged)
        self.assertEqual(got[self.tag], "pending")
        self.assertEqual(self.stub.calls, {"cut": 1, "upload": 1, "submit": 1})
        self.assertEqual(te.read_json(f"{self.ep.raw}/rec/{self.tag}.op.json")["attempts"], 2)
        removed = []
        self.ep.run_proc = lambda args: removed.append(args[-1]) or subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(te, "log"):
            self.ep.cleanup()
        self.assertEqual(removed, ["gs://b/stt/111-1/"])                    # nothing waits on the earlier prefix any more
        self.assertEqual(te.read_json(f"{self.tmp}/uploads.json"), [self.ep.prefix])
        self.ep.silence, self.ep.timing_events = [], []
        words = [(1.0, 1.5, "bir"), (1.5, 2.0, "iki.")]
        cues, stats, builder = te.build_subtitle(words, "tr-TR", 60.0)
        with mock.patch.object(te, "log") as logged:
            self.ep.finish(cues, words, 60.0, 60.0, [0.0, 60.0], 0, dict(te.empty_recovery(), status="ran"), stats, builder, 2, 0, 0)
        text = " ".join(str(c.args[0]) for c in logged.call_args_list)
        self.assertIn("nothing billed yet; 1 operation(s) still pending, billed when they finish", text)
        self.assertNotIn("all responses reused", text)
        self.assertEqual(te.read_json(f"{self.tmp}/report.json")["pending_operations"], 1)

    def test_old_operation_still_running_is_only_polled_briefly(self):
        self.left_by_earlier_run(3.5 * 3600)
        got, waited, _ = self.fetch(lambda name, **kw: None)
        self.assertEqual(got[self.tag], "pending")
        self.assertLessEqual(waited, te.REC_RESUME_WAIT + te.REC_POLL)
        self.assertEqual(self.stub.calls["submit"], 0)

    def test_recent_interrupted_operation_keeps_the_rest_of_its_time(self):
        self.left_by_earlier_run(60.0)
        got, waited, _ = self.fetch(lambda name, **kw: None)
        self.assertGreater(waited, te.REC_TIMEOUT - 60.0 - te.REC_POLL)
        self.assertLessEqual(waited, te.REC_TIMEOUT)

    def test_piece_that_failed_twice_is_not_sent_a_third_time(self):
        self.left_by_earlier_run(3600, attempts=2)
        def poll(name, **kw):
            st = response([]); st["error"] = {"message": "internal"}; return st
        got, _, _ = self.fetch(poll)
        self.assertIsNone(got[self.tag])
        self.assertEqual(self.stub.calls["cut"], 0)
        self.assertFalse(os.path.exists(f"{self.ep.raw}/rec/{self.tag}.op.json"))


class KilledRun(TempWorkMixin, unittest.TestCase):
    def test_prefix_is_listed_before_audio_is_uploaded_and_a_later_run_removes_it(self):
        ep = self.episode()
        ep.deploy = lambda: ("p", "b", "gcloud")
        ep.cut = lambda s, e, p: p
        listed_at_upload = []
        def upload(local, tag):
            listed_at_upload.append(te.read_json(f"{self.tmp}/uploads.json"))       # what a kill right now leaves
            ep.uploaded = True
            return "uri"
        ep.upload = upload
        ep.api = lambda method, url, body=None, **kw: {"name": "operation-9"}
        class Killed(BaseException):
            pass
        def poll(name, **kw):
            raise Killed()                   # SIGKILL or a closed terminal: no finally, no cleanup
        ep.poll = poll
        with mock.patch.object(te.time, "sleep"), mock.patch.object(te, "log"):
            with self.assertRaises(Killed):
                ep.recognize(0.0, 45.0, "part-004")
        self.assertEqual(listed_at_upload, [[ep.prefix]])
        later = self.episode()
        later.prefix = "stt/2-2/"
        later.deploy = lambda: ("p", "b", "gcloud")
        later.poll = lambda name, **kw: response([(0.0, 0.5, "bir")], billed="45s")
        removed = []
        later.run_proc = lambda args: removed.append(args[-1]) or subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(te.time, "sleep"), mock.patch.object(te, "log"):
            later.recognize(0.0, 45.0, "part-004")
            later.cleanup()
        self.assertEqual(removed, ["gs://b/" + ep.prefix])
        self.assertEqual(te.read_json(f"{self.tmp}/uploads.json"), [])


class LostAnswers(TempWorkMixin, unittest.TestCase):
    def call(self, outcomes, method="GET", attempts=5):
        ep = self.episode()
        ep.token = lambda refresh=False: "t"
        sent = []
        class Resp:
            def __init__(self, item): self.item = item
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                if isinstance(self.item, BaseException):
                    raise self.item
                return self.item
        def urlopen(req, timeout):
            sent.append(req.get_method())
            return Resp(outcomes.pop(0))
        with mock.patch.object(te.urllib.request, "urlopen", urlopen), mock.patch.object(te.time, "sleep"):
            try:
                return ep.api(method, "https://example.invalid/v2/projects/secret-project/x", {} if method == "POST" else None,
                              attempts=attempts), sent, None
            except SystemExit as e:
                return None, sent, e

    def test_status_request_is_asked_again_after_a_lost_answer(self):
        for lost in (http.client.IncompleteRead(b"partial body", 992), ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
                     b"<html>proxy error</html>"):
            result, sent, err = self.call([lost, b'{"done": true}'])
            self.assertEqual((result, len(sent), err), ({"done": True}, 2, None), lost)

    def test_submission_is_not_sent_twice(self):
        result, sent, err = self.call([http.client.IncompleteRead(b"partial body", 10), b'{"name": "op"}'], method="POST")
        self.assertEqual(len(sent), 1)
        self.assertIsInstance(err, te.RequestFailed)

    def test_giving_up_raises_without_url_or_partial_body(self):
        result, sent, err = self.call([http.client.IncompleteRead(b"partial body", 10)] * 5)
        self.assertEqual(len(sent), 5)
        self.assertIsInstance(err, SystemExit)
        for secret in ("secret-project", "example.invalid", "partial body"):
            self.assertNotIn(secret, str(err))


class NetworkOutage(TempWorkMixin, unittest.TestCase):
    def pieces(self, n):
        return [(100.0 + 41 * k, 145.0 + 41 * k, 102.0 + 41 * k, 143.0 + 41 * k) for k in range(n)]

    def test_outage_ends_the_wait_long_before_the_timeout(self):
        ep, clock = self.episode(), FakeClock()
        stub = PieceStub(ep, clock)
        polls = []
        def poll(name, **kw):
            polls.append(kw)
            raise te.RequestFailed("GET request failed: timed out")
        ep.poll = poll
        with mock.patch.object(te.time, "sleep", clock.sleep), mock.patch.object(te.time, "time", clock.time), mock.patch.object(te, "log"):
            start = clock.time()
            got = ep.fetch_pieces(self.pieces(51))
        self.assertLessEqual(clock.time() - start, (te.REC_BAD_ROUNDS + 1) * te.REC_POLL)
        self.assertTrue(all(v == "pending" for v in got.values()))
        self.assertEqual(len(ep.pending), 51)                                 # a later run resumes them
        self.assertTrue(all(kw.get("attempts") == 1 for kw in polls))

    def test_rejected_status_request_fails_that_piece_once(self):
        ep, clock = self.episode(), FakeClock()
        PieceStub(ep, clock)
        polls = []
        def poll(name, **kw):
            polls.append(name)
            raise te.RequestFailed("GET request failed with HTTP 403: denied", 403)
        ep.poll = poll
        with mock.patch.object(te.time, "sleep", clock.sleep), mock.patch.object(te.time, "time", clock.time), mock.patch.object(te, "log"):
            got = ep.fetch_pieces(self.pieces(1))
        self.assertEqual(list(got.values()), [None])
        self.assertEqual(len(polls), 1)
        self.assertEqual(ep.pending, set())

    def test_locked_temporary_file_does_not_stop_recovery(self):
        ep, clock = self.episode(), FakeClock()
        stub = PieceStub(ep, clock)
        ep.poll = lambda name, **kw: response([(1.0, 1.4, "var")])
        def locked(path):
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        with mock.patch.object(te.time, "sleep", clock.sleep), mock.patch.object(te.time, "time", clock.time), \
                mock.patch.object(te, "log"), mock.patch.object(te.os, "remove", locked):
            got = ep.fetch_pieces(self.pieces(4))
        self.assertEqual(stub.calls["submit"], 4)
        self.assertTrue(all(isinstance(v, dict) for v in got.values()))


class ConsoleEncoding(unittest.TestCase):
    def test_log_survives_a_console_that_cannot_encode_the_file_name(self):
        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="cp1252")
        with mock.patch.object(te.sys, "stdout", stream):
            te.log("wrote 3 cues to Örnek Çığlık Dosyası 7.srt")      # ı and ğ are not in cp1252
            stream.flush()
        self.assertIn(b"wrote 3 cues to \xd6rnek \xc7", buf.getvalue())


class EmptyInsideRecovered(unittest.TestCase):
    def test_stretch_a_piece_left_unanswered_is_reported_and_the_whole_gap_is_listed(self):
        rng = random.Random(12)
        before, after = dialogue(rng, 0.5, 300.0), dialogue(rng, 680.0, 900.0)
        hole = dialogue(rng, 352.0, 670.0, prefix="kayıp")
        truth = before + hole + after
        def fetch(pieces):
            got = {}
            for s, e, _, _ in pieces:
                if s < 300.0:       # this piece answers with its first second only
                    got[te.piece_tag(s, e)] = response([(0.2, 0.5, "dur"), (0.5, 0.9, "dur")], billed=f"{int(math.ceil(e - s))}s")
                else:
                    got[te.piece_tag(s, e)] = piece_response(truth, s, e)
            return got
        with mock.patch.object(te, "log"):
            out, stats = te.recover_holes(before + after, 900.0, "tr-TR", fetch)
        gap_a = before[-1][1]
        self.assertEqual(len(stats["still_empty"]), 1)
        self.assertAlmostEqual(stats["still_empty"][0]["start"], gap_a)
        self.assertAlmostEqual(stats["still_empty"][0]["end"], hole[0][0], places=3)
        cues, _, _ = te.build_subtitle(out, "tr-TR", 900.0)
        verify = te.run_checks(cues, out, 900.0, 900.0, [], [], stats, [0.0, 900.0])
        self.assertIn("gap_still_empty", [f["check"] for f in verify["warns"]])
        self.assertEqual([f["start"] for f in verify["notes"] if f["check"] == "recovered"], [round(gap_a, 2)])
        text = te.render_notes(cues, out, 15.0, verify, stats, [], {})
        item = [l for l in text.splitlines() if l.startswith("- ")][0]
        self.assertTrue(item.startswith(f"- {te.clock(gap_a)} to {te.clock(after[0][0])}"), item)
        self.assertIn("recognition skipped this stretch", item.split(";")[0])

    def test_music_between_recovered_lines_is_not_reported(self):
        rng = random.Random(13)
        before, after = dialogue(rng, 0.5, 300.0), dialogue(rng, 680.0, 900.0)
        hole = dialogue(rng, 310.0, 400.0, prefix="kayıp") + dialogue(rng, 450.0, 670.0, prefix="geri")
        truth = before + hole + after
        with mock.patch.object(te, "log"):
            out, stats = te.recover_holes(before + after, 900.0, "tr-TR",
                                          lambda pieces: {te.piece_tag(s, e): piece_response(truth, s, e) for s, e, _, _ in pieces})
        self.assertGreater(stats["inserted"], 0)
        self.assertEqual(stats["still_empty"], [])


class FirstPieceOfAGap(unittest.TestCase):
    def test_words_before_the_gap_are_not_moved_into_it(self):
        rng = random.Random(51)
        edge = [(280.24, 280.6, "Şu"), (280.6, 280.8, "an"), (280.8, 280.96, "mı"), (280.96, 281.06, "yok,"),
                (281.06, 281.3, "ölüm"), (281.3, 281.38, "mü"), (281.38, 281.5, "yok?"), (281.5, 281.74, "Nerede"),
                (281.74, 281.84, "yok"), (281.84, 282.06, "la?")]
        after = [(297.18, 297.38, "Ee,"), (297.38, 298.0, "bozkırdayız.")] + dialogue(rng, 299.5, 500.0)
        existing = dialogue(rng, 0.5, 280.0) + edge + after
        def fetch(pieces):
            got = {}
            for s, e, _, _ in pieces:
                if abs(s - 280.0) < 0.01:
                    heard = [(round(w0 - s, 3), None if w == "ölüm" else round(w1 - s, 3), w) for w0, w1, w in edge]
                    heard += [(round(w0 - s, 3), round(w1 - s, 3), w) for w0, w1, w in after if w1 <= e]
                    got[te.piece_tag(s, e)] = response(heard, billed=f"{int(math.ceil(e - s))}s")
                else:
                    got[te.piece_tag(s, e)] = piece_response(existing, s, e)
            return got
        with mock.patch.object(te, "log"):
            out, stats = te.recover_holes(existing, 500.0, "tr-TR", fetch)
        self.assertEqual((stats["displaced_runs"], stats["inserted"]), ([], 0))

    def test_piece_starting_inside_music_still_gets_its_opening_repaired(self):
        # compressed toward the piece start, spoken 23 s later: no subtitle words at the piece start
        st = response([(None, 0.08, "Her"), (0.08, 0.8, "nefes"), (0.8, 1.32, "zamanın"), (1.32, 24.72, "üstüne")], billed="26s")
        context = [(105.55, 105.75, "Her"), (105.77, 106.0, "nefes"), (106.07, 106.4, "zamanın"), (106.47, 106.8, "üstüne")]
        got = te.piece_words(st, 82.0, 108.0, 82.0, 108.0, "x", context, "tr-TR")
        self.assertGreater(got[0]["s"], 100.0)


class ShortRecoveredWords(unittest.TestCase):
    def test_short_stretches_are_listed_with_their_time(self):
        words = [(float(t), t + 0.4, "w") for t in range(0, 60)] + [(1297.0, 1297.5, "الله"), (1298.0, 1298.5, "الله")] + \
                [(float(t), t + 0.4, "w") for t in range(1400, 1460)]
        cues = [[w[0], w[1], w[2]] for w in words]
        rec = dict(te.empty_recovery(), status="ran", inserted=2,
                   inserted_ranges=[{"start": 1297.0, "end": 1298.5, "words": 2, "gap_start": 60.4, "gap_end": 1400.0}])
        verify = te.run_checks(cues, words, 1460.0, 1460.0, [], [], rec, [0.0, 1460.0])
        text = te.render_notes(cues, words, 24.0, verify, rec, [], {})
        self.assertIn("## Short recovered words", text)
        self.assertIn("00:21:37 (cues 61 to 62)", text)
        self.assertIn("(0 listed above, 1 as short words)", text)

    def test_quote_starts_with_the_first_recovered_word(self):
        words = [(101.006 + 0.4 * k, 101.3 + 0.4 * k, f"yeni{k + 101}") for k in range(58)]
        cues = [[words[i][0], words[min(i + 5, 57)][1], " ".join(w[2] for w in words[i:i + 6])] for i in range(0, 58, 6)]
        rec = dict(te.empty_recovery(), status="ran", inserted=58,
                   inserted_ranges=[{"start": 101.006, "end": words[-1][1], "words": 58, "gap_start": 80.0, "gap_end": 130.0}])
        verify = te.run_checks(cues, words, 200.0, 200.0, [], [], rec, [0.0, 200.0])
        text = te.render_notes(cues, words, 3.0, verify, rec, [], {})
        self.assertIn('"yeni101 yeni102', text)


class NotesDetails(TempWorkMixin, unittest.TestCase):
    def test_merged_item_does_not_repeat_a_sentence(self):
        rec = dict(te.empty_recovery(), status="ran", skipped=[{"start": 1397.0, "end": 1440.0, "reason": "recognition failed"},
                                                                 {"start": 1440.0, "end": 1484.0, "reason": "recognition failed"}])
        verify = te.run_checks([], [], 3000.0, 3000.0, [], [], rec, [0.0, 3000.0])
        text = te.render_notes([], [], 50.0, verify, rec, [], {})
        item = [l for l in text.splitlines() if l.startswith("- ")]
        self.assertEqual(len(item), 1)
        self.assertEqual(item[0].count("could not be checked for missing speech"), 1)

    def test_stretch_without_words_is_between_cues(self):
        cues = [[10.0, 11.1, "a b"], [30.0, 31.0, "c"]]
        words = [(10.0, 10.4, "a"), (10.5, 11.0, "b"), (30.0, 30.5, "c")]
        self.assertEqual(te.cue_range(cues, 11.0, 30.0, te.cue_word_spans(cues, words)), "between cues 1 and 2")
        self.assertEqual(te.cue_range(cues, 10.2, 10.6, te.cue_word_spans(cues, words)), "cue 1")

    def test_recovery_line_says_why_gaps_were_skipped(self):
        ep = self.episode()
        ep.silence, ep.timing_events = [], []
        words = [(1.0, 1.5, "bir"), (1.5, 2.0, "iki.")]
        cues, stats, builder = te.build_subtitle(words, "tr-TR", 60.0)
        rec = dict(te.empty_recovery(), status="ran", gaps_checked=5,
                   skipped=[{"start": 10.0, "end": 30.0, "reason": "budget"}, {"start": 40.0, "end": 58.0, "reason": "recognition failed"}])
        with mock.patch.object(te, "log") as logged:
            ep.finish(cues, words, 60.0, 60.0, [0.0, 60.0], 0, rec, stats, builder, 2, 0, 0)
        line = next(str(c.args[0]) for c in logged.call_args_list if str(c.args[0]).startswith("RECOVERY:"))
        self.assertTrue(line.endswith("2 gaps skipped (1 over budget, 1 failed)"), line)


class CueLimits(unittest.TestCase):
    def test_no_cue_over_84_characters_or_7_seconds(self):
        rng = random.Random(61)
        vocab = ["kelime", "uzunbirkelime", "söz,", "bitti.", "ve", "في", "كلمة", "نعم؟", "gerçekten", "12"]
        for _ in range(300):
            words, t = [], 0.0
            for _ in range(rng.randint(1, 150)):
                s = t + rng.choice([0.0, 0.05, 0.3, 0.8, 1.2])
                words.append((s, s + rng.uniform(0.1, 1.5), rng.choice(vocab)))
                t = words[-1][1]
            for lang in ("tr-TR", "ar-XA"):
                for a, b in te.cue_groups(words, lang):
                    if b - a > 1:
                        self.assertLessEqual(len(" ".join(w[2] for w in words[a:b])), te.MAX_CHARS)
                        self.assertLessEqual(words[b - 1][1] - words[a][0], te.MAX_SPAN + 1e-9)

    def test_sentence_end_that_crosses_the_limit_still_backs_off(self):
        toks = [f"kelime{k:02d}" for k in range(9)] + ["sonuncukelime."]
        words = words_of(" ".join(toks), step=0.3)
        groups = te.cue_groups(words, "tr-TR")
        self.assertTrue(all(len(" ".join(w[2] for w in words[a:b])) <= 84 for a, b in groups), groups)
        self.assertEqual([k for a, b in groups for k in range(a, b)], list(range(len(words))))


class SmallRobustness(TempWorkMixin, unittest.TestCase):
    def test_redact_names_gcloud_and_google_use(self):
        env = {"SE_STT_BUCKET": "demo-bucket-x", "SE_STT_PROJECT": "demo-project-7"}
        with mock.patch.dict(os.environ, env):
            text = te.redact("ERROR: (gcloud.storage.cp) [robot] does not have permission to access b instance [demo-bucket-x]; "
                             "API has not been used in project 123456789012 before, visit overview?project=123456789012; "
                             "projects/demo-project-7/locations/us/operations/5555555555")
        for secret in ("demo-bucket-x", "123456789012", "demo-project-7", "5555555555"):
            self.assertNotIn(secret, text)

    def test_gap_min_refuses_values_that_break_recovery(self):
        with mock.patch.object(te, "log") as logged, mock.patch.object(te, "_warned", set()):
            for value, expected in (("0", 5.0), ("-5", 5.0), ("nan", 15.0), ("inf", 15.0), ("abc", 15.0), ("9000", 600.0), ("20", 20.0)):
                with mock.patch.dict(os.environ, {"SE_STT_GAP_MIN": value}):
                    self.assertEqual(te.gap_min(), expected, value)
                    te.gap_min()
        self.assertEqual(len(logged.call_args_list), 6)             # one warning per bad value, not per call

    def test_old_work_directory_with_a_short_full_flac_is_extracted_again(self):
        ep = self.episode()
        ep.ffmpeg, ep.ffprobe = "ffmpeg", "ffprobe"
        full = f"{self.tmp}/full.flac"
        with open(full, "w") as fh:
            fh.write("half")
        lengths = {full: 285.47}
        ep.duration = lambda path: lengths.get(path)
        def sh(*args):
            with open(args[-1], "w") as fh:
                fh.write("whole")
            lengths[full] = 2632.08
            return subprocess.CompletedProcess(args, 0, "", "")
        ep.sh = sh
        with mock.patch.object(te, "log"):
            path, have = ep.prepare_audio(2632.1)
        self.assertEqual(have, 2632.08)
        self.assertEqual(te.read_json(f"{self.tmp}/audio.json")["duration_s"], 2632.08)

    def test_response_set_aside_by_a_wrong_language_run_is_used_again(self):
        ep = self.episode()
        good = response([(0.0, 0.5, "اهلا")], lang="ar-XA")
        good["_span"] = {"start_ms": 0, "end_ms": 45000, "language": "ar-XA", "model": "chirp_3", "video_bytes": 1234}
        te.atomic_write(f"{ep.raw}/part-000.json", json.dumps(good))
        with mock.patch.object(te, "log"):
            self.assertIsNone(ep.cached(ep.raw, "part-000", 0.0, 45.0))          # a run set to tr-TR moves it aside
        wrong = response([(0.0, 0.5, "merhaba")])
        wrong["_span"] = dict(good["_span"], language="tr-TR")
        te.atomic_write(f"{ep.raw}/part-000.json", json.dumps(wrong))            # and saves its own answer
        ep.lang = "ar-XA"
        with mock.patch.object(te, "log"):
            st = ep.cached(ep.raw, "part-000", 0.0, 45.0)
        self.assertEqual(st["_span"]["language"], "ar-XA")
        self.assertEqual(te.read_json(f"{ep.raw}/part-000.json")["_span"]["language"], "ar-XA")


# ---------- round 2 ----------
class ResumedOperationIsKept(TempWorkMixin, unittest.TestCase):
    """A resumed chunk operation is given up only when Google says it is gone; otherwise submitting
    again pays for audio Google already billed."""
    def setUp(self):
        super().setUp()
        self.ep = self.episode()
        self.op = f"{self.ep.raw}/part-000.op.json"
        te.atomic_write(self.op, json.dumps({"name": "operation-1", "start_ms": 0, "end_ms": 45000, "submitted": time.time(),
                                             "prefix": "stt/1-1/", "uri": "gs://b/stt/1-1/part-000.flac"}))
        self.submits = []
        self.ep.deploy = lambda: ("p", "b", "gcloud")
        self.ep.cut = lambda s, e, p: p
        self.ep.upload = lambda p, tag: "uri"
        self.ep.submit = lambda folder, tag, *a, **k: self.submits.append(tag) or "operation-2"

    def recognize(self):
        with mock.patch.object(te.time, "sleep"), mock.patch.object(te, "log"):
            self.ep.recognize(0.0, 45.0, "part-000")

    def assert_kept(self):
        self.assertTrue(os.path.exists(self.op))
        self.assertEqual(self.submits, [])

    def test_network_failure(self):
        def urlopen(req, timeout):
            raise urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided"))
        with mock.patch.object(te.urllib.request, "urlopen", urlopen):
            with self.assertRaises(te.RequestFailed):
                self.recognize()
        self.assert_kept()

    def test_token_failure(self):
        del self.ep.token                          # the real token(), with gcloud refusing
        def sh(*a):
            raise SystemExit("command failed (1): gcloud auth print-access-token\nReauthentication failed.")
        self.ep.sh = sh
        with self.assertRaises(te.RequestFailed) as caught:
            self.recognize()
        self.assertIsNone(caught.exception.status)
        self.assert_kept()

    def test_server_error_and_signal(self):
        for failure in (te.RequestFailed("GET request failed with HTTP 503: unavailable", 503), te.Stopped(143)):
            def poll(name, **kw):
                raise failure
            self.ep.poll = poll
            with self.assertRaises(type(failure)):
                self.recognize()
            self.assert_kept()


class PostIsNotSentTwice(TempWorkMixin, unittest.TestCase):
    """A real socket that reads the whole request and closes without answering."""
    def serve(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        received = []
        def loop():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    conn.settimeout(5)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    head, _, body = data.partition(b"\r\n\r\n")
                    length = re.search(rb"(?i)content-length:\s*(\d+)", head)
                    while length and len(body) < int(length.group(1)):
                        body += conn.recv(65536)
                    received.append(head.split(b" ", 1)[0].decode())
        threading.Thread(target=loop, daemon=True).start()
        self.addCleanup(srv.close)
        return f"http://127.0.0.1:{srv.getsockname()[1]}/v2/projects/secret-project/x", received

    def test_post_whose_answer_was_lost_is_sent_once(self):
        url, received = self.serve()
        ep = self.episode()
        with mock.patch.object(te.time, "sleep"):
            with self.assertRaises(te.RequestFailed) as caught:
                ep.api("POST", url, {"files": [{"uri": "gs://b/x.flac"}]}, timeout=5)
        self.assertEqual(received, ["POST"])
        self.assertNotIn("secret-project", str(caught.exception))

    def test_get_is_still_asked_again(self):
        url, received = self.serve()
        ep = self.episode()
        with mock.patch.object(te.time, "sleep"):
            with self.assertRaises(te.RequestFailed):
                ep.api("GET", url, attempts=3, timeout=5)
        self.assertEqual(received, ["GET"] * 3)

    def test_post_that_never_left_is_sent_again(self):
        ep = self.episode()
        outcomes = [urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), None]
        sent = []
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"name": "op"}'
        def urlopen(req, timeout):
            sent.append(req.get_method())
            item = outcomes.pop(0)
            if item is not None:
                raise item
            return Resp()
        with mock.patch.object(te.urllib.request, "urlopen", urlopen), mock.patch.object(te.time, "sleep"):
            self.assertEqual(ep.api("POST", "https://example.invalid/x", {}), {"name": "op"})
        self.assertEqual(sent, ["POST", "POST"])


class StopSignals(TempWorkMixin, unittest.TestCase):
    """SIGTERM, SIGHUP and SIGBREAK end the run: the best-effort handlers must not swallow them."""
    PIECES = [(100.0 + 41 * k, 145.0 + 41 * k, 102.0 + 41 * k, 143.0 + 41 * k) for k in range(6)]

    def test_real_sigterm_while_polling_pieces(self):
        if os.name == "nt":
            self.skipTest("no SIGTERM delivery to the own process on Windows")
        ep, clock = self.episode(), FakeClock()
        stub = PieceStub(ep, clock)
        polls = []
        def poll(name, **kw):
            polls.append(name)
            os.kill(os.getpid(), signal.SIGTERM)
            return None
        ep.poll = poll
        previous = signal.signal(signal.SIGTERM, te._stop)
        try:
            with mock.patch.object(te.time, "sleep", clock.sleep), mock.patch.object(te.time, "time", clock.time), \
                    mock.patch.object(te, "log"):
                with self.assertRaises(te.Stopped) as caught:
                    ep.fetch_pieces(self.PIECES)
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(len(polls), 1)
        self.assertEqual(clock.time() - FakeClock().time(), te.REC_POLL)     # no further round

    def test_stop_during_cut_upload_or_submit_sends_nothing_after_it(self):
        for step in ("cut", "upload", "submit"):
            ep, clock = self.episode(), FakeClock()
            stub = PieceStub(ep, clock)
            real = getattr(stub, step)
            def stopping(*a, real=real, step=step):
                if stub.calls[step] >= 1:
                    raise te.Stopped(143)
                return real(*a)
            setattr(ep, step, stopping)
            ep.poll = lambda name, **kw: None
            with mock.patch.object(te.time, "sleep", clock.sleep), mock.patch.object(te.time, "time", clock.time), \
                    mock.patch.object(te, "log"):
                with self.assertRaises(te.Stopped):
                    ep.fetch_pieces(self.PIECES)
            self.assertLessEqual(stub.calls["submit"], 1, step)
            if step != "submit":
                self.assertEqual(stub.calls["submit"], 0, step)

    def test_run_stops_cleans_up_and_writes_no_subtitle(self):
        ep = self.episode()
        video = os.path.join(self.tmp, "video.mp4")
        open(video, "w").close()
        te.atomic_write(f"{ep.raw}/part-000.json", "{}")
        ep.sh = lambda *a: subprocess.CompletedProcess(a, 0, "120.0", "")
        ep.prepare_audio = lambda total: ("full.flac", 120.0)
        ep.silences = lambda: ([], [])
        def span(s, e, tag):
            ep.spans.append({"tag": tag, "start": s, "used": True, "words": 2, "looped": 0, "dropped": 0})
            return [(0.5, 1.0, "a"), (e - s - 1.0, e - s - 0.5, "b")], [], []
        ep.transcribe_span = span
        def fetch(pieces):
            raise te.Stopped(143)
        ep.fetch_pieces = fetch
        calls = []
        ep.cleanup = lambda: calls.append("cleanup")
        ep.finish = lambda *a: calls.append("finish")
        with mock.patch.object(te, "_tool", lambda *a: "/usr/bin/true"), mock.patch.object(te, "log") as logged, \
                mock.patch.object(te, "recover_holes", lambda words, end, lang, f, *a: f([(1.0, 40.0, 1.0, 40.0)])):
            with self.assertRaises(te.Stopped):
                ep.run()
        self.assertEqual(calls, ["cleanup"])
        self.assertFalse(os.path.exists(ep.out_srt))
        self.assertNotIn("hole recovery failed", " ".join(str(c.args[0]) for c in logged.call_args_list))

    def test_main_exits_with_128_plus_the_signal(self):
        video = os.path.join(self.tmp, "video.mp4")
        open(video, "w").close()
        with mock.patch.object(te.Episode, "run", side_effect=te.Stopped(143)), mock.patch.object(te, "load_config"), \
                mock.patch.object(te.signal, "signal"):
            with self.assertRaises(SystemExit) as caught:
                te.main([video, os.path.join(self.tmp, "out.srt"), self.tmp])
        self.assertEqual(caught.exception.code, 143)
        self.assertNotIsInstance(caught.exception, te.Stopped)
        self.assertFalse(issubclass(te.Stopped, (Exception, SystemExit)))
        self.assertTrue(issubclass(te.RequestFailed, SystemExit))           # still absorbed where recovery can do without

    def test_missing_video_and_help(self):
        missing = os.path.join(self.tmp, "no such episode.mp4")
        with mock.patch.object(te.signal, "signal"):
            with self.assertRaises(SystemExit) as caught:
                te.main([missing, os.path.join(self.tmp, "out.srt"), os.path.join(self.tmp, "work")])
            self.assertEqual(str(caught.exception.code), f"video not found: {missing}")
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "work")))
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                te.main(["--help"])
        self.assertIn("usage:", out.getvalue())

    def test_interrupt_during_uploads_does_not_wait_for_the_queue(self):
        ep = self.episode()
        stub = PieceStub(ep)
        started, lock = [], threading.Lock()
        def upload(path, tag):
            with lock:
                started.append(tag)
                first = len(started) == 1
            if first:
                raise KeyboardInterrupt()
            time.sleep(0.05)
            return "uri-" + tag
        ep.upload = upload
        pieces = [(100.0 + 41 * k, 145.0 + 41 * k, 102.0 + 41 * k, 143.0 + 41 * k) for k in range(40)]
        with mock.patch.object(te, "log"):
            with self.assertRaises(KeyboardInterrupt):
                ep.fetch_pieces(pieces)
        self.assertLess(len(started), 12)
        self.assertEqual(stub.calls["submit"], 0)


class RepairedOpeningAtASeam(unittest.TestCase):
    EARLIER = response([(41.76, 41.96, "ben"), (41.96, 42.2, "sizin"), (42.2, 42.52, "tahlil"), (42.52, 43.08, "sonuçlarınızı"),
                        (43.08, 43.44, "aldım."), (44.64, 44.84, "Tabii")])
    # the later piece's opening comes back broken: no start, no end, no offsets at all
    LATER = response([(None, 0.96, "Ben"), (0.96, 1.2, "sizin"), (1.2, 1.48, "tahlil"), (1.48, None, "sonuçlarınızı"),
                      (None, None, "aldım."), (3.6, 3.88, "Tabii"), (3.88, 4.16, "size"), (4.16, 4.64, "ulaşmak"),
                      (4.64, 4.96, "biraz"), (4.96, 5.12, "zor"), (5.12, 5.48, "oldu."), (6.32, 6.64, "Telefon")])

    def test_repaired_opening_does_not_duplicate_the_earlier_tail(self):
        pieces = [(470.0, 515.0, 472.0, 513.0), (511.0, 556.0, 513.0, 554.0)]
        per = [te.piece_words(st, *p, "x", lang="tr-TR", all_words=True) for st, p in zip((self.EARLIER, self.LATER), pieces)]
        got = sorted(te.seam_merge(pieces, per, "tr-TR"), key=lambda x: x["s"])
        self.assertEqual([x["w"] for x in got], ["ben", "sizin", "tahlil", "sonuçlarınızı", "aldım.", "Tabii", "size", "ulaşmak",
                                                 "biraz", "zor", "oldu.", "Telefon"])
        verb = got[4]
        self.assertAlmostEqual(verb["s"], 513.08, places=2)
        self.assertFalse(verb["est"])

    def test_words_without_any_offset_count_as_estimated(self):
        words = te.piece_words(self.LATER, 511.0, 556.0, 511.0, 554.0, "x", lang="tr-TR")
        self.assertTrue(next(x for x in words if x["w"] == "aldım.")["est"])
        # the next word has no start either, so the filled offsets (0.0 to 0.4) look plausible to the old repair
        st = response([(None, None, "ان"), (None, 26.4, "هي"), (26.4, 26.9, "الحرب")], lang="ar-XA")
        first = te.piece_words(st, 2192.0, 2220.5, 2192.0, 2220.5, "y", lang="ar-XA")[0]
        self.assertTrue(first["est"])
        existing = [(2180.0, 2180.4, "قبل"), (2240.0, 2240.4, "بعد")]
        res = te.classify_gap(existing, [first], 2180.4, 2240.0, "ar-XA")
        self.assertEqual((res["new"], len(res["filtered"])), ([], 1))


class CompressedPieceOpening(unittest.TestCase):
    ST = response([(None, 0.48, "Selim"), (0.48, 0.68, "Bey."), (33.32, 33.84, "Şey"), (33.88, 33.96, "eee")])

    def test_opening_is_packed_before_the_words_after_the_jump(self):
        with mock.patch.object(te, "log"):
            got = te.piece_words(self.ST, 470.0, 515.0, 472.0, 513.0, "x", None, "tr-TR")
        self.assertEqual([x["w"] for x in got], ["Selim", "Bey.", "Şey", "eee"])
        self.assertGreaterEqual(got[0]["s"], 472.0)
        self.assertLessEqual(got[1]["e"], got[2]["s"])
        self.assertGreater(got[0]["s"], 500.0)

    def test_words_the_subtitle_or_the_previous_piece_has_there_stay_out(self):
        context = [(470.18, 470.48, "Selim"), (470.48, 470.68, "Bey.")]
        for kw in ({"context": context}, {"prior": context}):
            got = te.piece_words(self.ST, 470.0, 515.0, 472.0, 513.0, "x", lang="tr-TR", **kw)
            self.assertEqual([x["w"] for x in got], ["Şey", "eee"], kw)

    def test_opening_that_repeats_the_words_after_the_jump_stays_out(self):
        st = response([(None, 0.1, "ثم"), (0.1, 0.3, "لعمري"), (0.3, 0.5, "نريد"), (0.5, 0.7, "الملك"),
                       (25.5, 25.8, "ثم"), (25.8, 26.2, "لعمري"), (26.4, 26.7, "نريد"), (26.7, 26.9, "الملك")], lang="ar-XA")
        got = te.piece_words(st, 2328.0, 2362.0, 2330.0, 2362.0, "x", None, "ar-XA")
        self.assertEqual([x["w"] for x in got], ["ثم", "لعمري", "نريد", "الملك"])

    def test_first_piece_of_a_region_is_unchanged(self):
        got = te.piece_words(self.ST, 470.0, 515.0, 470.0, 513.0, "x", None, "tr-TR")
        self.assertEqual([x["w"] for x in got], ["Selim", "Bey.", "Şey", "eee"])
        self.assertLess(got[1]["e"], 471.0)


class SentenceCutByASwallowedPause(unittest.TestCase):
    def test_early_half_sentence_is_reported_not_inserted_again(self):
        rng = random.Random(41)
        t = 819.08
        before = arabic_dialogue(rng, 0.5, 818.0, "قبل")
        phrase = [(t, t + 0.42, "اعتقد"), (t + 0.42, t + 0.54, "ان"), (t + 0.54, t + 0.66, "لا"),
                  (t + 0.66, t + 1.02, "حجه"), (t + 1.02, t + 1.32, "لكم"), (t + 1.32, t + 1.6, "بعد")]
        today = [(t + 165.22, t + 165.72, "اليوم")]
        after = arabic_dialogue(rng, 986.0, 1200.0, "تال")
        new = arabic_dialogue(rng, 880.0, 890.0, "جديد")
        heard = [(w0 + 163.92, w1 + 163.92 + (0.44 if k >= 3 else 0.0), w) for k, (w0, w1, w) in enumerate(phrase)]
        heard = [(s + (0.44 if k >= 3 else 0.0), e, w) for k, (s, e, w) in enumerate(heard)]
        heard.append((heard[-1][1], heard[-1][1] + 0.26, "اليوم"))
        truth = before + new + heard + after
        def fetch(pieces):
            return {te.piece_tag(s, e): piece_response(truth, s, e) for s, e, _, _ in pieces}
        with mock.patch.object(te, "log"):
            out, stats = te.recover_holes(before + phrase + today + after, 1200.0, "ar-XA", fetch)
        self.assertEqual(stats["inserted"], len(new))
        self.assertEqual([w[2] for w in out].count("اعتقد"), 1)
        self.assertEqual(len(stats["misplaced"]), 1)
        self.assertAlmostEqual(stats["misplaced"][0]["run_start"], t, places=1)
        self.assertGreater(stats["misplaced"][0]["offset_s"], 100)


class RepairEventsAndStarts(unittest.TestCase):
    def test_broken_end_only_is_not_a_long_move(self):
        ws = [[10.0, 10.3, "a"], [10.4, 10.7, "b"], [11.0, 300.0, "G"], [11.3, 11.6, "c"], [11.7, 12.0, "d"],
              [12.1, 12.4, "e"], [12.5, 12.8, "f"], [12.9, 13.2, "g"]]
        kept, events = te.repair(ws, 400.0)
        outliers = [ev for ev in events if ev["kind"] == "outlier"]
        self.assertTrue(outliers)
        self.assertTrue(all(abs(ev["shift_s"]) < te.MOVE_WARN_S for ev in outliers), outliers)
        ws[2] = [250.0, 300.0, "G"]                  # both offsets far away: still a long move
        kept, events = te.repair(ws, 400.0)
        self.assertTrue(any(abs(ev["shift_s"]) > te.MOVE_WARN_S for ev in events), events)

    def test_long_first_word_with_a_real_start_keeps_it(self):
        kept, _ = te.repair([[10.2, 12.6, "Anneeee"], [12.8, 13.2, "gel"], [13.3, 15.6, "buraya"]], 100.0)
        self.assertEqual(kept[0], (10.2, 12.6, "Anneeee"))


class NotesRound2(TempWorkMixin, unittest.TestCase):
    def test_slow_chunk_is_listed_in_its_own_section(self):
        words = [(round(t, 2), round(t + 0.4, 2), "k") for t in te_frange(0.0, 600.0, 0.6)]
        words += [(float(t), t + 0.4, "k") for t in range(600, 900, 10)]
        words += [(round(t, 2), round(t + 0.4, 2), "k") for t in te_frange(900.0, 1199.0, 0.6)]
        cues = [[w[0], w[1], w[2]] for w in words]
        rec = dict(te.empty_recovery(), status="ran")
        verify = te.run_checks(cues, words, 1200.0, 1200.0, [], [], rec, [0.0, 300.0, 600.0, 900.0, 1200.0])
        self.assertEqual(verify["status"], "pass")
        text = te.render_notes(cues, words, 20.0, verify, rec, [], {})
        self.assertIn("## Slow chunks", text)
        self.assertIn("00:10:00 to 00:15:00", text)
        self.assertIn("| Slow chunks (listed above) | 1 |", text)
        self.assertNotIn("## Please check", text)

    def test_laughter_in_silence_or_moved_is_not_a_warning(self):
        laughter = [(10.0, 10.2, "ههه"), (10.3, 10.5, "ههه"), (10.6, 10.8, "ه")]
        events = [{"start": 10.6, "end": 10.8, "kind": "outlier", "words": 1, "shift_s": 164.0}]
        verify = te.run_checks([], laughter, 60.0, 60.0, [(9.0, 12.0)], events, dict(te.empty_recovery(), status="ran"), [0.0, 60.0])
        self.assertEqual(verify["warns"], [])
        speech = [(10.0, 10.2, "bir"), (10.3, 10.5, "iki"), (10.6, 10.8, "üç")]
        verify = te.run_checks([], speech, 60.0, 60.0, [(9.0, 12.0)], events, dict(te.empty_recovery(), status="ran"), [0.0, 60.0])
        self.assertEqual(sorted(f["check"] for f in verify["warns"]), ["timing_moved", "words_in_silence"])

    def test_findings_milliseconds_apart_are_one_item_and_wording(self):
        cues = [[2748.0, 2749.5, "ه ههه"]]
        words = [(2748.0, 2748.4, "ه"), (2749.0, 2749.4, "ههه")]
        warns = [te.finding("timing_moved", 2748.0, 2749.0, "1 word was moved 9 s later by the timing repair, check the timing", words=1),
                 te.finding("timing_moved", 2749.005, 2749.4, "1 word was moved 9 s later by the timing repair, check the timing", words=1)]
        verify = {"status": "warn", "fails": [], "warns": warns, "notes": []}
        rec = dict(te.empty_recovery(), status="ran", inserted=0)
        text = te.render_notes(cues, words, 46.0, verify, rec, [], {})
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("- ")]), 1)
        self.assertNotIn("passed", text)
        self.assertIn("in 0 stretches", text)

    def test_unchecked_stretches_add_up_when_recovery_did_not_run(self):
        words = [(0.0, 0.5, "a"), (100.0, 100.5, "b")]
        rec = dict(te.empty_recovery(), status="disabled")
        verify = te.run_checks([[0.0, 0.5, "a"], [100.0, 100.5, "b"]], words, 100.5, 100.5, [], [], rec, [0.0, 100.5])
        text = te.render_notes([[0.0, 0.5, "a"], [100.0, 100.5, "b"]], words, 2.0, verify, rec, [], {})
        self.assertIn("| Stretches not checked for missing speech (listed above) | 1 |", text)


def te_frange(start, end, step):
    t = start
    while t < end:
        yield t
        t += step


class Stragglers(TempWorkMixin, unittest.TestCase):
    """One piece Google never finishes must not hold the run for the whole stage, and the audio no
    waiting operation reads must not stay in the bucket."""
    STRAGGLER = (2627.0, 2672.0, 2629.0, 2670.0)

    def run_pieces(self, extra_record=None):
        ep, clock = self.episode(), FakeClock()
        ep.deploy = lambda: ("p", "b", "gcloud")
        ep.cut = lambda s, e, p: (open(p, "w").close(), p)[1]
        ep.sh = lambda *a: subprocess.CompletedProcess(a, 0, "", "")        # the real upload(), gcloud answering
        sent = {}
        def api(method, url, body=None, **kw):
            if method == "POST":
                name = f"op{len(sent) + 1}"
                sent[name] = (clock.time(), body["files"][0]["uri"])
                return {"name": name}
            t0, uri = sent[url.rsplit("/", 1)[1]]
            if "2627000" in uri or clock.time() - t0 < 60:
                return {}
            return response([(1.0, 1.4, "bir")], billed="45s")
        ep.api = api
        removed = []
        ep.run_proc = lambda args: removed.append(list(args)) or subprocess.CompletedProcess(args, 0, "", "")
        pieces = [(100.0 + 50 * k, 145.0 + 50 * k, 102.0 + 50 * k, 143.0 + 50 * k) for k in range(40)] + [self.STRAGGLER]
        with mock.patch.object(te.time, "sleep", clock.sleep), mock.patch.object(te.time, "time", clock.time), \
                mock.patch.object(te, "log"):
            start = clock.time()
            got = ep.fetch_pieces(pieces)
            waited = clock.time() - start
            if extra_record:
                te.atomic_write(f"{ep.raw}/rec/rec-1-2.op.json", json.dumps(dict(extra_record, submitted=clock.time(), prefix=ep.prefix)))
            ep.cleanup()
        return ep, got, waited, removed

    def test_last_piece_waits_a_few_times_the_usual_answer_time(self):
        ep, got, waited, removed = self.run_pieces()
        tag = te.piece_tag(*self.STRAGGLER[:2])
        self.assertEqual(got[tag], "pending")
        self.assertEqual(sum(1 for v in got.values() if isinstance(v, dict)), 40)
        self.assertLess(waited, 420)
        rm = [a for a in removed if a[1:3] == ["storage", "rm"]]
        self.assertFalse(any("--recursive" in a for a in rm))
        gone = {u for a in rm for u in a if u.startswith("gs://")}
        self.assertEqual(len(ep.uploaded_uris), 41)
        self.assertEqual(gone, ep.uploaded_uris - {f"gs://b/{ep.prefix}{tag}.flac"})
        self.assertEqual(te.read_json(f"{self.tmp}/uploads.json"), [ep.prefix])
        record = te.read_json(f"{ep.raw}/rec/{tag}.op.json")
        self.assertEqual(record["uri"], f"gs://b/{ep.prefix}{tag}.flac")

    def test_a_record_without_uri_keeps_everything(self):
        ep, got, waited, removed = self.run_pieces(extra_record={"name": "op-x", "start_ms": 1, "end_ms": 2})
        self.assertEqual([a for a in removed if a[1:3] == ["storage", "rm"]], [])
        self.assertEqual(te.read_json(f"{self.tmp}/uploads.json"), [ep.prefix])


class LeftoverRecords(TempWorkMixin, unittest.TestCase):
    def test_record_next_to_its_saved_response_or_too_old_holds_nothing(self):
        ep = self.episode()
        saved = response([(0.0, 0.5, "bir")], billed="45s")
        saved["_span"] = {"start_ms": 0, "end_ms": 45000, "language": "tr-TR", "model": "chirp_3", "video_bytes": 1234}
        te.atomic_write(f"{ep.raw}/part-000.json", json.dumps(saved))
        for tag, age in (("part-000", 0.0), ("part-001", 25 * 3600.0)):
            te.atomic_write(f"{ep.raw}/{tag}.op.json", json.dumps({"name": "op", "start_ms": 0, "end_ms": 45000,
                                                                   "submitted": time.time() - age, "prefix": "stt/1-1/"}))
        self.assertEqual(ep.prefixes_in_use(), set())
        self.assertFalse(os.path.exists(f"{ep.raw}/part-000.op.json"))
        self.assertFalse(os.path.exists(f"{ep.raw}/part-001.op.json"))

    def test_work_directory_with_brackets_still_finds_waiting_records(self):
        work = os.path.join(self.tmp, "Show [1080p]")
        ep = te.Episode(os.path.join(work, "v.mp4"), os.path.join(work, "o.srt"), work)
        os.makedirs(f"{ep.raw}/rec")
        te.atomic_write(f"{ep.raw}/rec/rec-1-2.op.json", json.dumps({"name": "op", "start_ms": 1, "end_ms": 2,
                                                                   "submitted": time.time(), "prefix": "stt/1-1/"}))
        self.assertEqual(ep.prefixes_in_use(), {"stt/1-1/"})

    def test_token_failure_stops_recovery_before_any_upload(self):
        ep = self.episode()
        stub = PieceStub(ep)
        def token(refresh=False):
            raise te.RequestFailed("could not get an access token: command failed (1)")
        ep.token = token
        with mock.patch.object(te, "log"):
            with self.assertRaises(te.RequestFailed):
                ep.fetch_pieces([(100.0, 145.0, 102.0, 143.0)])
        self.assertEqual((stub.calls["cut"], stub.calls["upload"]), (0, 0))

    def test_redact_user_email_and_project_number(self):
        text = te.redact("(gcloud.storage.cp) HTTPError 403: editor@acme-subtitling.com does not have storage.objects.create access; "
                         "Your current active account [editor@acme-subtitling.com] does not have any valid credentials; "
                         "Quota exceeded for quota metric of service 'speech.googleapis.com' for consumer 'project_number:123456789012'.")
        for secret in ("acme-subtitling", "editor@", "123456789012"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
