"""Found by an independent tester, 24.10: the 7-day forecast stretched a 45-minute burst over three days."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import limit_forecast as lf  # noqa: E402
from test_limit_forecast import H, T0, Base, event  # noqa: E402

DAY = 24 * H


class Week(Base):
    def feed(self, pcts, reset, step=300):
        for i, p in enumerate(pcts):
            self.line(event(seven=p, seven_reset=reset), T0 + i * step)
        return T0 + (len(pcts) - 1) * step

    def test_a_short_burst_does_not_cry_wolf_for_the_week(self):
        # 4 days into the week at 41%, a 35-minute burst to 43.1%
        now = self.feed([41 + 0.3 * i for i in range(8)], reset=T0 + 3 * DAY)
        line = self.line(event(seven=43.1, seven_reset=T0 + 3 * DAY), now)
        self.assertFalse(line.startswith("!"), line)

    def test_a_heavy_week_is_flagged_by_its_whole_pace(self):
        # 4 days in, already at 80%: 20 points/day, 3 days left -> runs out about a day in
        now = self.feed([80.0, 80.1], reset=T0 + 3 * DAY)
        line = self.line(event(seven=80.1, seven_reset=T0 + 3 * DAY), now)
        self.assertTrue(line.startswith("!"), line)

    def test_the_week_line_says_what_is_left_per_day(self):
        now = self.feed([43.0, 43.0], reset=T0 + 3 * DAY)
        line = self.line(event(seven=43.0, seven_reset=T0 + 3 * DAY), now)
        self.assertIn("19%/day", line)                       # (100 - 43) / 3 days

    def test_status_shows_the_same(self):
        self.feed([43.0, 43.0], reset=T0 + 3 * DAY)
        f = lf.forecast(lf.load(), "seven_day", T0 + 300)
        self.assertAlmostEqual(f["per_day_left"], 19.0, places=0)


class HookErrors(Base):
    def test_a_hook_error_is_logged_not_swallowed(self):
        with open(os.path.join(self.tmp.name, "warned.json"), "w") as f:
            f.write("{broken")
        self.line(event(five=95, five_reset=T0 + H), T0)
        code, _ = self.hook(T0 + 60)
        self.assertEqual(code, 0)                            # still never blocks the prompt
        with open(os.path.join(self.tmp.name, "errors.log"), encoding="utf-8") as f:
            self.assertIn("JSONDecodeError", f.read())


if __name__ == "__main__":
    unittest.main()
