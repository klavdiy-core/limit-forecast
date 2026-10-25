#!/usr/bin/env python3
"""limit-forecast: see your Claude Code plan limit running out before it does.

Claude Code hands its status line the plan's rate limits (5-hour and 7-day: percent used and reset time) for Pro
and Max subscribers. This script, used as the status line, records them, measures your pace and shows when you
will hit 100% at that pace, next to when the window resets:

    5h 63% +18%/h · window ends in 1h40, lasts | 7d 41%
    ! 5h 88% +40%/h · window ends in 2h10, short by ~1h52 | 7d 52%   <- "!" : you run out before the reset

As a UserPromptSubmit hook it tells Claude itself when the end is near, so the work is committed and handed off
while there is still budget to do it. No tokens, no network: only the data Claude Code already gives the status
line. Standard library only.
"""
import json
import re
import os
import shutil
import subprocess
import sys
import threading
import time

__version__ = "0.4.1"
KEEP_S = 6 * 3600          # samples kept
PACE_S = 45 * 60           # pace is measured over the last 45 minutes of the current window
MIN_SPAN_S = 5 * 60        # ... and needs at least 5 minutes of it
STALE_S = 10 * 60          # the hook ignores data older than this
WARN_PCT = {"five_hour": 85, "seven_day": 90}
WARN_MINUTES = 20
AGAIN_AFTER = 5            # warn again after 5 more points
NAMES = {"five_hour": ("5h", "5-hour"), "seven_day": ("7d", "7-day")}
WEEK_S = 7 * 24 * 3600


def _dir():
    d = os.environ.get("LIMIT_FORECAST_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "limit-forecast")
    os.makedirs(d, exist_ok=True)
    return d


class _Lock:
    """An OS lock on a lasting file, around every read-modify-write of samples.jsonl. Status lines of several
    sessions record at once: 64 records from 8 threads kept 3 samples and 35 died on FileNotFoundError (one shared
    .tmp, no lock - external review, 25.10). The kernel drops the lock when its holder dies."""

    def __init__(self, wait_s=5.0):
        self.path, self.wait_s, self.f = os.path.join(_dir(), "samples.lock"), wait_s, None

    def __enter__(self):
        self.f = open(self.path, "a+b")
        end = time.time() + self.wait_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > end:
                    self.f.close()
                    raise TimeoutError("limit-forecast: samples are locked by another session")
                time.sleep(0.005)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        finally:
            self.f.close()


def _load():
    p = os.path.join(_dir(), "samples.jsonl")
    out = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def load():
    with _Lock():
        return _load()


def record(now, limits):
    with _Lock():                                       # the whole read-modify-write is one step
        keep = [s for s in _load() if now - s.get("t", 0) <= KEEP_S]
        for w, v in limits.items():
            if isinstance(v, dict) and v.get("used_percentage") is not None:
                keep.append({"t": now, "w": w, "p": float(v["used_percentage"]), "r": int(v.get("resets_at") or 0)})
        tmp = os.path.join(_dir(), f"samples.jsonl.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for s in keep:
                f.write(json.dumps(s) + "\n")
        os.replace(tmp, os.path.join(_dir(), "samples.jsonl"))


def _same_reset(a, b, slack_s=60):
    return a == b or (a is not None and b is not None and abs(a - b) <= slack_s)


def forecast(samples, window, now):
    mine = [s for s in samples if s.get("w") == window]
    if not mine:
        return None
    last = max(mine, key=lambda s: s["t"])
    # the same window: a reset time reported a second or two apart is still the same reset
    pts = [(s["t"], s["p"]) for s in mine if _same_reset(s["r"], last["r"]) and now - s["t"] <= PACE_S]
    rate = None
    if len(pts) >= 2 and max(t for t, _ in pts) - min(t for t, _ in pts) >= MIN_SPAN_S:
        mt = sum(t for t, _ in pts) / len(pts)
        mp = sum(p for _, p in pts) / len(pts)
        var = sum((t - mt) ** 2 for t, _ in pts)
        slope = sum((t - mt) * (p - mp) for t, p in pts) / var if var else 0.0
        rate = slope * 3600 if slope > 0 else 0.0
    per_day = None
    if window == "seven_day" and last["r"]:
        # the week is planned by its whole pace (nights and idle days included), not by the last 45 minutes:
        # a burst stretched over three days cried wolf after every busy hour
        elapsed_h = (now - (last["r"] - WEEK_S)) / 3600
        rate = last["p"] / elapsed_h if elapsed_h >= 1 else None
        days_left = (last["r"] - now) / 86400
        per_day = (100 - last["p"]) / days_left if days_left > 0 else None
    to_full = (100 - last["p"]) / rate * 60 if rate and rate > 0.05 else None
    to_reset = (last["r"] - now) / 60 if last["r"] else None
    full = last["p"] >= 100                     # already out: no pace, no "per day left", only when it comes back
    return {
        "pct": last["p"], "resets_at": last["r"], "age_s": now - last["t"], "full": full,
        "rate_per_h": rate, "minutes_to_full": to_full, "minutes_to_reset": to_reset, "per_day_left": per_day,
        "runs_out_before_reset": full or bool(to_full is not None and to_reset is not None and to_full < to_reset),
    }


def _hm(minutes):
    m = max(0, int(round(minutes)))
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m}m"


def segment(window, f):
    short = NAMES[window][0]
    if f.get("full"):
        reset = f" in {_hm(f['minutes_to_reset'])}" if f["minutes_to_reset"] is not None else ""
        return f"{short} 100% limit reached, reset{reset}"
    s = f"{short} {f['pct']:.0f}%"
    if window == "seven_day" and f.get("per_day_left") is not None:
        s += f" ({f['per_day_left']:.0f}%/day left)"          # what a week can be planned with
    elif f["rate_per_h"]:
        s += f" +{f['rate_per_h']:.0f}%/h"
    if window == "five_hour" and f["minutes_to_reset"] is not None:
        # the countdown of THIS window, always, and a plain verdict once there is a pace (a complaint, 25.10:
        # "how long until the window ends, and will I make it")
        s += f" · window ends in {_hm(f['minutes_to_reset'])}"
        if f["minutes_to_full"] is not None:
            gap = f["minutes_to_reset"] - f["minutes_to_full"]
            s += f", short by ~{_hm(gap)}" if gap > 0 else ", lasts"
    elif f["minutes_to_full"] is not None and (window == "five_hour" or f["runs_out_before_reset"]):
        s += f" -> full ~{_hm(f['minutes_to_full'])}"
        if f["minutes_to_reset"] is not None:
            s += f" (reset {_hm(f['minutes_to_reset'])})"
    return s


# ---------- status line ----------

GIT_BASH = (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe")


def find_bash(env=None, fallbacks=GIT_BASH):
    """The bash Claude Code itself uses. On Windows that is Git Bash, never the WSL stubs in System32 or
    WindowsApps: inside WSL a Windows path does not exist, and the user's own status line came back empty."""
    env = os.environ if env is None else env
    if os.name != "nt":
        return shutil.which("bash", path=env.get("PATH"))
    if env.get("CLAUDE_CODE_GIT_BASH_PATH"):
        return env["CLAUDE_CODE_GIT_BASH_PATH"]
    for d in (env.get("PATH") or "").split(os.pathsep):
        low = d.lower().replace("/", "\\").rstrip("\\")
        if low.endswith("\\windows\\system32") or low.endswith("\\windowsapps") or low.endswith("\\sysnative"):
            continue
        p = os.path.join(d, "bash.exe")
        if os.path.isfile(p):
            return p
    return next((p for p in fallbacks if os.path.isfile(p)), None)


def statusline(stdin_text, now=None, argv=()):
    now = time.time() if now is None else now
    argv = list(argv)
    own = ""
    if "--then" in argv:
        cmd = argv[argv.index("--then") + 1]
        bash = find_bash()   # Claude Code runs status lines in bash (Git Bash on Windows), not cmd.exe
        try:
            r = subprocess.run([bash, "-c", cmd] if bash else cmd, shell=not bash, input=stdin_text,
                               capture_output=True, text=True, timeout=5)
            own = r.stdout.rstrip("\n")
        except (OSError, subprocess.SubprocessError):
            own = ""
    try:
        event = json.loads(stdin_text or "{}")
        limits = event.get("rate_limits") or {}
        if not limits:
            seg = "no plan limits yet"
        else:
            record(now, limits)
            samples = load()
            parts, warn = [], False
            for w in ("five_hour", "seven_day"):
                f = forecast(samples, w, now)
                if f and w in limits:
                    parts.append(segment(w, f))
                    warn |= f["runs_out_before_reset"]
            seg = ("! " if warn else "") + " | ".join(parts)
    except Exception as e:  # a status line must never crash the UI
        seg = f"limit-forecast: {type(e).__name__}"
    print(f"{own}  {seg}" if own else seg)
    return 0


# ---------- hook ----------

def hook(stdin_text, now=None):
    """UserPromptSubmit: print a note for Claude when a limit is close. Never blocks, never fails loudly."""
    now = time.time() if now is None else now
    try:
        samples = load()
        warned_p = os.path.join(_dir(), "warned.json")
        warned = {}
        if os.path.exists(warned_p):
            try:
                with open(warned_p, encoding="utf-8") as f:
                    warned = json.load(f)
            except ValueError as e:
                # a torn file once meant no warning ever again (the whole hook failed); now it is logged and reset
                warned = {}
                try:
                    with open(os.path.join(_dir(), "errors.log"), "a", encoding="utf-8") as fl:
                        fl.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} hook: warned.json unreadable, reset: "
                                 f"{type(e).__name__}: {str(e)[:200]}\n")
                except OSError:
                    pass
        notes = []
        for w in ("five_hour", "seven_day"):
            f = forecast(samples, w, now)
            if not f or f["age_s"] > STALE_S:
                continue
            close = f["full"] or f["pct"] >= WARN_PCT[w] or (
                f["runs_out_before_reset"] and f["minutes_to_full"] is not None and f["minutes_to_full"] <= WARN_MINUTES)
            if not close:
                continue
            last = warned.get(w)
            if last and _same_reset(last.get("r"), f["resets_at"]) and f["pct"] < last.get("p", 0) + AGAIN_AFTER:
                continue
            warned[w] = {"r": f["resets_at"], "p": f["pct"]}
            pace = (" and used up" if f["full"] else
                    f", and at the current pace it runs out in about {_hm(f['minutes_to_full'])}"
                    if f["minutes_to_full"] is not None else "")
            reset = f" (it resets in {_hm(f['minutes_to_reset'])})" if f["minutes_to_reset"] is not None else ""
            notes.append(f"the {NAMES[w][1]} limit is at {f['pct']:.0f}%{pace}{reset}")
        if not notes:
            return 0
        tmp = f"{warned_p}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:              # written whole, then swapped in
            json.dump(warned, fh)
        with _Lock():
            os.replace(tmp, warned_p)
        event = json.loads(stdin_text or "{}")
        cwd = event.get("cwd") or os.getcwd()
        wrote = None
        try:
            wrote = write_handoff(cwd, event.get("transcript_path") or latest_transcript(cwd),
                                  status="Plan usage: " + "; ".join(notes))
        except Exception:  # noqa: BLE001 — the note still goes out without the file
            wrote = None
        done = (f" I already wrote {wrote} with the last request, the plan and the code state; add what it misses."
                if wrote else " Write a short HANDOFF.md (done / in progress / next / where things are).")
        print(f"[limit-forecast] Plan usage: {'; '.join(notes)}. Before starting anything long, commit what works."
              f"{done} Then the work survives if the session stops at the limit. Tell the user about the limit "
              "in one line.")
    except Exception as e:  # noqa: BLE001 — a usage note is never worth blocking a prompt, but it is logged
        try:
            with open(os.path.join(_dir(), "errors.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} hook: {type(e).__name__}: {str(e)[:200]}\n")
        except OSError:
            pass
        return 0
    return 0


# ---------- handoff ----------

TEST_CMD = re.compile(
    r"\b(pytest|py\.test|unittest|tox|jest|vitest|mocha|rspec|phpunit|ctest)\b|\b(npm|yarn|pnpm|bun)\s+(run\s+)?test\b"
    r"|\bgo\s+test\b|\bcargo\s+(test|nextest)\b|\b(mvn|gradlew?)\b.*\btest\b|\bdotnet\s+test\b|\bnode\s+--test\b"
    r"|\bmake\s+(check|test)\b|(^|[\s/\\])(test_\w+|\w+_test|\w+\.(test|spec))\.(py|[cm]?[jt]sx?|rb)\b", re.I)
FAILED_OUTPUT = re.compile(r"\bFAILED\b|\b[1-9]\d*\s+(failed|failing|errors?)\b|^FAIL\b|^not ok \d|\bpanicked\b"
                           r"|^Traceback \(most recent call last\)", re.M)
HANDOFF = "HANDOFF.md"


def project_slug(path):
    """The folder name Claude Code uses for a project's transcripts."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def latest_transcript(root, projects_dir=None):
    base = projects_dir or os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or
                                        os.path.join(os.path.expanduser("~"), ".claude"), "projects")
    d = os.path.join(base, project_slug(root))
    if not os.path.isdir(d):
        return None
    files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
    return max(files, key=os.path.getmtime) if files else None


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def read_intent(transcript):
    """Last user requests, the last TodoWrite plan, and the last test run, from a Claude Code transcript."""
    prompts, todos, uses, results = [], None, {}, {}
    last_test = None
    with open(transcript, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("message") or {}
            content = msg.get("content")
            if e.get("type") == "user":
                t = _text(content).strip()
                if t and not t.startswith("<"):            # skip command and system wrappers
                    prompts.append(t)
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        results[b.get("tool_use_id")] = (bool(b.get("is_error")), _text(b.get("content")) or
                                                         (b.get("content") if isinstance(b.get("content"), str) else ""))
            elif e.get("type") == "assistant" and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    inp = b.get("input") or {}
                    if b.get("name") == "TodoWrite" and isinstance(inp.get("todos"), list):
                        todos = inp["todos"]
                    if b.get("name") in ("Bash", "PowerShell") and TEST_CMD.search(str(inp.get("command", ""))):
                        last_test = (b.get("id"), inp.get("command"))
                        uses[b.get("id")] = inp.get("command")
    test = None
    if last_test:
        err, out = results.get(last_test[0], (False, None))
        if out is None:
            test = (last_test[1], "no result recorded")
        else:
            failed = err or bool(FAILED_OUTPUT.search(out))
            tail = " ".join(out.strip().splitlines()[-2:])[:200]
            test = (last_test[1], ("FAILED" if failed else "passed") + (f": {tail}" if tail else ""))
    return {"prompts": prompts[-2:], "todos": todos, "test": test}


def _git(root, *args):
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _exclude(root):
    git_dir = (_git(root, "rev-parse", "--git-dir") or "").strip()
    if not git_dir:
        return
    path = os.path.join(git_dir if os.path.isabs(git_dir) else os.path.join(root, git_dir), "info", "exclude")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    have = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if HANDOFF not in have.split():
        with open(path, "a", encoding="utf-8") as f:
            f.write(("" if have.endswith("\n") or not have else "\n") + HANDOFF + "\n")


def write_handoff(root, transcript=None, test_cmd=None, status=None):
    """Write HANDOFF.md in root: the request, the plan, the code state (diffstat, never the changed lines)."""
    root = os.path.abspath(root)
    intent = read_intent(transcript) if transcript and os.path.exists(transcript) else None
    out = [f"# HANDOFF", "", f"Written by limit-forecast at {time.strftime('%Y-%m-%d %H:%M')}"
           + (f". {status}" if status else "") + ".", "", "## What was being done", ""]
    if intent and intent["prompts"]:
        for label, p in zip(["Before that" if len(intent["prompts"]) > 1 else "Last request", "Last request"],
                            intent["prompts"]):
            out.append(f"**{label}:**")
            out += ["> " + l for l in p[:1500].splitlines()] + [""]
    else:
        out += ["(no transcript found; write the goal here in one line)", ""]
    if intent and intent["todos"]:
        out += ["## Plan (last TodoWrite)", ""]
        tag = {"completed": "done", "in_progress": "in progress", "pending": "todo"}
        out += [f"- [{tag.get(t.get('status'), t.get('status', '?'))}] {t.get('content', '')}" for t in intent["todos"]]
        out.append("")
    out += ["## Code state", ""]
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        out += ["This folder is not a git repository: nothing to list. Copy the folder or commit before switching tools.", ""]
    else:
        _exclude(root)
        branch = (_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "?").strip()
        head = (_git(root, "log", "-1", "--format=%h %s") or "(no commits yet)").strip()
        out += [f"Branch `{branch}`, last commit `{head}`.", ""]
        stat = (_git(root, "diff", "--stat", "HEAD") or "").rstrip()
        untracked = [u for u in (_git(root, "ls-files", "--others", "--exclude-standard") or "").splitlines() if u]
        if not stat and not untracked:
            out += ["No uncommitted changes.", ""]
        else:
            if stat:
                out += ["Uncommitted changes (diffstat only; the changed lines are in git, not here):", "", "```",
                        stat, "```", ""]
            if untracked:
                out += ["Untracked files:", ""] + [f"- {u}" for u in untracked[:50]] + [""]
    out += ["## Tests", ""]
    if test_cmd:
        out += [f"Run: `{test_cmd}`", ""]
    elif intent and intent["test"]:
        out += [f"Last run: `{intent['test'][0]}` -> {intent['test'][1]}", ""]
    else:
        out += ["(no test run seen; add the command with --test-cmd)", ""]
    out += ["## How to continue", "",
            "- Same tool after the reset: run `claude --continue` (or `claude --resume`) in this folder.",
            "- Another tool or model: give it this file and say \"continue from HANDOFF.md\".", ""]
    path = os.path.join(root, HANDOFF)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return path


# ---------- install ----------

def install(settings_path, script=None):
    script = (script or os.path.abspath(__file__)).replace("\\", "/")
    py = "python" if os.name == "nt" else "python3"
    data = {}
    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
        shutil.copy2(settings_path, settings_path + ".bak")
    sl = data.get("statusLine") or {}
    current = sl.get("command", "")
    if "limit_forecast.py" not in current:
        cmd = f'{py} "{script}" statusline'
        if current:
            cmd += ' --then "' + current.replace("\\", "\\\\").replace('"', '\\"') + '"'
        data["statusLine"] = {**sl, "type": "command", "command": cmd}
        data["statusLine"].setdefault("refreshInterval", 60)
    entries = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    if not any("limit_forecast.py" in c.get("command", "") for h in entries for c in h.get("hooks", [])):
        entries.append({"hooks": [{"type": "command", "command": f'{py} "{script}" hook'}]})
    os.makedirs(os.path.dirname(os.path.abspath(settings_path)), exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return settings_path


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "status"
    if cmd in ("-h", "--help"):
        print(__doc__ + "\nusage: limit_forecast.py statusline [--then CMD] | hook | status | install [SETTINGS]"
              "\n                         | handoff [--dir DIR] [--transcript JSONL] [--test-cmd CMD]")
        return 0
    if cmd == "--version":
        print(__version__)
        return 0
    if cmd == "statusline":
        return statusline(sys.stdin.read(), argv=argv[1:])
    if cmd == "hook":
        return hook(sys.stdin.read())
    if cmd == "handoff":
        opt = lambda k: argv[argv.index(k) + 1] if k in argv and argv.index(k) + 1 < len(argv) else None
        root = opt("--dir") or os.getcwd()
        transcript = opt("--transcript") or latest_transcript(root)
        print(f"wrote {write_handoff(root, transcript, test_cmd=opt('--test-cmd'))}"
              + ("" if transcript else " (no Claude Code session found for this folder: git state only)"))
        return 0
    if cmd == "install":
        path = argv[1] if len(argv) > 1 else os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
        print(f"installed into {install(path)} (a .bak was left next to it)")
        return 0
    if cmd == "status":
        now, samples = time.time(), load()
        for w in ("five_hour", "seven_day"):
            f = forecast(samples, w, now)
            print(segment(w, f) + (f"  (data {int(f['age_s'] // 60)} min old)" if f else "")
                  if f else f"{NAMES[w][0]}: no data yet (Claude Code must run with this as its status line)")
        return 0
    print(f"unknown command {cmd!r}; try --help", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
