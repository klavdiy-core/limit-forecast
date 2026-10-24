#!/usr/bin/env python3
"""limit-forecast: see your Claude Code plan limit running out before it does.

Claude Code hands its status line the plan's rate limits (5-hour and 7-day: percent used and reset time) for Pro
and Max subscribers. This script, used as the status line, records them, measures your pace and shows when you
will hit 100% at that pace, next to when the window resets:

    5h 63% +18%/h -> full ~2h03 (reset 1h40) | 7d 41%
    ! 5h 88% +40%/h -> full ~18m (reset 2h10) | 7d 52%        <- "!" : you run out before the reset

As a UserPromptSubmit hook it tells Claude itself when the end is near, so the work is committed and handed off
while there is still budget to do it. No tokens, no network: only the data Claude Code already gives the status
line. Standard library only.
"""
import json
import os
import shutil
import subprocess
import sys
import time

__version__ = "0.1.0"
KEEP_S = 6 * 3600          # samples kept
PACE_S = 45 * 60           # pace is measured over the last 45 minutes of the current window
MIN_SPAN_S = 5 * 60        # ... and needs at least 5 minutes of it
STALE_S = 10 * 60          # the hook ignores data older than this
WARN_PCT = {"five_hour": 85, "seven_day": 90}
WARN_MINUTES = 20
AGAIN_AFTER = 5            # warn again after 5 more points
NAMES = {"five_hour": ("5h", "5-hour"), "seven_day": ("7d", "7-day")}


def _dir():
    d = os.environ.get("LIMIT_FORECAST_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "limit-forecast")
    os.makedirs(d, exist_ok=True)
    return d


def load():
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


def record(now, limits):
    keep = [s for s in load() if now - s.get("t", 0) <= KEEP_S]
    for w, v in limits.items():
        if isinstance(v, dict) and v.get("used_percentage") is not None:
            keep.append({"t": now, "w": w, "p": float(v["used_percentage"]), "r": int(v.get("resets_at") or 0)})
    tmp = os.path.join(_dir(), "samples.jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for s in keep:
            f.write(json.dumps(s) + "\n")
    os.replace(tmp, os.path.join(_dir(), "samples.jsonl"))


def forecast(samples, window, now):
    mine = [s for s in samples if s.get("w") == window]
    if not mine:
        return None
    last = max(mine, key=lambda s: s["t"])
    pts = [(s["t"], s["p"]) for s in mine if s["r"] == last["r"] and now - s["t"] <= PACE_S]
    rate = None
    if len(pts) >= 2 and max(t for t, _ in pts) - min(t for t, _ in pts) >= MIN_SPAN_S:
        mt = sum(t for t, _ in pts) / len(pts)
        mp = sum(p for _, p in pts) / len(pts)
        var = sum((t - mt) ** 2 for t, _ in pts)
        slope = sum((t - mt) * (p - mp) for t, p in pts) / var if var else 0.0
        rate = slope * 3600 if slope > 0 else 0.0
    to_full = (100 - last["p"]) / rate * 60 if rate and rate > 0.05 else None
    to_reset = (last["r"] - now) / 60 if last["r"] else None
    return {
        "pct": last["p"], "resets_at": last["r"], "age_s": now - last["t"],
        "rate_per_h": rate, "minutes_to_full": to_full, "minutes_to_reset": to_reset,
        "runs_out_before_reset": bool(to_full is not None and to_reset is not None and to_full < to_reset),
    }


def _hm(minutes):
    m = max(0, int(round(minutes)))
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m}m"


def segment(window, f):
    short = NAMES[window][0]
    s = f"{short} {f['pct']:.0f}%"
    if f["rate_per_h"]:
        s += f" +{f['rate_per_h']:.0f}%/h"
    if f["minutes_to_full"] is not None and (window == "five_hour" or f["runs_out_before_reset"]):
        s += f" -> full ~{_hm(f['minutes_to_full'])}"
        if f["minutes_to_reset"] is not None:
            s += f" (reset {_hm(f['minutes_to_reset'])})"
    return s


# ---------- status line ----------

def statusline(stdin_text, now=None, argv=()):
    now = time.time() if now is None else now
    argv = list(argv)
    own = ""
    if "--then" in argv:
        cmd = argv[argv.index("--then") + 1]
        bash = shutil.which("bash")   # Claude Code runs status lines in bash (Git Bash on Windows), not cmd.exe
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
            with open(warned_p, encoding="utf-8") as f:
                warned = json.load(f)
        notes = []
        for w in ("five_hour", "seven_day"):
            f = forecast(samples, w, now)
            if not f or f["age_s"] > STALE_S:
                continue
            close = f["pct"] >= WARN_PCT[w] or (f["runs_out_before_reset"] and f["minutes_to_full"] <= WARN_MINUTES)
            if not close:
                continue
            last = warned.get(w)
            if last and last.get("r") == f["resets_at"] and f["pct"] < last.get("p", 0) + AGAIN_AFTER:
                continue
            warned[w] = {"r": f["resets_at"], "p": f["pct"]}
            pace = (f", and at the current pace it runs out in about {_hm(f['minutes_to_full'])}"
                    if f["minutes_to_full"] is not None else "")
            reset = f" (it resets in {_hm(f['minutes_to_reset'])})" if f["minutes_to_reset"] is not None else ""
            notes.append(f"the {NAMES[w][1]} limit is at {f['pct']:.0f}%{pace}{reset}")
        if not notes:
            return 0
        with open(warned_p, "w", encoding="utf-8") as fh:
            json.dump(warned, fh)
        print(f"[limit-forecast] Plan usage: {'; '.join(notes)}. Before starting anything long: commit what works, "
              "and write a short HANDOFF.md (done / in progress / next / where things are), so the work survives "
              "if the session stops at the limit. Tell the user about the limit in one line.")
    except Exception:  # noqa: BLE001 — a usage note is never worth blocking a prompt
        return 0
    return 0


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
        print(__doc__ + "\nusage: limit_forecast.py statusline [--then CMD] | hook | status | install [SETTINGS]")
        return 0
    if cmd == "--version":
        print(__version__)
        return 0
    if cmd == "statusline":
        return statusline(sys.stdin.read(), argv=argv[1:])
    if cmd == "hook":
        return hook(sys.stdin.read())
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
