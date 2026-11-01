import unittest
from datetime import timedelta

import numpy as np

from advisor.basal_night import analyze
from advisor.config import NightBasalConfig
from advisor.data import parse_entries, parse_treatments
from advisor.insulin import model_for
from advisor.profile import ProfileTimeline, parse_tz
from advisor.report import format_night_basal
from tests.simulate import simulate


def run(programmed, true_need, extra=None, days=28):
    entries, treatments, profiles, end = simulate(days=days + 2, programmed=programmed, true_need=true_need)
    treatments = treatments + (extra(end) if extra else [])
    prof = ProfileTimeline(profiles)
    ds = parse_treatments(treatments)
    ds.glucose = parse_entries(entries)
    end_day = (end - timedelta(days=1)).astimezone(prof.current.tz).date()
    return analyze(ds, prof, NightBasalConfig(days=days), end_day=end_day)


class InsulinModelTests(unittest.TestCase):
    def test_matches_loopkit_shape(self):
        m = model_for("rapidActingAdult")
        self.assertAlmostEqual(float(m.effect_remaining(0)), 1.0)
        self.assertAlmostEqual(float(m.effect_remaining(10)), 1.0)  # délai de 10 min
        self.assertAlmostEqual(float(m.effect_remaining(370)), 0.0)
        r = m.effect_remaining(np.arange(0, 371, 5.0))
        self.assertTrue(np.all(np.diff(r) <= 1e-12))
        act = -np.diff(r)
        self.assertAlmostEqual(float(np.argmax(act) * 5 + 2.5), 85, delta=5)  # pic 75 min + délai 10


class ProfileTests(unittest.TestCase):
    def test_fixed_offset_tz(self):
        self.assertEqual(parse_tz("GMT-0400").utcoffset(None), timedelta(hours=-4))
        self.assertEqual(parse_tz("ETC/GMT+4").utcoffset(None), timedelta(hours=-4))


class NightBasalTests(unittest.TestCase):
    def test_basal_too_low_is_detected_and_bounded(self):
        r = run(programmed=0.8, true_need=1.0)
        self.assertGreaterEqual(r.nights_valid, 25)
        self.assertGreater(r.median_delivery_ratio, 1.15)
        seg = r.segments[0]
        self.assertAlmostEqual(seg.estimated_need_u_h, 1.0, delta=0.06)
        self.assertEqual(seg.action, "augmenter")
        self.assertLessEqual(seg.proposed_u_h, 0.8 * 1.15 + 1e-9)
        self.assertEqual(seg.confidence, "élevée")
        for h in r.hourly:
            self.assertAlmostEqual(h["median_need_u_h"], 1.0, delta=0.08)

    def test_basal_too_high_is_detected(self):
        r = run(programmed=1.2, true_need=1.0)
        seg = r.segments[0]
        self.assertEqual(seg.action, "diminuer")
        self.assertGreaterEqual(seg.proposed_u_h, 1.2 * 0.85 - 1e-9)

    def test_correct_basal_gives_no_change(self):
        r = run(programmed=1.0, true_need=1.0)
        self.assertEqual(r.segments[0].action, "aucun changement")

    def test_exclusions(self):
        def extra(end):
            d = (end - timedelta(days=5)).replace(hour=6)  # ~02:00 en GMT-4
            return [
                {"eventType": "Temporary Override", "created_at": d.strftime("%Y-%m-%dT%H:%M:%SZ"), "duration": 120},
                {"eventType": "Site Change", "created_at": (d - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")},
                {"eventType": "Carb Correction", "carbs": 15,
                 "created_at": (d - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            ]
        r = run(programmed=0.8, true_need=1.0, extra=extra)
        self.assertEqual(r.exclusion_summary.get("override actif"), 1)
        self.assertEqual(r.exclusion_summary.get("changement de site"), 1)
        self.assertEqual(r.exclusion_summary.get("glucides pendant la nuit"), 1)
        text = format_night_basal(r)
        self.assertIn("Par segment de basale", text)

    def test_too_few_nights(self):
        r = run(programmed=0.8, true_need=1.0, days=3)
        self.assertEqual(r.segments[0].action, "pas de recommandation")


if __name__ == "__main__":
    unittest.main()
