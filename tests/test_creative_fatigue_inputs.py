"""predict-fatigue accepts the histories real exports produce, and says what it used.

Follow-up to the calendar-day fix (PR #17, test_creative_fatigue_calendar.py). That fix
made every row carry a positive CPM, which rejected CTR-only histories (email, and
search bought on CPC) that worked before, and it rejected newest-first exports, ISO
timestamps and numeric strings with errors such as "Invalid performance history: 'cpm'".
Each test here was run against the pre-follow-up script and failed there.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import import_script  # noqa: E402

predictor = import_script("creative-fatigue-predictor.py", module_name="fatigue_inputs_test")


def rows(ctrs, cpms=None, start=date(2026, 1, 1), step=1, iso=False):
    out = []
    for i, ctr in enumerate(ctrs):
        day = (start + timedelta(days=i * step)).isoformat()
        row = {"date": day + ("T09:30:00Z" if iso else ""), "ctr": ctr}
        if cpms is not None and cpms[i] is not None:
            row["cpm"] = cpms[i]
        out.append(row)
    return out


class TestCpmIsOptional(unittest.TestCase):
    def test_ctr_only_history_forecasts_from_ctr(self):
        r = predictor.predict_fatigue("email-1", rows([0.10, 0.095, 0.09]))
        self.assertNotIn("error", r)
        self.assertEqual(r["days_remaining"], 4)
        self.assertEqual(r["signals_used"], ["ctr"])
        self.assertIsNone(r["cpm_trend_slope"])
        self.assertIn("CTR only", r["signals_note"])

    def test_cpm_missing_on_some_rows_drops_cpm_and_names_the_rows(self):
        r = predictor.predict_fatigue("c", rows([0.10, 0.095, 0.09], cpms=[10, None, 30]))
        self.assertEqual(r["signals_used"], ["ctr"])
        self.assertIn("row(s) 2", r["signals_note"])
        self.assertEqual(r["days_remaining"], 4, "a rising CPM on rows that cannot be used must not count")

    def test_zero_baseline_cpm_drops_cpm(self):
        r = predictor.predict_fatigue("c", rows([0.10, 0.095, 0.09], cpms=[0, 10, 11]))
        self.assertNotIn("error", r)
        self.assertEqual(r["signals_used"], ["ctr"])
        self.assertIn("earliest cpm is 0", r["signals_note"])

    def test_full_history_still_uses_both_signals(self):
        r = predictor.predict_fatigue("c", rows([0.10] * 3, cpms=[10, 11, 12]))
        self.assertEqual(r["signals_used"], ["ctr", "cpm"])
        self.assertEqual(r["days_remaining"], 1)
        self.assertNotIn("signals_note", r)

    def test_a_given_but_invalid_cpm_is_still_an_error(self):
        r = predictor.predict_fatigue("c", rows([0.10, 0.095, 0.09], cpms=[10, -1, 12]))
        self.assertIn("row 2 'cpm'", r["error"])


class TestErrorsNameTheRowAndField(unittest.TestCase):
    def test_missing_fields(self):
        h = rows([0.10, 0.095, 0.09])
        del h[1]["date"]
        self.assertIn("row 2 is missing 'date'", predictor.predict_fatigue("c", h)["error"])
        h = rows([0.10, 0.095, 0.09])
        del h[2]["ctr"]
        self.assertIn("row 3 is missing 'ctr'", predictor.predict_fatigue("c", h)["error"])

    def test_bad_values(self):
        h = rows([0.10, 0.095, 0.09])
        h[0]["date"] = "01/01/2026"
        self.assertIn("row 1 'date'", predictor.predict_fatigue("c", h)["error"])
        h = rows([0.10, 0.095, 0.09])
        h[1]["ctr"] = "4.5%"
        self.assertIn("row 2 'ctr' '4.5%' is not a number", predictor.predict_fatigue("c", h)["error"])
        h = rows([0.10, 0.095, 0.09])
        h[2] = "not a row"
        self.assertIn("row 3 is not an object", predictor.predict_fatigue("c", h)["error"])


class TestOrderAndDates(unittest.TestCase):
    def test_newest_first_is_sorted_and_gives_the_same_answer(self):
        oldest_first = predictor.predict_fatigue("c", rows([0.10, 0.095, 0.09], cpms=[10, 10, 10]))
        newest_first = predictor.predict_fatigue("c", list(reversed(rows([0.10, 0.095, 0.09], cpms=[10, 10, 10]))))
        self.assertNotIn("error", newest_first)
        for key in ("days_remaining", "predicted_fatigue_date", "ctr_trend_slope"):
            self.assertEqual(newest_first[key], oldest_first[key], key)
        self.assertEqual(newest_first["predicted_fatigue_date"], "2026-01-07", "counted from the latest date")

    def test_duplicate_dates_are_still_rejected_by_date(self):
        h = rows([0.10, 0.095, 0.09])
        h[2]["date"] = h[0]["date"]
        self.assertIn("two rows share the date 2026-01-01", predictor.predict_fatigue("c", h)["error"])

    def test_iso_timestamps_use_the_date_part(self):
        plain = predictor.predict_fatigue("c", rows([0.10, 0.095, 0.09], step=7))
        iso = predictor.predict_fatigue("c", rows([0.10, 0.095, 0.09], step=7, iso=True))
        self.assertNotIn("error", iso)
        self.assertEqual(iso["days_remaining"], plain["days_remaining"])
        self.assertEqual(iso["predicted_fatigue_date"], "2026-02-12")

    def test_numeric_strings_are_read_as_numbers(self):
        h = [{"date": d, "ctr": c, "cpm": "10"} for d, c in
             (("2026-01-01", "0.10"), ("2026-01-02", " 0.095 "), ("2026-01-03", "0.09"))]
        r = predictor.predict_fatigue("c", h)
        self.assertNotIn("error", r)
        self.assertEqual(r["days_remaining"], 4)
        self.assertEqual(r["signals_used"], ["ctr", "cpm"])


if __name__ == "__main__":
    unittest.main()
