# limit-forecast

See your Claude Code plan limit running out before it does.

> RU: «кончились лимиты» посреди работы — потому что процент виден, а темп нет. limit-forecast считает скорость расхода и показывает, когда при таком темпе будет 100% и когда сброс. А когда конец близко, говорит самому Claude: закоммить и запиши передачу дел, пока есть чем.

## What hurts

You're deep in a task, the limit hits, the session stops mid-change. The percentage was there all along. What wasn't there: *how fast* it was going, and whether you'd reach 100% before the window resets.

## What it does

Claude Code gives its status line the plan's rate limits (Pro and Max: 5-hour and 7-day, percent used and reset time). limit-forecast, used as the status line:

- records those numbers (a small file in `~/.claude/limit-forecast/`, last 6 hours, across all your sessions — the limit is per account);
- measures your pace: the 5-hour window over its last 45 minutes; the 7-day window over the whole week so far
  (nights and idle days included), and shows how many percent a day are left until it resets;
- shows when you hit 100% at that pace, next to when the window resets:

```
5h 63% +18%/h · window ends in 1h40, lasts | 7d 41% (20%/day left)
! 5h 88% +40%/h · window ends in 2h10, short by ~1h52 | 7d 52% (16%/day left)
! 5h 100% limit reached, reset in 50m | 7d 61% (13%/day left)
```

`!` means: at this pace you run out **before** the reset, or you already have (then it says only when it comes back).

As a `UserPromptSubmit` hook it reads the same file and, when the 5-hour limit is at 85% (7-day at 90%) or will run out within 20 minutes, adds a short note to Claude's context: commit what works and write a `HANDOFF.md` (done / in progress / next / where things are) before starting anything long, and tell you in one line. It says it once per level (again after 5 more points), stays quiet on data older than 10 minutes, and never blocks a prompt.

No tokens, no network, no credentials read: only what Claude Code already hands the status line. Standard library only, Python 3.8+.

## HANDOFF.md: so the work survives the limit

When the limit hits mid-task, the code is still on disk. What's lost is the *intent*: what you asked for, the plan, which step it was on. When the hook warns, it also writes `HANDOFF.md` in the project:

- **the last two requests** you made in the session, and **the plan** from the last TodoWrite (`[done]` / `[in progress]` / `[todo]`), read from the session's transcript;
- **the code state:** branch, last commit, uncommitted files as a **diffstat only** (the changed lines stay in git and never land in this file, because they can hold secrets), and untracked files;
- **the last test run** seen in the session and whether it failed (or the command you pass with `--test-cmd`);
- **how to continue:** `claude --continue` / `--resume` in the same folder after the reset, or hand the file to another tool or model.

`HANDOFF.md` is added to `.git/info/exclude`, so it never shows up in `git status`, and your `.gitignore` is left alone. Write one any time by hand:

```sh
python limit_forecast.py handoff                       # this folder, its latest Claude Code session
python limit_forecast.py handoff --dir ~/proj --test-cmd "npm test"
```

## Install

```sh
curl -O https://raw.githubusercontent.com/klavdiy-core/limit-forecast/main/limit_forecast.py
python limit_forecast.py install                     # ~/.claude/settings.json; or pass another settings file
```

`install` sets the status line and adds the hook, leaving everything else and a `.bak`. **If you already have a status line, it is kept**: limit-forecast runs it first (`--then "<your command>"`, same input, through bash like Claude Code does) and appends its own segment. On Windows that bash is Git Bash (`CLAUDE_CODE_GIT_BASH_PATH`, then PATH): the WSL `bash.exe` stubs in `System32` and `WindowsApps` are skipped, because inside WSL a Windows path doesn't exist and your own line would vanish (found by a teammate on a clean clone). Running `install` twice changes nothing.

By hand:

```json
{
  "statusLine": { "type": "command", "command": "python /path/limit_forecast.py statusline", "refreshInterval": 60 },
  "hooks": { "UserPromptSubmit": [ { "hooks": [ { "type": "command", "command": "python /path/limit_forecast.py hook" } ] } ] }
}
```

`python limit_forecast.py status` prints the current forecast in a terminal.

## Limits (said plainly)

- The rate limits only reach the status line for claude.ai **Pro and Max** subscribers, and only after the session's first response. With an API key you'll see `no plan limits yet`.
- The pace needs at least 5 minutes of samples in the current window. Samples arrive when the status line refreshes (on activity, and every 60 s with `refreshInterval`).
- The 5-hour forecast is a straight line through the last 45 minutes; a burst of agents changes it fast, that's what
  the `!` is for. The 7-day one uses the week's average, so one busy hour no longer flags the whole week.
- If the hook itself fails, the prompt still goes through and the reason lands in `errors.log` next to the samples.
- Built against the status-line and hook formats in the [Claude Code docs](https://code.claude.com/docs/en/statusline); tested with that input shape, not yet watched across a full 5-hour window in a live session. If your numbers look off, an issue with `python limit_forecast.py status` output helps.

## Test

```sh
python -m unittest discover -s tests
```

Pace and time-to-full from samples, "runs out before reset", a new window starting fresh, too little data, no `rate_limits` at all, keeping your own status line, pruning old samples, the hook staying quiet when fine, warning near the end, not nagging, ignoring stale data, surviving broken state files, and an install that keeps an existing status line. Handoff: a real temporary git repo and a hand-made transcript (no line from a real session): modified and untracked files, branch and commit, no changed lines leaking, the requests, the plan, the failing test, `.git/info/exclude`, a clean tree, a folder that isn't a repo, the hook writing it, and finding the folder's latest session. Choosing Git Bash over the WSL stubs. Every check here was broken on purpose to see a test go red.

## License

MIT
