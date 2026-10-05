"""Offline tests for tools/transcribe-broadcast.py. Synthetic data only: no network, no media.
Run from the repository root with:

    python3 -m unittest discover tools/tests
"""
import importlib.util, os, random, shutil, sys, tempfile, unittest
from unittest import mock

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "transcribe-broadcast.py")


def load_script():
    spec = importlib.util.spec_from_file_location("transcribe_broadcast", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bc = load_script()
te = bc.te

EN_TEXT = "the garden of the children is what we learn about the summer and it was in the shed when they said that this is not what you can do".split()
EN_AS_AR = "ذا جاردن اوف ذا تشيلدرن ايز وات وي لرن ابوت ذا سامر اند ات واز ان ذا شد ون ذي ساد ذات ديس ايز نات وات يو كان دو".split()
AR_TEXT = "الحديقة في هذه القرية تعمل على رعاية الاطفال ولكن الطقس البارد صعب والناس تنتظر الشمس من الجبل الكبير".split()
AR_AS_EN = "alhadiqa fi hadihi alqarya ta'mal ala ri'ayat alatfal walakin alttaqs albarid sa'b walnas tantazir alshams min aljabal alkabir".split()


def speech(tokens, t0, step=0.35):
    """[start, end, word] rows for tokens spoken one after another from t0."""
    return [[t0 + i * step, t0 + i * step + 0.3, w] for i, w in enumerate(tokens)]


def two_language_file():
    """English 0-40 s, Arabic 40-80 s, English 80-120 s, each with a pause before it, as two runs would write it."""
    ar, en = [], []
    for base, lang in ((0.0, "en"), (41.0, "ar"), (82.0, "en")):
        for k in range(3):
            t0 = base + k * 13.0
            if lang == "en":
                n = len(EN_TEXT)
                en += speech(EN_TEXT, t0); ar += speech(EN_AS_AR, t0)
            else:
                n = len(AR_TEXT)
                en += speech(AR_AS_EN, t0); ar += speech(AR_TEXT, t0)
    return sorted(ar), sorted(en)


class Normalizers(unittest.TestCase):
    def test_arabic_letters_only(self):
        self.assertEqual(bc.norm_ar("الحلقة، 5 hello"), "الحلقة")
        self.assertEqual(bc.norm_ar("أهلاً،"), "اهلا")

    def test_english_lowercase_and_apostrophe(self):
        self.assertEqual(bc.norm_en("Don't,"), "don't")
        self.assertEqual(bc.norm_en("STOP!"), "stop")

    def test_definite_article(self):
        self.assertTrue(bc.has_def_article("الشباب"))
        self.assertFalse(bc.has_def_article("ال"))          # the bare article is not a word
        self.assertFalse(bc.has_def_article("ذا"))


class LanguageDecision(unittest.TestCase):
    def test_separates_two_languages(self):
        ar, en = two_language_file()
        stretches, llr, seeds = bc.decide(ar, en)
        by_time = lambda t: [c for c in stretches if c["s"] <= t <= c["e"]][0]["lang"]
        self.assertEqual(by_time(5.0), "en")
        self.assertEqual(by_time(45.0), "ar")
        self.assertEqual(by_time(100.0), "en")
        self.assertGreater(len(seeds), 3)

    def test_every_word_of_both_runs_lands_in_exactly_one_stretch(self):
        rnd = random.Random(7)
        for trial in range(20):
            ar, en, t = [], [], 0.0
            for _ in range(rnd.randint(40, 200)):
                t += rnd.choice([0.1, 0.2, 0.3, 0.6, 1.5, 4.0])
                d = rnd.uniform(0.1, 0.5)
                if rnd.random() < 0.9:
                    ar.append([t, t + d, rnd.choice(AR_TEXT + EN_AS_AR)])
                if rnd.random() < 0.9:
                    en.append([t + rnd.uniform(-0.05, 0.05), t + d, rnd.choice(EN_TEXT + AR_AS_EN) + rnd.choice(["", ".", "?"])])
            ar.sort(); en.sort()
            stretches, _, _ = bc.decide(ar, en)
            self.assertEqual(sorted(k for c in stretches for k in c["ar"]), list(range(len(ar))), trial)
            self.assertEqual(sorted(k for c in stretches for k in c["en"]), list(range(len(en))), trial)

    def test_unknown_words_are_judged_by_their_kind_or_not_at_all(self):
        llr = {"ar": {}, "en": {}}
        self.assertLess(bc.event_score("ar", [0, 1, "\u0627\u0644\u0645\u0646\u0637\u0642\u0629"], llr), 0)   # a definite article
        self.assertLess(bc.event_score("ar", [0, 1, "\u0641\u064a"], llr), 0)                                     # an Arabic function word
        self.assertGreater(bc.event_score("en", [0, 1, "the"], llr), 0)                                            # an English function word
        self.assertEqual(bc.event_score("ar", [0, 1, "\u0641\u0648\u0631\u0648\u0631\u062f"], llr), 0)        # a word nothing is known about
        self.assertEqual(bc.event_score("en", [0, 1, "forward"], llr), 0)

    def test_the_arabic_run_outweighs_the_english_run_for_english_evidence(self):
        llr = {"ar": {"\u0633\u0644\u0627\u0645": 2.0}, "en": {"y": 2.0}}
        self.assertEqual(bc.event_score("ar", [0, 1, "\u0633\u0644\u0627\u0645"], llr), 2.0)
        self.assertEqual(bc.event_score("en", [0, 1, "y"], llr), 1.0)          # fluent English there may be a translation
        self.assertEqual(bc.event_score("en", [0, 1, "z"], {"ar": {}, "en": {"z": -2.0}}), -2.0)   # romanized Arabic counts in full

    def test_arabic_letters_from_the_english_run_are_judged_like_arabic_run_words(self):
        llr = {"ar": {"\u0630\u0627": 2.0}, "en": {}}          # a transliterated "the" is English evidence
        self.assertGreater(bc.event_score("en", [0, 1, "\u0630\u0627"], llr), 0)
        self.assertLess(bc.event_score("en", [0, 1, "\u0627\u0644\u0628\u0644\u0627\u062f"], llr), 0)

    def test_the_hosts_arabic_is_not_taken_for_english_because_the_english_run_translated_it(self):
        """The Arabic run writes real Arabic; the English run wrote a fluent English translation of it."""
        ar, en = two_language_file()
        t0 = 130.0
        ar += speech("الجبال العالية والانهار التي تمر بها في المنطقة".split(), t0)
        en += speech("the high mountains and the rivers that run through them all year in the region".split(), t0)
        ar.sort(); en.sort()
        stretches, _, _ = bc.decide(ar, en)
        self.assertEqual([c for c in stretches if c["s"] <= t0 + 1.0 <= c["e"]][0]["lang"], "ar")

    def test_a_lone_filler_does_not_start_a_new_stretch(self):
        ar, en = two_language_file()
        t = 13.0 + len(EN_TEXT) * 0.35 + 0.6
        en.append([t, t + 0.2, "uh"]); ar.append([t, t + 0.2, "\u0627\u0627\u0627"])
        ar.sort(); en.sort()
        stretches, _, _ = bc.decide(ar, en)
        self.assertEqual([c for c in stretches if c["s"] <= t <= c["e"]][0]["lang"], "en")

    def test_digits_alone_are_neutral(self):
        llr = {"ar": {}, "en": {}}
        self.assertEqual(bc.event_score("en", [0, 1, "5"], llr), 0.0)
        self.assertEqual(bc.event_score("ar", [0, 1, "5"], llr), 0.0)

    def test_switching_is_dearer_in_the_middle_of_a_run_than_after_a_pause(self):
        weak = [(0, 0.3, 10), (0.4, 0.7, 10), (0.8, 1.1, -4), (1.2, 1.5, -4), (1.6, 1.9, 10), (2.0, 2.3, 10)]
        self.assertEqual(bc.smooth(weak), ["en"] * 6)                                    # no pause: two words do not switch
        paused = [(0, 0.3, 10), (0.4, 0.7, 10), (2.0, 2.3, -4), (2.4, 2.7, -4), (4.0, 4.3, 10), (4.4, 4.7, 10)]
        self.assertEqual(bc.smooth(paused), ["en", "en", "ar", "ar", "en", "en"])       # the same evidence after pauses does
        self.assertEqual(bc.smooth([(0, 1, 10), (1.1, 2, -9), (2.1, 3, -9), (3.1, 4, -9)]), ["en", "ar", "ar", "ar"])
        self.assertEqual(bc.smooth([]), [])

    def test_a_rare_word_says_nothing_until_it_has_been_met_often(self):
        ar = speech(["\u0631\u0627\u0631"] * 12, 0.0); en = speech(["word"] * 12, 0.0)
        tab = bc.train(ar, en, {0: "ar"}, size=20.0, alpha=3.0, min_count=10)
        self.assertIn("\u0631\u0627\u0631", tab["ar"])
        tab = bc.train(ar[:5], en[:5], {0: "ar"}, size=20.0, alpha=3.0, min_count=10)
        self.assertNotIn("\u0631\u0627\u0631", tab["ar"])


def window(tag, span, core, words):
    return {"tag": tag, "span": span, "core": core,
            "words": [{"s": s, "e": e, "w": "x", "spk": spk} for s, e, spk in words]}


class SpeakerStitching(unittest.TestCase):
    def two_windows(self, second_labels):
        """Speaker A talks 0-300 then B 300-600; the second window sees 200-800 and names them differently."""
        w1 = window("dz-000", (0.0, 400.0), (0.0, 300.0),
                    [(t, t + 0.4, "1") for t in range(0, 300)] + [(t, t + 0.4, "2") for t in range(300, 400)])
        a, b = second_labels
        w2 = window("dz-001", (200.0, 800.0), (300.0, 800.0),
                    [(t - 200.0, t - 200.0 + 0.4, a) for t in range(200, 300)] +
                    [(t - 200.0, t - 200.0 + 0.4, b) for t in range(300, 600)] +
                    [(t - 200.0, t - 200.0 + 0.4, "3") for t in range(600, 800)])
        return [w1, w2]

    def test_a_speaker_is_followed_into_the_next_window_under_another_label(self):
        wins = self.two_windows(("7", "5"))
        timeline, report = bc.stitch(wins)
        tl = bc.Timeline(timeline)
        self.assertEqual(tl.at(100.0), "S1")
        self.assertEqual(tl.at(350.0), "S2")            # same person as before, whatever the window called them
        self.assertEqual(tl.at(700.0), "S3")            # someone new

    def test_the_second_window_is_only_used_from_its_own_core(self):
        timeline, _ = bc.stitch(self.two_windows(("7", "5")))
        for s, e, lab in timeline:
            self.assertGreaterEqual(s, 0.0)
        self.assertTrue(all(e <= 800.0 for _, e, _ in timeline))

    def test_a_label_that_shares_no_time_becomes_a_new_speaker(self):
        wins = self.two_windows(("7", "5"))
        # nobody of window two speaks inside the shared time
        wins[1]["words"] = [w for w in wins[1]["words"] if w["s"] + 200.0 >= 400.0]      # only after the shared audio
        wins[1]["words"] = [dict(w, spk="9") for w in wins[1]["words"]]
        timeline, report = bc.stitch(wins)
        self.assertTrue(any(r["matched"] is None for r in report))

    def test_broken_offsets_do_not_make_segments(self):
        win = window("dz-000", (0.0, 100.0), (0.0, 100.0), [(5.0, 4.0, "1"), (1.0, 2.0, "1"), (3.0, 60.0, "1")])
        self.assertEqual(bc.window_segments(win), [[1.0, 2.0, "1"]])

    def test_a_single_word_flip_inside_a_turn_is_smoothed(self):
        win = window("dz-000", (0.0, 100.0), (0.0, 100.0), [(1.0, 1.4, "1"), (1.5, 1.9, "2"), (2.0, 2.4, "1")])
        self.assertEqual(bc.window_segments(win), [[1.0, 2.4, "1"]])

    def test_timeline_reaches_a_second_past_a_segment(self):
        tl = bc.Timeline([[10.0, 12.0, "S1"], [20.0, 22.0, "S2"]])
        self.assertEqual(tl.at(11.0), "S1")
        self.assertEqual(tl.at(12.7), "S1")
        self.assertIsNone(tl.at(16.0))
        self.assertEqual(tl.at(19.4), "S2")


class SoundGaps(unittest.TestCase):
    def loud(self, n=1000):
        return [-70.0] * n, [-70.0] * n

    def test_loud_gap_is_kept_silent_gap_is_not(self):
        rms, peak = self.loud()
        for i in range(300, 400):                 # 30-40 s is music
            rms[i], peak[i] = -25.0, -12.0
        words = [{"s": 0.0, "e": 5.0}, {"s": 25.0, "e": 29.0}, {"s": 41.0, "e": 45.0}, {"s": 70.0, "e": 72.0}]
        gaps = bc.sound_gaps(words, (rms, peak), 100.0)
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0]["s"], gaps[0]["e"]), (29.0, 41.0))
        self.assertTrue(gaps[0]["sustained"])
        self.assertTrue(gaps[0]["audiblyNonSilent"])

    def test_a_short_blip_is_a_sound_not_music(self):
        rms, peak = self.loud()
        for i in range(100, 108):
            rms[i], peak[i] = -30.0, -14.0
        words = [{"s": 0.0, "e": 9.0}, {"s": 12.0, "e": 15.0}]
        gaps = bc.sound_gaps(words, (rms, peak), 20.0)
        self.assertEqual(len(gaps), 1)
        self.assertFalse(gaps[0]["sustained"])


class ForeignAndCrossCheck(unittest.TestCase):
    def test_foreign_runs_merge_across_short_pauses_and_fillers(self):
        words = [{"w": "hello", "s": 0.0, "e": 0.4, "lang": "en"}, {"w": "there", "s": 0.5, "e": 1.0, "lang": "en"},
                 {"w": "uh", "s": 1.1, "e": 1.3, "lang": "ar"},
                 {"w": "again", "s": 1.4, "e": 2.0, "lang": "en"},
                 {"w": "مرحبا", "s": 9.0, "e": 9.5, "lang": "ar"}]
        segs = bc.foreign_segments(words, "ar")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["language"], "en")
        self.assertEqual(segs[0]["original"], "hello there again")      # a filler of the other language neither splits it nor joins it

    def test_cross_check_lists_words_the_two_runs_read_differently(self):
        ar = speech("الحديقة تعمل على رعاية الاطفال".split(), 0.0)
        en = speech("الحديقة تعمل على رغاية الاطفال".split(), 0.0)
        isl = [{"lang": "ar", "ar": list(range(len(ar))), "en": list(range(len(en)))}]
        diff = bc.cross_check(isl, ar, en)
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["heard"], "رعاية")
        self.assertEqual(diff[0]["other"], "رغاية")


class CompiledCues(unittest.TestCase):
    def words(self):
        ws = []
        t = 0.0
        for lang, spk, text in (("en", "S1", "Hello and welcome. Thanks for having me."), ("en", "S2", "It is a pleasure."),
                                ("ar", "S1", "مرحبا بكم جميعا.")):
            for tok in text.split():
                ws.append({"w": tok, "s": round(t, 3), "e": round(t + 0.3, 3), "lang": lang, "speaker": spk})
                t += 0.35
            t += 0.2
        return ws

    def test_a_cue_never_mixes_speakers_or_languages(self):
        ws = self.words()
        cues, groups = bc.build_cues(ws, [], 30.0)
        for idxs in groups:
            self.assertEqual(len({(ws[k]["lang"], ws[k]["speaker"]) for k in idxs}), 1)
        self.assertEqual(sum(len(g) for g in groups), len(ws))

    def test_cues_are_in_order_and_do_not_overlap(self):
        cues, _ = bc.build_cues(self.words(), [], 30.0)
        self.assertEqual(te.cue_violations(cues, 30.0), {"inverted": 0, "out_of_order": 0, "overlap": 0, "outside": 0})

    def test_sound_events_become_cues_between_speech(self):
        ws = self.words()
        ev = [{"label": "[صوت]", "s": 20.0, "e": 24.0}]
        cues, groups = bc.build_cues(ws, ev, 30.0)
        self.assertEqual(cues[-1][2], "[صوت]")
        self.assertEqual(groups[-1], [])

    def test_cue_text_is_the_word_stream(self):
        ws = self.words()
        cues, groups = bc.build_cues(ws, [], 30.0)
        joined = " ".join(c[2] for c in cues).split()
        self.assertEqual(sorted(joined), sorted(w["w"] for w in ws))


class ShortWindowGaps(unittest.TestCase):
    def dz(self, spec):
        return [{"span": (0.0, 200.0), "core": (0.0, 200.0),
                 "words": [{"s": s, "e": e, "w": w, "spk": "1"} for s, e, w in spec]}]

    def word(self, w, s, e, lang="en", **kw):
        return dict({"w": w, "s": s, "e": e, "lang": lang}, **kw)

    def test_an_empty_stretch_inside_english_is_filled_from_the_short_windows(self):
        words = [self.word("hello", 0.0, 0.4), self.word("again", 9.0, 9.4)]
        stretches = [{"lang": "en", "s": 0.0, "e": 9.4}]
        heard = self.dz([(0.0, 0.4, "hello"), (2.0, 2.3, "yes"), (2.4, 2.8, "you"), (3.0, 3.4, "are"), (9.0, 9.4, "again")])
        added = bc.fill_from_short_windows(words, heard, stretches)
        self.assertEqual(added, 3)
        self.assertEqual([w["w"] for w in words], ["hello", "yes", "you", "are", "again"])
        self.assertTrue(all(w.get("recalled") for w in words if w["w"] in ("yes", "you", "are")))

    def test_nothing_is_added_in_an_arabic_stretch_where_the_short_windows_translated(self):
        words = [self.word("\u0645\u0631\u062d\u0628\u0627", 0.0, 0.4, "ar"), self.word("\u0634\u0643\u0631\u0627", 9.0, 9.4, "ar")]
        stretches = [{"lang": "ar", "s": 0.0, "e": 9.4}]
        heard = self.dz([(2.0, 2.3, "welcome"), (2.4, 2.8, "to"), (3.0, 3.4, "the")])
        self.assertEqual(bc.fill_from_short_windows(words, heard, stretches), 0)

    def test_a_word_both_runs_heard_is_not_added_twice(self):
        words = [self.word("hello", 0.0, 0.4), self.word("there", 0.5, 0.9), self.word("you", 1.0, 1.4)]
        stretches = [{"lang": "en", "s": 0.0, "e": 1.4}]
        heard = self.dz([(0.1, 0.5, "hello"), (0.6, 1.0, "there"), (1.1, 1.5, "you")])
        self.assertEqual(bc.fill_from_short_windows(words, heard, stretches), 0)

    def test_words_outside_a_windows_core_are_left_to_the_neighbouring_window(self):
        words = [self.word("a", 0.0, 0.4), self.word("b", 20.0, 20.4)]
        stretches = [{"lang": "en", "s": 0.0, "e": 20.4}]
        heard = [{"span": (0.0, 100.0), "core": (30.0, 60.0), "words": [{"s": 5.0, "e": 5.3, "w": "x", "spk": "1"}, {"s": 6.0, "e": 6.3, "w": "y", "spk": "1"}]}]
        self.assertEqual(bc.fill_from_short_windows(words, heard, stretches), 0)


class SpeakerMerging(unittest.TestCase):
    def word(self, s, lang, spk, d=1.0):
        return {"w": "x", "s": s, "e": s + d, "lang": lang, "speaker": spk}

    def test_labels_are_read_as_two_people_by_language_and_by_who_was_heard_together(self):
        words = [self.word(0, "en", "S4", 100), self.word(200, "en", "S11", 80), self.word(300, "ar", "S3", 40),
                 self.word(400, "ar", "S7", 30), self.word(500, "en", "S14", 60)]
        conflicts = {(3, 4): 20.0, (3, 11): 15.0, (7, 11): 10.0, (3, 14): 12.0}
        mapping = bc.merge_speakers(words, conflicts, 2)
        self.assertEqual({mapping[g] for g in ("S4", "S11", "S14")}, {mapping["S4"]})
        self.assertEqual({mapping[g] for g in ("S3", "S7")}, {mapping["S3"]})
        self.assertNotEqual(mapping["S4"], mapping["S3"])
        self.assertEqual(sorted(set(mapping.values())), ["S1", "S2"])
        self.assertEqual(words[0]["speaker"], "S1")           # numbered by who speaks first

    def test_labels_heard_together_are_never_left_on_one_side(self):
        # both mostly English, but heard together in a window: two people
        words = [self.word(0, "en", "S1", 100), self.word(200, "en", "S2", 90)]
        mapping = bc.merge_speakers(words, {(1, 2): 500.0}, 2)
        self.assertNotEqual(mapping["S1"], mapping["S2"])

    def test_only_two_speakers_are_supported_and_anything_else_leaves_the_labels_alone(self):
        words = [self.word(0, "en", "S1"), self.word(5, "ar", "S2")]
        self.assertEqual(bc.merge_speakers(words, {}, 3), {"S1": "S1", "S2": "S2"})
        self.assertEqual([w["speaker"] for w in words], ["S1", "S2"])

    def test_a_word_without_a_label_takes_the_nearest_of_its_own_language(self):
        words = [self.word(0, "en", "S1"), {"w": "y", "s": 1.5, "e": 2.0, "lang": "en"}, self.word(3, "ar", "S2")]
        guessed = bc.fill_speakers(words)
        self.assertEqual(words[1]["speaker"], "S1")
        self.assertEqual(guessed, 0)

    def test_a_word_with_no_neighbour_is_guessed_from_its_language_and_marked(self):
        words = [self.word(0, "en", "S1", 50), self.word(100, "ar", "S2", 50), {"w": "y", "s": 500.0, "e": 500.5, "lang": "ar"}]
        guessed = bc.fill_speakers(words)
        self.assertEqual((words[2]["speaker"], words[2]["speakerGuess"], guessed), ("S2", True, 1))

    def test_words_of_the_other_language_take_the_speaker_who_mostly_speaks_it(self):
        words = [self.word(0, "ar", "S1", 30), self.word(40, "ar", "S2", 5), self.word(50, "en", "S2", 60), self.word(120, "en", "S1", 4)]
        self.assertEqual(bc.arabic_words_take_the_arabic_speaker(words), "S1")
        self.assertEqual([w["speaker"] for w in words], ["S1", "S1", "S2", "S1"])       # English keeps its own label

    def test_nothing_changes_when_nobody_speaks_the_other_language(self):
        words = [self.word(0, "en", "S1", 30)]
        self.assertIsNone(bc.arabic_words_take_the_arabic_speaker(words))
        self.assertEqual(words[0]["speaker"], "S1")

    def test_fillers_are_flagged_and_words_are_not(self):
        words = [{"w": "uh", "s": 0, "e": 1, "lang": "en"}, {"w": "Uh,", "s": 1, "e": 2, "lang": "en"},
                 {"w": "\u0627\u0627\u0627", "s": 2, "e": 3, "lang": "ar"}, {"w": "hello", "s": 3, "e": 4, "lang": "en"}]
        bc.mark_fillers(words)
        self.assertEqual([bool(w.get("filler")) for w in words], [True, True, True, False])

    def test_stitch_reports_which_labels_were_heard_together(self):
        win = window("dw-000", (0.0, 100.0), (0.0, 100.0), [(t, t + 0.4, "1") for t in range(0, 40)] + [(t, t + 0.4, "2") for t in range(40, 80)])
        _, _, conflicts = bc.stitch([win], with_conflicts=True)
        self.assertGreater(conflicts[(1, 2)], 10.0)


class EngineRequests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"SE_STT_LANGUAGE": "tr-TR", "SE_STT_MODEL": "chirp_3"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def engine(self, lang, tag, diarize):
        e = bc.Engine(os.path.join(self.tmp, "a.wav"), self.tmp, lang, tag, diarize=diarize)
        os.makedirs(e.raw, exist_ok=True)
        e.deploy = lambda: ("proj", "bucket", "gcloud")
        e.video_bytes = 1
        return e

    def submitted_body(self, e):
        sent = {}
        def fake_api(method, url, body=None, **kw):
            sent["body"] = body
            return {"name": "operations/1"}
        e.api = fake_api
        e.submit(e.raw, "part-000", 0.0, 60.0, "gs://bucket/x.flac")
        return sent["body"]

    def test_the_run_language_wins_over_the_environment(self):
        body = self.submitted_body(self.engine("ar-XA", "ar", False))
        self.assertEqual(body["config"]["languageCodes"], ["ar-XA"])
        self.assertNotIn("diarizationConfig", body["config"]["features"])
        self.assertTrue(body["config"]["features"]["enableWordTimeOffsets"])

    def test_only_the_diarization_run_asks_for_speakers(self):
        body = self.submitted_body(self.engine("en-US", "dz", True))
        self.assertEqual(body["config"]["languageCodes"], ["en-US"])
        self.assertIn("diarizationConfig", body["config"]["features"])

    def test_each_job_has_its_own_upload_prefix(self):
        a, b = self.engine("ar-XA", "ar", False), self.engine("en-US", "en", False)
        self.assertNotEqual(a.prefix, b.prefix)

    def test_windows_tile_the_file_and_share_their_overlap(self):
        e = self.engine("en-US", "dz", True)
        for total in (100.0, 180.0, 200.0, 555.0, 3308.8):
            wins = e.diar_windows(total)
            self.assertEqual(wins[0]["core"][0], 0.0)
            self.assertEqual(wins[-1]["core"][1], total)
            self.assertEqual(wins[-1]["span"][1], total)
            for w, nxt in zip(wins, wins[1:]):
                self.assertEqual(w["core"][1], nxt["core"][0])                       # the cores tile the file
                self.assertAlmostEqual(w["span"][1] - nxt["span"][0], bc.DIAR_OVERLAP)  # the audio both heard
            for w in wins:
                self.assertLessEqual(w["span"][1] - w["span"][0], bc.DIAR_WIN + 30.0)
                self.assertLessEqual(w["span"][1] - w["span"][0], 1195.0)            # under Google's 20 minute cap
                self.assertLessEqual(w["span"][0], w["core"][0])
                self.assertGreaterEqual(w["span"][1], w["core"][1])

    def test_a_tiny_tail_joins_the_last_window(self):
        e = self.engine("en-US", "dz", True)
        wins = e.diar_windows(bc.DIAR_WIN + 20.0)
        self.assertEqual(len(wins), 1)

    def words(self, spec):
        return [{"s": s, "e": e, "w": w, "spk": "1"} for s, e, w in spec]

    def test_a_transcript_that_stops_while_the_audio_is_loud_is_cut_short(self):
        e = self.engine("en-US", "dz", True)
        e.loud = ([-30.0] * 3000, [-10.0] * 3000)
        w = {"tag": "dw-000", "span": (0.0, 180.0), "core": (0.0, 150.0)}
        self.assertTrue(e.cut_short(w, self.words([(1.0, 1.4, "hello"), (60.0, 60.4, "there")])))

    def test_a_transcript_that_stops_in_silence_is_complete(self):
        e = self.engine("en-US", "dz", True)
        e.loud = ([-70.0] * 3000, [-70.0] * 3000)
        w = {"tag": "dw-000", "span": (0.0, 180.0), "core": (0.0, 150.0)}
        self.assertFalse(e.cut_short(w, self.words([(1.0, 1.4, "hello"), (60.0, 60.4, "there")])))

    def test_the_labellers_own_text_at_the_end_means_cut_short(self):
        e = self.engine("en-US", "dz", True)
        e.loud = ([-70.0] * 3000, [-70.0] * 3000)
        w = {"tag": "dw-000", "span": (0.0, 180.0), "core": (0.0, 150.0)}
        spec = [(1.0, 1.4, "hello"), (170.0, 170.5, "there"), (175.0, None, "sp"), (None, 175.0, "k:1")]
        self.assertTrue(e.cut_short(w, self.words(spec)))
        self.assertTrue(bc.LEAK.match("Speaker:2"))
        self.assertFalse(bc.LEAK.match("spoke"))

    def test_a_nonsense_offset_at_the_end_does_not_make_a_short_transcript_look_complete(self):
        e = self.engine("en-US", "dz", True)
        e.loud = ([-30.0] * 3000, [-10.0] * 3000)
        w = {"tag": "dw-000", "span": (0.0, 180.0), "core": (0.0, 150.0)}
        spec = [(1.0, 1.4, "hello"), (30.0, 30.5, "there"), (58640619982.16, 14316555.68, "content")]
        self.assertTrue(e.cut_short(w, self.words(spec)))
        self.assertEqual([x["w"] for x in e.sane(self.words(spec), 180.0)], ["hello", "there"])

    def test_a_cut_short_window_splits_into_overlapping_halves_that_keep_its_core(self):
        e = self.engine("en-US", "dz", True)
        e.quiet = [90.0, 150.0]
        w = {"tag": "dw-001", "core": (30.0, 300.0), "span": (0.0, 330.0)}
        first, second = e.halves(w)
        self.assertEqual(first["span"][0], 0.0)
        self.assertEqual(second["span"][1], 330.0)
        self.assertEqual(first["core"][1], second["core"][0])
        self.assertEqual((first["core"][0], second["core"][1]), (30.0, 300.0))
        self.assertAlmostEqual(first["span"][1] - second["span"][0], 40.0)
        self.assertIsNone(e.halves({"tag": "t", "core": (0, 100), "span": (0.0, 150.0)}))   # too short to split

    def test_a_split_window_is_not_asked_for_again(self):
        e = self.engine("en-US", "dz", True)
        e.quiet = [165.0]
        e.loud = ([-70.0] * 5000, [-70.0] * 5000)
        w = {"tag": "dw-001", "core": (0.0, 300.0), "span": (0.0, 330.0)}
        open(f"{e.raw}/dw-001.split", "w").close()
        e.recognize = mock.Mock(side_effect=AssertionError("must not be sent again"))
        e.window_response = lambda x: (x["tag"], x["span"], {"ok": True})
        self.assertEqual([x["tag"] for x in e.fetch_window(w)], ["dw-001a", "dw-001b"])

    def test_a_window_too_short_to_split_is_kept_and_marked(self):
        e = self.engine("en-US", "dz", True)
        e.quiet = []
        e.loud = ([-30.0] * 3000, [-10.0] * 3000)
        w = {"tag": "dw-002", "core": (0.0, 100.0), "span": (0.0, 120.0)}
        st = {"response": {"results": {"f": {"inlineResult": {"transcript": {"results": [{"alternatives": [{"words": [
            {"word": "hi", "startOffset": "1s", "endOffset": "2s", "speakerLabel": "1"}]}]}]}}}}}}
        e.window_response = lambda x: (x["tag"], x["span"], st)
        out = e.fetch_window(w)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["cut_short"])


if __name__ == "__main__":
    unittest.main()
