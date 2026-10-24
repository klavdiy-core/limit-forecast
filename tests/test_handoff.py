import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import limit_forecast as lf  # noqa: E402

T0 = 1_800_000_000
SECRET = "API_KEY = 'sk-test-do-not-leak-0000'"   # lives in a diff hunk; must never reach HANDOFF.md


def git(root, *a):
    subprocess.run(["git", "-C", root, *a], check=True, capture_output=True)


def make_repo(root):
    git(root, "init", "-q", "-b", "feature-stream")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    for name in ("parser.py", "cli.py"):
        with open(os.path.join(root, name), "w") as f:
            f.write("x = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "start the streaming parser")
    with open(os.path.join(root, "parser.py"), "a") as f:
        f.write(SECRET + "\n")
    with open(os.path.join(root, "cli.py"), "a") as f:
        f.write("y = 2\n")
    with open(os.path.join(root, "notes_new.txt"), "w") as f:
        f.write("untracked\n")


def synthetic_transcript(path, failing=True):
    """Hand-made, in the shape Claude Code writes: no line copied from a real session."""
    lines = [
        {"type": "user", "message": {"role": "user", "content": "Refactor the parser so it streams rows instead of loading the whole file."}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {"todos": [
            {"content": "Split read_all into a generator", "status": "completed"},
            {"content": "Stream rows in cli.py", "status": "in_progress"},
            {"content": "Add a test for a 1 GB file", "status": "pending"}]}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Bash",
                                                        "input": {"command": "python -m pytest tests/test_parser.py -q"}}]}},
        {"type": "user", "message": {"content": [dict({"type": "tool_result", "tool_use_id": "t2",
                                                        "content": "1 failed, 4 passed" if failing else "5 passed"},
                                                       **({"is_error": True} if failing else {}))]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Keep going, and don't touch the CSV writer."}]}},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")


class Handoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        os.environ["LIMIT_FORECAST_DIR"] = os.path.join(self.tmp.name, "state")

    def tearDown(self):
        os.environ.pop("LIMIT_FORECAST_DIR", None)
        self.tmp.cleanup()

    def read(self, name="HANDOFF.md"):
        with open(os.path.join(self.root, name), encoding="utf-8") as f:
            return f.read()

    def test_git_state_without_hunks(self):
        make_repo(self.root)
        lf.write_handoff(self.root, transcript=None)
        h = self.read()
        for want in ("feature-stream", "parser.py", "cli.py", "notes_new.txt", "start the streaming parser"):
            self.assertIn(want, h)
        head = subprocess.run(["git", "-C", self.root, "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        self.assertIn(head, h)
        self.assertNotIn("sk-test-do-not-leak", h)        # diffstat only, never the changed lines

    def test_intent_from_the_transcript(self):
        make_repo(self.root)
        t = os.path.join(self.tmp.name, "s.jsonl")
        synthetic_transcript(t)
        lf.write_handoff(self.root, transcript=t)
        h = self.read()
        self.assertIn("Keep going, and don't touch the CSV writer.", h)        # the last request
        self.assertIn("Refactor the parser so it streams rows", h)            # and the one before it
        self.assertIn("[in progress] Stream rows in cli.py", h)
        self.assertIn("[done] Split read_all into a generator", h)
        self.assertIn("[todo] Add a test for a 1 GB file", h)
        self.assertIn("python -m pytest tests/test_parser.py -q", h)
        self.assertIn("FAILED", h)

    def test_test_cmd_flag_wins(self):
        make_repo(self.root)
        lf.write_handoff(self.root, transcript=None, test_cmd="npm test -- parser")
        self.assertIn("npm test -- parser", self.read())

    def test_clean_tree(self):
        make_repo(self.root)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "done")
        lf.write_handoff(self.root, transcript=None)
        self.assertIn("No uncommitted changes", self.read())

    def test_kept_out_of_git_without_touching_gitignore(self):
        make_repo(self.root)
        lf.write_handoff(self.root, transcript=None)
        lf.write_handoff(self.root, transcript=None)
        with open(os.path.join(self.root, ".git", "info", "exclude")) as f:
            self.assertEqual(f.read().count("HANDOFF.md"), 1)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".gitignore")))
        st = subprocess.run(["git", "-C", self.root, "status", "--porcelain"], capture_output=True, text=True).stdout
        self.assertNotIn("HANDOFF.md", st)

    def test_not_a_git_repo_still_writes_the_intent(self):
        t = os.path.join(self.tmp.name, "s.jsonl")
        synthetic_transcript(t)
        lf.write_handoff(self.root, transcript=t)
        h = self.read()
        self.assertIn("not a git repository", h)
        self.assertIn("Stream rows in cli.py", h)

    def test_hook_writes_the_handoff_when_it_warns(self):
        make_repo(self.root)
        t = os.path.join(self.tmp.name, "s.jsonl")
        synthetic_transcript(t)
        lf.record(T0, {"five_hour": {"used_percentage": 93, "resets_at": T0 + 3600}})
        out = io.StringIO()
        with redirect_stdout(out):
            lf.hook(json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "next",
                                "cwd": self.root, "transcript_path": t}), now=T0 + 5)
        self.assertIn("HANDOFF.md", out.getvalue())
        self.assertIn("Stream rows in cli.py", self.read())

    def test_finds_the_latest_session_of_this_folder(self):
        make_repo(self.root)
        projects = os.path.join(self.tmp.name, "claude_projects")
        slug = lf.project_slug(self.root)
        os.makedirs(os.path.join(projects, slug))
        old, new = os.path.join(projects, slug, "a.jsonl"), os.path.join(projects, slug, "b.jsonl")
        synthetic_transcript(old, failing=False)
        synthetic_transcript(new)
        os.utime(old, (T0 - 100, T0 - 100))
        os.utime(new, (T0, T0))
        self.assertEqual(lf.latest_transcript(self.root, projects_dir=projects), new)


if __name__ == "__main__":
    unittest.main()
