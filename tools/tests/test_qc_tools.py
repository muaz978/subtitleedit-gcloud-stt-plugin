"""Offline tests for the helper scripts in tools/qc. Synthetic data only: no network, no media.
Run from the repository root with:

    python3 -B -m unittest discover tools/tests
"""
import json, os, re, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
QC = os.path.abspath(os.path.join(HERE, os.pardir, "qc"))
SCRIPTS = ["cues", "compare", "consensus", "displace", "fview", "make_spans", "make_evidence", "patch", "recheck", "retime"]

# invented vocabulary, nothing from a real recording
W = ("river stone lamp window garden candle morning bridge harbor market silver orchard meadow pillow "
     "ladder basket feather mirror anchor button copper desert engine forest glacier hammer island jacket "
     "kettle lantern marble needle ocean pepper quartz rocket saddle timber umbrella velvet walnut yellow zipper").split()


def stamp(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def srt_text(cues):
    return "".join("%d\n%s --> %s\n%s\n\n" % (i, stamp(s), stamp(e), text) for i, (s, e, text) in enumerate(cues, 1))


def run(script, *args, expect=0, env=None):
    p = subprocess.run([sys.executable, "-B", os.path.join(QC, script)] + [str(a) for a in args],
                       capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, **env) if env else None)
    assert p.returncode == expect, "%s exited %s, wanted %s:\n%s\n%s" % (script, p.returncode, expect, p.stdout, p.stderr)
    return p


def steady_cues(n=10, words=3, step=3.0):
    """n cues of `words` words each, one cue every `step` seconds, all different words."""
    cues = []
    for i in range(n):
        text = " ".join(W[(i * words + k) % len(W)] for k in range(words))
        cues.append((i * step, i * step + step - 0.3, text))
    return cues


def fresh_from(cues, swap=None):
    """A fresh read of the same cues: [[start, end, word], ...]; swap = {word: replacement}."""
    out = []
    for s, e, text in cues:
        ws = text.split()
        for k, w in enumerate(ws):
            a = s + (e - s) * k / len(ws)
            out.append([round(a, 2), round(a + (e - s) / len(ws) * 0.8, 2), (swap or {}).get(w, w)])
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def write(self, name, text):
        with open(self.path(name), "w", encoding="utf-8") as f:
            f.write(text)
        return self.path(name)

    def write_json(self, name, obj):
        with open(self.path(name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        return self.path(name)

    def read(self, name):
        with open(self.path(name), encoding="utf-8") as f:
            return f.read()


class SourceTests(unittest.TestCase):
    def test_every_script_compiles(self):
        for name in SCRIPTS:
            path = os.path.join(QC, name + ".py")
            with open(path, encoding="utf-8") as f:
                compile(f.read(), path, "exec")

    def test_no_machine_specific_text(self):
        for name in [n + ".py" for n in SCRIPTS] + ["README.md", "review_rules.md"]:
            with open(os.path.join(QC, name), encoding="utf-8") as f:
                text = f.read()
            for bad in ("/Users/", "C:\\Users", "Downloads"):
                self.assertNotIn(bad, text, "%s contains %r" % (name, bad))
            self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "%s contains an email address" % name)

    def test_no_dashes_in_prose(self):
        for name in ("README.md", "review_rules.md"):
            with open(os.path.join(QC, name), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("\u2013", text)
            self.assertNotIn("\u2014", text)


class CuesTests(Base):
    def test_scan_reports_structure_short_cues_and_repeats(self):
        gram = "river stone lamp window garden candle"
        cues = [(0.0, 3.0, gram), (3.5, 3.2, "inverted cue here"), (4.0, 7.0, "plain words between"),
                (6.5, 9.0, "overlapping cue starts early"), (9.2, 9.35, "tiny"), (40.0, 44.0, gram)]
        p = run("cues.py", self.write("a.srt", srt_text(cues)), "scan")
        self.assertIn("INVERTED 2", p.stdout)
        self.assertIn("OVERLAP 3 4", p.stdout)
        self.assertIn("cues under 0.30 s", p.stdout)
        self.assertIn("river stone lamp window garden candle", p.stdout)   # the repeated six-gram, 40 s apart

    def test_show_and_at(self):
        f = self.write("a.srt", srt_text(steady_cues(6)))
        shown = run("cues.py", f, "show", 2, 3).stdout.strip().splitlines()
        self.assertEqual(len(shown), 2)
        self.assertTrue(shown[0].startswith("2 "))
        at = run("cues.py", f, "at", 9.5, 9.6).stdout.strip().splitlines()
        self.assertEqual(len(at), 1)
        self.assertTrue(at[0].startswith("4 "))


class PatchTests(Base):
    def setUp(self):
        super().setUp()
        self.cues = [(0.0, 2.0, "river stone"), (3.0, 5.0, "lamp window"), (6.0, 8.0, "garden candle"), (12.0, 14.0, "morning bridge")]
        self.raw = self.write("raw.srt", srt_text(self.cues))

    def patch(self, acc, expect=0):
        return run("patch.py", self.raw, self.write_json("acc.json", acc), self.path("out.srt"), self.path("log.tsv"),
                   self.path("map.json"), expect=expect)

    def test_empty_patch_reproduces_the_input(self):
        self.patch({"edits": [], "inserts": []})
        self.assertEqual(self.read("out.srt"), srt_text(self.cues))
        self.assertEqual(json.loads(self.read("map.json")), {"1": 1, "2": 2, "3": 3, "4": 4})

    def test_wrong_old_text_stops_everything(self):
        p = self.patch({"edits": [{"cue": 2, "old": "not the text", "new": "x"}], "inserts": []}, expect=1)
        self.assertIn("differs from the reviewed old text", p.stderr)
        self.assertFalse(os.path.exists(self.path("out.srt")))

    def test_delete_insert_edit_and_cue_map(self):
        p = self.patch({"edits": [{"cue": 2, "old": "lamp window", "new": ""},
                                  {"cue": 3, "old": "garden candle", "new": "garden candle again"}],
                        "inserts": [{"after_cue": 3, "start": 9.0, "end": 10.5, "text": "silver orchard"}]})
        out = self.read("out.srt")
        self.assertNotIn("lamp window", out)
        self.assertIn("garden candle again", out)
        self.assertIn("silver orchard", out)
        self.assertIn("written 4 cues (was 4): 1 edited, 1 new, 1 deleted", p.stdout)
        self.assertEqual(json.loads(self.read("map.json")), {"1": 1, "3": 2, "4": 4})
        log = self.read("log.tsv")
        self.assertIn("deleted", log)
        self.assertIn("text", log)

    def test_overlap_is_refused(self):
        p = self.patch({"edits": [{"cue": 1, "old": "river stone", "new": "river stone", "new_start": 2.5, "new_end": 3.5}],
                        "inserts": []}, expect=1)
        self.assertIn("STOP", p.stdout)
        self.assertFalse(os.path.exists(self.path("out.srt")))


class RetimeTests(Base):
    def test_squeezed_block_is_spread_over_its_real_times(self):
        cues = [(5.0, 6.0, "first line here"),
                (10.0, 10.2, "river stone lamp window"),
                (10.2, 10.4, "garden candle morning bridge"),
                (60.0, 61.0, "last line there")]
        real = [[40.0, 40.4, "river"], [40.5, 40.9, "stone"], [41.0, 41.4, "lamp"], [41.5, 41.9, "window"],
                [43.0, 43.4, "garden"], [43.5, 43.9, "candle"], [44.0, 44.4, "morning"], [44.5, 44.9, "bridge"]]
        srt = self.write("a.srt", srt_text(cues))
        fresh = self.write_json("fresh.json", {"0-100": real})
        run("retime.py", srt, 2, 3, 35, 50, self.path("retime.json"), fresh)
        edits = json.loads(self.read("retime.json"))
        self.assertEqual([e["cue"] for e in edits], [2, 3])
        self.assertAlmostEqual(edits[0]["new_start"], 40.0, delta=0.05)
        self.assertAlmostEqual(edits[0]["new_end"], 42.15, delta=0.05)
        self.assertAlmostEqual(edits[1]["new_start"], 43.0, delta=0.05)
        self.assertGreater(edits[1]["new_start"], edits[0]["new_end"])
        self.assertEqual(edits[0]["new"], edits[0]["old"])      # a retime-only edit keeps the text


class SpansTests(Base):
    def cuts(self, spans):
        return {round(x, 1) for s in spans for x in s}

    def test_a_and_b_cover_the_episode_and_keep_apart(self):
        cues = [(i * 21.0, i * 21.0 + 20.0, "word %d" % i) for i in range(20)]      # 1 s gaps, 420 s
        srt = self.write("a.srt", srt_text(cues))
        run("make_spans.py", srt, 420, "A", self.path("a.json"))
        run("make_spans.py", srt, 420, "B", self.path("b.json"), self.path("a.json"))
        a, b = json.loads(self.read("a.json")), json.loads(self.read("b.json"))
        for spans in (a, b):
            self.assertEqual(spans[0][0], 0.0)
            self.assertEqual(spans[-1][1], 420.0)
            for x, y in zip(spans, spans[1:]):
                self.assertEqual(x[1], y[0])            # contiguous, nothing skipped
        inner_a = {x for s in a for x in s} - {0.0, 420.0}
        for cut in {x for s in b for x in s} - {0.0, 420.0}:
            self.assertGreaterEqual(min(abs(cut - x) for x in inner_a), 30.0)


class CompareTests(Base):
    def test_agreement_and_differences(self):
        cues = steady_cues(10)
        srt = self.write("a.srt", srt_text(cues))
        same = self.write_json("same.json", {"0-30": fresh_from(cues)})
        run("compare.py", srt, same, self.path("out.json"))
        res = json.loads(self.read("out.json"))
        self.assertEqual(res[0]["agree"], 1.0)
        self.assertEqual(res[0]["ops"], [])
        swapped = self.write_json("swap.json", {"0-30": fresh_from(cues, swap={"basket": "casket"})})
        run("compare.py", srt, swapped, self.path("out2.json"))
        ops = json.loads(self.read("out2.json"))[0]["ops"]
        self.assertEqual([o["tag"] for o in ops], ["swap"])
        self.assertEqual((ops[0]["srt"], ops[0]["fresh"]), ("basket", "casket"))

    def test_displaced_cue_is_reported(self):
        cues = [(10.0, 12.0, "river stone lamp window garden"), (80.0, 82.0, "candle morning bridge harbor market")]
        srt = self.write("a.srt", srt_text(cues))
        moved = [[50.0, 50.3, "river"], [50.4, 50.7, "stone"], [50.8, 51.1, "lamp"], [51.2, 51.5, "window"], [51.6, 51.9, "garden"],
                 [80.0, 80.3, "candle"], [80.4, 80.7, "morning"], [80.8, 81.1, "bridge"], [81.2, 81.5, "harbor"], [81.6, 81.9, "market"]]
        fresh = self.write_json("f.json", {"0-100": moved})
        p = run("displace.py", srt, fresh)
        self.assertIn("more than 2.5 s from the fresh read: 1", p.stdout)
        self.assertIn("river stone lamp window garden", p.stdout)


class VoteTests(Base):
    def setUp(self):
        super().setUp()
        self.cues = steady_cues(10)
        self.srt = self.write("a.srt", srt_text(self.cues))
        # the subtitle says "basket", both reads hear "casket"
        self.a = self.write_json("a.json", {"0-100": fresh_from(self.cues, swap={"basket": "casket"})})
        self.b = self.write_json("b.json", {"0-100": fresh_from(self.cues, swap={"basket": "casket"})})

    def test_two_agreeing_reads_make_a_proposal(self):
        run("consensus.py", self.srt, self.a, self.b, self.path("c.json"))
        prop = json.loads(self.read("c.json"))["proposals"]
        self.assertEqual(len(prop), 1)
        self.assertEqual((prop[0]["kind"], prop[0]["srt"], prop[0]["a"], prop[0]["b"]), ("replace", "basket", "casket", "casket"))

    def test_one_dissenting_read_makes_none(self):
        clean = self.write_json("clean.json", {"0-100": fresh_from(self.cues)})
        p = run("consensus.py", self.srt, self.a, clean, self.path("c.json"))
        self.assertEqual(json.loads(self.read("c.json"))["proposals"], [])
        self.assertIn("only A disagrees 1", p.stdout)

    def test_evidence_files_are_written(self):
        run("consensus.py", self.srt, self.a, self.b, self.path("c.json"))
        run("make_evidence.py", self.srt, self.a, self.b, self.path("c.json"), self.path("evidence"))
        index = json.loads(self.read(os.path.join("evidence", "index.json")))
        self.assertEqual(len(index["batches"]), 1)
        batch = self.read(os.path.join("evidence", "batch_01.md"))
        self.assertIn("Proposals", batch)
        self.assertIn("basket", batch)
        self.assertIn("casket", batch)


class FviewTests(Base):
    def test_shows_reads_and_cues(self):
        cues = steady_cues(6)
        srt = self.write("a.srt", srt_text(cues))
        fresh = self.write_json("f.json", {"0-60": fresh_from(cues)})
        out = run("fview.py", fresh, srt, 3, 9).stdout
        self.assertIn("--- fresh piece", out)
        self.assertIn("=== SRT cues in range", out)


FAKE_TOOL = """
import os
def load_config(): pass
def _tool(env, default): return default
def piece_tag(s, e): return "%g-%g" % (s, e)
def words_from_raw(r): return r["words"]
class Episode:
    def __init__(self, video, out, work): pass
    def fetch_pieces(self, pieces):
        got = {}
        for s, e, _, _ in pieces:
            if os.environ.get("FAKE_OK") or s == 0:
                got[piece_tag(s, e)] = {"words": [[0.5, 0.9, "alpha"], [1.0, None, "beta"]]}
            else:
                got[piece_tag(s, e)] = "pending" if s == 100 else None
        return got
    def cleanup(self): pass
"""

_TR = "\u00e7i\u00e7ek a\u011fa\u00e7 \u015feker g\u00fcl \u0131\u015f\u0131k \u00f6rg\u00fc b\u00fcy\u00fck ta\u015f \u00e7ay kap\u0131 yol".split()
TR = _TR + [w + "ler" for w in _TR] + [w + "in" for w in _TR]      # 33 different invented-looking words


class RobustnessTests(Base):
    def setUp(self):
        super().setUp()
        self.cues = [(0.0, 2.0, "river stone"), (3.0, 5.0, "lamp window"), (70.0, 72.0, "garden candle")]
        self.raw = self.write("raw.srt", srt_text(self.cues))

    def patch(self, acc, expect=0, env=None):
        return run("patch.py", self.raw, self.write_json("acc.json", acc), self.path("out.srt"), self.path("log.tsv"),
                   self.path("map.json"), expect=expect, env=env)

    def test_subtitle_with_a_byte_order_mark_is_read(self):
        f = self.write("bom.srt", "\ufeff" + srt_text(steady_cues(4)))
        self.assertIn("cues 4", run("cues.py", f, "scan").stdout)

    def test_a_time_just_under_a_minute_never_prints_as_sixty_seconds(self):
        self.patch({"edits": [{"cue": 2, "old": "lamp window", "new": "lamp window", "new_start": 59.9996, "new_end": 62.0}]})
        out = self.read("out.srt")
        self.assertIn("00:01:00,000 --> 00:01:02,000", out)
        self.assertNotIn(":60,", out)

    def test_patch_checks_survive_python_dash_O(self):
        p = self.patch({"edits": [{"cue": 2, "old": "not the text", "new": "x"}], "inserts": []}, expect=1, env={"PYTHONOPTIMIZE": "1"})
        self.assertIn("differs from the reviewed old text", p.stderr)
        self.assertFalse(os.path.exists(self.path("out.srt")))

    def test_patch_input_shapes(self):
        keep = {"cue": 1, "old": "river stone", "new": "river stone"}
        self.patch({"edits": [keep]})                                    # no "inserts" key
        self.patch([keep])                                               # a bare list, as retime.py writes it
        p = self.patch({"edits": [dict(keep, new_start=1.0)]}, expect=1)  # half a time
        self.assertIn("only one of new_start and new_end", p.stderr)
        p = self.patch({"edits": [keep, keep]}, expect=1)
        self.assertIn("two edits for cue 1", p.stderr)

    def test_change_log_has_reason_and_end_times(self):
        self.patch({"edits": [{"cue": 2, "old": "lamp window", "new": "lamp windows", "why": "plural", "new_start": 3.5, "new_end": 5.5}]})
        rows = [r.split("\t") for r in self.read("log.tsv").splitlines()]
        self.assertEqual(rows[0][-3:], ["old_end", "new_end", "why"])
        self.assertEqual((rows[1][3], rows[1][4], rows[1][-1]), ("lamp window", "lamp windows", "plural"))
        self.assertEqual(rows[1][-2], "00:00:05.500")

    def test_cue_map_defaults_beside_the_output(self):
        run("patch.py", self.raw, self.write_json("acc.json", {"edits": []}), self.path("out.srt"), self.path("log.tsv"))
        self.assertTrue(os.path.exists(self.path("out.cue_map.json")))

    def test_every_script_prints_usage_without_arguments(self):
        for name in SCRIPTS:
            p = run(name + ".py", expect=1)
            self.assertIn("usage", p.stderr.lower(), name)

    def test_make_spans_mode_b_needs_the_a_file(self):
        srt = self.write("a.srt", srt_text(steady_cues(4)))
        p = run("make_spans.py", srt, 100, "B", self.path("b.json"), expect=1)
        self.assertIn("fifth argument", p.stderr)

    def test_recheck_guards_run_before_anything_is_paid_for(self):
        spans = self.write_json("spans.json", [[0, 50]])
        work = os.path.join(self.dir.name, "work")
        os.mkdir(work)
        p = run("recheck.py", work, spans, self.path("out.json"), expect=1, env={"SE_STT_TOOL": self.path("nope.py")})
        self.assertIn("cannot find the episode tool", p.stderr)
        tool = self.write("fake_tool.py", FAKE_TOOL)
        p = run("recheck.py", work, spans, self.path("out.json"), expect=1, env={"SE_STT_TOOL": tool})
        self.assertIn("has no full.flac", p.stderr)

    def test_recheck_saves_what_it_has_and_a_rerun_completes_it(self):
        tool = self.write("fake_tool.py", FAKE_TOOL)
        work = os.path.join(self.dir.name, "work")
        os.mkdir(work)
        self.write(os.path.join("work", "full.flac"), "")
        spans = self.write_json("spans.json", [[0, 50], [100, 150], [200, 250]])
        p = run("recheck.py", work, spans, self.path("out.json"), expect=2, env={"SE_STT_TOOL": tool})
        self.assertIn("MISSING 2 piece(s)", p.stdout)
        self.assertEqual(list(json.loads(self.read("out.json"))), ["0-50"])
        self.assertEqual(json.loads(self.read("out.json.missing.json")), [[100.0, 150.0], [200.0, 250.0]])
        run("recheck.py", work, self.path("out.json.missing.json"), self.path("out.json"), env={"SE_STT_TOOL": tool, "FAKE_OK": "1"})
        out = json.loads(self.read("out.json"))
        self.assertEqual(sorted(out), ["0-50", "100-150", "200-250"])
        self.assertEqual(out["100-150"][0], [100.5, 100.9, "alpha"])
        self.assertEqual(out["100-150"][1], [101.0, None, "beta"])         # a missing end stays missing
        self.assertFalse(os.path.exists(self.path("out.json.missing.json")))   # nothing stale is left behind

    def test_turkish_text_survives_a_console_that_is_not_utf8(self):
        env = {"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
        cues = [(i * 3.0, i * 3.0 + 2.7, " ".join(TR[(i * 3 + k) % len(TR)] for k in range(3))) for i in range(10)]
        srt = self.write("tr.srt", srt_text(cues))
        swap = {TR[4]: TR[5]}
        a = self.write_json("a.json", {"0-100": fresh_from(cues, swap=swap)})
        b = self.write_json("b.json", {"0-100": fresh_from(cues, swap=swap)})
        self.assertIn(TR[0], run("cues.py", srt, "show", 1, 2, env=env).stdout)
        self.assertIn(TR[0], run("fview.py", a, srt, 0, 10, env=env).stdout)
        run("compare.py", srt, a, self.path("c1.json"), env=env)
        run("displace.py", srt, a, env=env)
        run("consensus.py", srt, a, b, self.path("c.json"), env=env)
        self.assertEqual(len(json.loads(self.read("c.json"))["proposals"]), 1)
        run("make_evidence.py", srt, a, b, self.path("c.json"), self.path("evidence"), env=env)
        self.assertIn(TR[4], self.read(os.path.join("evidence", "batch_01.md")))


class VoteDetailTests(Base):
    def test_reads_that_disagree_with_each_other_are_listed_as_split(self):
        cues = steady_cues(10)
        srt = self.write("a.srt", srt_text(cues))
        a = self.write_json("a.json", {"0-100": fresh_from(cues, swap={"basket": "casket"})})
        b = self.write_json("b.json", {"0-100": fresh_from(cues, swap={"basket": "gasketx"})})
        p = run("consensus.py", srt, a, b, self.path("c.json"))
        res = json.loads(self.read("c.json"))
        self.assertEqual(res["proposals"], [])
        self.assertEqual([(x["srt"], x["a"], x["b"]) for x in res["split"]], [("basket", "casket", "gasketx")])
        self.assertIn("reads split", p.stdout)

    def test_an_insert_says_where_it_goes_and_when_the_reads_heard_it(self):
        cues = [(i * 5.0, i * 5.0 + 2.7, " ".join(W[(i * 3 + k) % len(W)] for k in range(3))) for i in range(8)]
        srt = self.write("a.srt", srt_text(cues))
        heard = sorted(fresh_from(cues) + [[17.9, 18.2, "violet"], [18.4, 18.8, "tunnel"]])    # between cues 4 and 5
        a, b = self.write_json("a.json", {"0-100": heard}), self.write_json("b.json", {"0-100": heard})
        run("consensus.py", srt, a, b, self.path("c.json"))
        prop = json.loads(self.read("c.json"))["proposals"]
        self.assertEqual([(x["kind"], x["after_cue"], x["a"]) for x in prop], [("insert", 4, "violet tunnel")])
        run("make_evidence.py", srt, a, b, self.path("c.json"), self.path("evidence"))
        batch = self.read(os.path.join("evidence", "batch_01.md"))
        self.assertIn("(insert after cue 4)", batch)
        self.assertIn("(00:00:17.90 to 00:00:18.80)", batch)


class RoundThreeTests(Base):
    def test_an_insert_before_the_first_cue_can_be_applied(self):
        cues = [(10.0, 12.0, "river stone"), (14.0, 16.0, "lamp window")]
        raw = self.write("raw.srt", srt_text(cues))
        acc = self.write_json("acc.json", {"inserts": [{"after_cue": 0, "start": 1.0, "end": 3.0, "text": "garden candle"}]})
        run("patch.py", raw, acc, self.path("out.srt"), self.path("log.tsv"))
        self.assertTrue(self.read("out.srt").startswith("1\n00:00:01,000 --> 00:00:03,000\ngarden candle\n"))

    def test_the_vote_proposes_an_insert_at_the_very_start(self):
        full = [(i * 5.0, i * 5.0 + 2.7, " ".join(W[(i * 3 + k) % len(W)] for k in range(3))) for i in range(8)]
        srt = self.write("a.srt", srt_text(full[2:]))                      # the subtitle lacks the first two cues
        heard = fresh_from(full)
        a, b = self.write_json("a.json", {"0-100": heard}), self.write_json("b.json", {"0-100": heard})
        run("consensus.py", srt, a, b, self.path("c.json"))
        prop = json.loads(self.read("c.json"))["proposals"]
        self.assertEqual([(x["kind"], x.get("after_cue")) for x in prop], [("insert", 0)])

    def test_words_missing_inside_a_cue_are_not_called_a_new_cue(self):
        cues = [(i * 5.0, i * 5.0 + 2.7, " ".join(W[(i * 3 + k) % len(W)] for k in range(3))) for i in range(8)]
        srt = self.write("a.srt", srt_text(cues))
        heard = sorted(fresh_from(cues) + [[16.2, 16.5, "violet"]])        # inside cue 4 (15.0 to 17.7)
        a, b = self.write_json("a.json", {"0-100": heard}), self.write_json("b.json", {"0-100": heard})
        run("consensus.py", srt, a, b, self.path("c.json"))
        prop = json.loads(self.read("c.json"))["proposals"]
        self.assertEqual([(x["kind"], x.get("inside_cue"), "after_cue" in x) for x in prop], [("insert", 4, False)])
        run("make_evidence.py", srt, a, b, self.path("c.json"), self.path("evidence"))
        batch = self.read(os.path.join("evidence", "batch_01.md"))
        self.assertIn("words missing inside cue 4", batch)
        self.assertNotIn("insert after cue", batch)

    def test_one_read_deleting_and_the_other_adding_is_recorded(self):
        cues = steady_cues(10)
        srt = self.write("a.srt", srt_text(cues))
        silent = [w for w in fresh_from(cues) if not (15.0 <= w[0] < 18.0)]                      # read A hears nothing in cue 6
        added = sorted(fresh_from(cues) + [[16.0, 16.3, "violet"]])                               # read B adds a word there
        a, b = self.write_json("a.json", {"0-100": silent}), self.write_json("b.json", {"0-100": added})
        p = run("consensus.py", srt, a, b, self.path("c.json"))
        res = json.loads(self.read("c.json"))
        self.assertEqual(res["proposals"], [])
        self.assertTrue(res["split"], "the disagreement must not vanish")
        self.assertIn("reads split", p.stdout)

    def test_a_repeated_word_inside_one_read_is_kept_by_retime(self):
        cues = [(5.0, 6.0, "first line here"), (10.0, 10.5, "stone stone"), (60.0, 61.0, "last line there")]
        heard = [[40.0, 40.3, "stone"], [40.45, 40.8, "stone"]]
        srt = self.write("a.srt", srt_text(cues))
        fresh = self.write_json("fresh.json", {"0-100": heard})
        run("retime.py", srt, 2, 2, 35, 50, self.path("retime.json"), fresh)
        edit = json.loads(self.read("retime.json"))[0]
        self.assertAlmostEqual(edit["new_end"], 41.05, delta=0.02)     # both words count, so the cue ends after the second

    def test_retime_with_a_range_that_matches_nothing_stops(self):
        srt = self.write("a.srt", srt_text(steady_cues(3)))
        fresh = self.write_json("fresh.json", {"0-100": []})
        for env in (None, {"PYTHONOPTIMIZE": "1"}):
            p = run("retime.py", srt, 8, 9, 0, 50, self.path("retime.json"), fresh, expect=1, env=env)
            self.assertIn("no cues numbered 8 to 9", p.stderr)

    def test_cues_rejects_an_unknown_mode_or_missing_numbers(self):
        f = self.write("a.srt", srt_text(steady_cues(3)))
        run("cues.py", f, "shw", 1, 2, expect=1)
        run("cues.py", f, "show", expect=1)
        run("cues.py", f, "at", 1, expect=1)

    def test_recheck_survives_a_console_that_cannot_print_the_path(self):
        env = {"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "SE_STT_TOOL": self.write("fake_tool.py", FAKE_TOOL)}
        work = os.path.join(self.dir.name, "work")
        os.mkdir(work)
        self.write(os.path.join("work", "full.flac"), "")
        folder = os.path.join(self.dir.name, "\u00e7\u0131kt\u0131")
        os.mkdir(folder)
        spans = self.write_json("spans.json", [[0, 50], [100, 150]])
        run("recheck.py", work, spans, os.path.join(folder, "out.json"), expect=2, env=env)
        self.assertTrue(os.path.exists(os.path.join(folder, "out.json.missing.json")))


if __name__ == "__main__":
    unittest.main()
