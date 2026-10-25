import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import limit_forecast as lf  # noqa: E402

H = 3600
T0 = 1_800_000_000          # a fixed "now" for the tests


def event(five=None, seven=None, five_reset=T0 + 2 * H, seven_reset=T0 + 5 * 24 * H, model="Opus"):
    rl = {}
    if five is not None:
        rl["five_hour"] = {"used_percentage": five, "resets_at": five_reset}
    if seven is not None:
        rl["seven_day"] = {"used_percentage": seven, "resets_at": seven_reset}
    e = {"model": {"display_name": model}, "context_window": {"used_percentage": 10}}
    if rl:
        e["rate_limits"] = rl
    return json.dumps(e)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LIMIT_FORECAST_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("LIMIT_FORECAST_DIR", None)
        self.tmp.cleanup()

    def line(self, stdin, now, argv=()):
        out = io.StringIO()
        with redirect_stdout(out):
            lf.statusline(stdin, now=now, argv=list(argv))
        return out.getvalue().strip()

    def hook(self, now, prompt="go on"):
        out = io.StringIO()
        with redirect_stdout(out):
            code = lf.hook(json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt}), now=now)
        return code, out.getvalue().strip()


class Forecast(Base):
    def test_pace_and_time_to_full(self):
        # 40% -> 55% over 30 minutes = 30 %/h; 45% left -> 1.5 h, but the window resets in 1 h: it lasts
        r = T0 + H
        self.line(event(five=40, seven=10, five_reset=r), T0 - 30 * 60)
        self.line(event(five=48, seven=11, five_reset=r), T0 - 15 * 60)
        s = self.line(event(five=55, seven=12, five_reset=r), T0)
        f = lf.forecast(lf.load(), "five_hour", now=T0)
        self.assertAlmostEqual(f["rate_per_h"], 30, delta=0.5)
        self.assertAlmostEqual(f["minutes_to_full"], 90, delta=2)
        self.assertFalse(f["runs_out_before_reset"])
        self.assertIn("5h 55%", s)
        self.assertIn("+30%/h", s)

    def test_runs_out_before_reset_is_flagged(self):
        self.line(event(five=60, five_reset=T0 + 3 * H), T0 - 20 * 60)
        s = self.line(event(five=80, five_reset=T0 + 3 * H), T0)   # 60 %/h, 20% left -> 20 min, reset in 3 h
        f = lf.forecast(lf.load(), "five_hour", now=T0)
        self.assertTrue(f["runs_out_before_reset"])
        self.assertAlmostEqual(f["minutes_to_full"], 20, delta=1)
        self.assertTrue(s.startswith("!"), s)

    def test_the_5h_window_counts_down_and_says_if_it_lasts(self):
        # a complaint (x.com/captainsolana, 25.10): "how long until THIS window ends, and will I make it" - the end
        # was shown only next to a time-to-full, and never as a yes/no
        s = self.line(event(five=30, seven=10, five_reset=T0 + H + 40 * 60), T0)   # no pace yet: still a countdown
        self.assertIn("window ends in 1h40", s)

    def test_a_pace_that_outruns_the_window_says_by_how_much(self):
        self.line(event(five=40, five_reset=T0 + 2 * H), T0 - 30 * 60)
        s = self.line(event(five=55, five_reset=T0 + 2 * H), T0)             # 30 %/h, 45 % left = 1h30 < 2h
        self.assertIn("window ends in 2h00", s)
        self.assertIn("short by ~30m", s)

    def test_a_pace_that_fits_says_it_lasts(self):
        self.line(event(five=40, five_reset=T0 + H), T0 - 30 * 60)
        s = self.line(event(five=55, five_reset=T0 + H), T0)                  # 1h30 to full > 1h left: it lasts
        self.assertIn("window ends in 1h00, lasts", s)

    def test_new_window_starts_fresh(self):
        self.line(event(five=95, five_reset=T0 - 60), T0 - 30 * 60)      # old window
        self.line(event(five=2, five_reset=T0 + 5 * H), T0 - 10 * 60)    # new one
        self.line(event(five=4, five_reset=T0 + 5 * H), T0)
        f = lf.forecast(lf.load(), "five_hour", now=T0)
        self.assertAlmostEqual(f["rate_per_h"], 12, delta=0.5)

    def test_too_little_data_gives_no_pace(self):
        s = self.line(event(five=30), T0)
        self.assertIsNone(lf.forecast(lf.load(), "five_hour", now=T0)["rate_per_h"])
        self.assertIn("5h 30%", s)

    def test_no_rate_limits_in_input(self):
        # API-key users and the first moments of a session have no rate_limits: say so, record nothing
        s = self.line(event(), T0)
        self.assertIn("no plan limits yet", s)
        self.assertEqual(lf.load(), [])

    def test_then_keeps_your_own_status_line(self):
        cmd = f'"{sys.executable}" -c "import sys; sys.stdin.read(); print(\'my line\')"'
        s = self.line(event(five=20), T0, argv=["--then", cmd])
        self.assertTrue(s.startswith("my line"), s)
        self.assertIn("5h 20%", s)

    def test_never_picks_the_wsl_bash_stub(self):
        # Windows ships bash.exe stubs for WSL in System32 and WindowsApps; Claude Code uses Git Bash.
        # With a WSL stub first on PATH, a Windows path inside --then does not exist and the user's line vanished.
        if os.name != "nt":
            self.skipTest("Windows only")
        base = self.tmp.name
        wsl = os.path.join(base, "Windows", "System32")
        apps = os.path.join(base, "AppData", "Local", "Microsoft", "WindowsApps")
        git = os.path.join(base, "Git", "usr", "bin")
        for d in (wsl, apps, git):
            os.makedirs(d)
            open(os.path.join(d, "bash.exe"), "w").close()
        path = os.pathsep.join([wsl, apps, git])
        self.assertEqual(lf.find_bash(env={"PATH": path}).lower(), os.path.join(git, "bash.exe").lower())
        forced = os.path.join(base, "custom", "bash.exe")
        self.assertEqual(lf.find_bash(env={"PATH": path, "CLAUDE_CODE_GIT_BASH_PATH": forced}), forced)
        self.assertIsNone(lf.find_bash(env={"PATH": os.pathsep.join([wsl, apps])}, fallbacks=()))

    def test_old_samples_are_dropped(self):
        for k in range(300):
            self.line(event(five=1 + k * 0.01, five_reset=T0 + 9 * 24 * H), T0 - 30 * H + k * 60)
        self.line(event(five=5, five_reset=T0 + 9 * 24 * H), T0)
        self.assertTrue(all(T0 - x["t"] <= lf.KEEP_S for x in lf.load()))


class Hook(Base):
    def test_quiet_when_fine(self):
        self.line(event(five=20), T0 - 600)
        self.line(event(five=22), T0)
        self.assertEqual(self.hook(T0), (0, ""))

    def test_warns_near_the_end_and_names_the_way_out(self):
        self.line(event(five=60, five_reset=T0 + 3 * H), T0 - 20 * 60)
        self.line(event(five=80, five_reset=T0 + 3 * H), T0)
        code, out = self.hook(T0 + 30)
        self.assertEqual(code, 0)
        self.assertIn("5-hour", out)
        self.assertIn("HANDOFF", out)
        self.assertIn("commit", out.lower())

    def test_warns_at_high_percent_even_without_a_pace(self):
        self.line(event(five=91), T0)
        self.assertIn("91%", self.hook(T0 + 5)[1])

    def test_does_not_nag_every_prompt(self):
        self.line(event(five=91), T0)
        self.assertNotEqual(self.hook(T0 + 5)[1], "")
        self.assertEqual(self.hook(T0 + 60)[1], "")              # same level: quiet
        self.line(event(five=97), T0 + 120)
        self.assertNotEqual(self.hook(T0 + 125)[1], "")         # 6 points higher: again

    def test_stale_data_says_nothing(self):
        self.line(event(five=95), T0)
        self.assertEqual(self.hook(T0 + 20 * 60)[1], "")

    def test_broken_state_never_blocks_the_prompt(self):
        with open(os.path.join(self.tmp.name, "samples.jsonl"), "w") as f:
            f.write("{not json\n")
        self.assertEqual(self.hook(T0)[0], 0)
        self.line(event(five=95), T0)
        with open(os.path.join(self.tmp.name, "warned.json"), "w") as f:
            f.write("[[[")
        self.assertEqual(self.hook(T0 + 5)[0], 0)


class Install(Base):
    def test_install_merges_and_keeps_an_existing_status_line(self):
        root = os.path.join(self.tmp.name, "home")
        os.makedirs(root)
        settings = os.path.join(root, "settings.json")
        with open(settings, "w") as f:
            json.dump({"statusLine": {"type": "command", "command": "my-status.sh"}, "model": "x"}, f)
        lf.install(settings, script="/opt/limit_forecast.py")
        lf.install(settings, script="/opt/limit_forecast.py")
        s = json.load(open(settings))
        self.assertEqual(s["model"], "x")
        self.assertIn("--then", s["statusLine"]["command"])
        self.assertIn("my-status.sh", s["statusLine"]["command"])
        self.assertEqual(s["statusLine"]["command"].count("limit_forecast.py"), 1)
        hooks = [c["command"] for h in s["hooks"]["UserPromptSubmit"] for c in h["hooks"]]
        self.assertEqual(sum("limit_forecast.py" in c for c in hooks), 1)
        self.assertTrue(os.path.exists(settings + ".bak"))


if __name__ == "__main__":
    unittest.main()
