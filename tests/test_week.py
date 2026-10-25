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


class Full(Base):
    """An outside re-grade (25.10): at 100% the line said 'full ~0m', at 104% '-1%/day left'."""

    def test_a_full_week_says_limit_reached_and_when_it_resets(self):
        self.line(event(seven=100.0, seven_reset=T0 + 2 * DAY), T0)
        line = self.line(event(seven=104.0, seven_reset=T0 + 2 * DAY), T0 + 300)
        self.assertIn("7d 100% limit reached, reset in 47h55", line)
        self.assertNotIn("full ~", line)
        self.assertNotIn("day left", line)
        self.assertTrue(line.startswith("! "), line)                # still flagged: you are out

    def test_a_full_five_hour_window_too(self):
        self.line(event(five=100.0, five_reset=T0 + H), T0)
        line = self.line(event(five=100.0, five_reset=T0 + H), T0 + 600)
        self.assertIn("5h 100% limit reached, reset in 50m", line)
        self.assertTrue(line.startswith("! "), line)
        code, out = self.hook(T0 + 660)
        self.assertEqual(code, 0)
        self.assertIn("at 100% and used up", out)
        self.assertNotIn("runs out in about 0m", out)


class Jitter(Base):
    def test_a_reset_time_that_moves_by_a_second_is_the_same_window(self):
        for i, p in enumerate([10.0, 12.0, 14.0, 16.0]):
            self.line(event(five=p, five_reset=T0 + 4 * H + (i == 3)), T0 + i * 300)  # the last one moved by 1 s
        f = lf.forecast(lf.load(), "five_hour", T0 + 900)
        self.assertAlmostEqual(f["rate_per_h"], 24.0, places=0)                       # 2 points / 5 min


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
