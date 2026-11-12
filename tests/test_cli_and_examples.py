"""Command-line behaviour: one-line errors without a traceback, and the shipped example."""
import os, subprocess, sys, tempfile, unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(ROOT, "lid_check.py")
EX = os.path.join(ROOT, "examples")


def run(*args, tz="UTC"):
    env = dict(os.environ, TZ=tz)
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env)


class Cli(unittest.TestCase):
    def test_bad_days(self):
        for bad in ("0", "-3"):
            r = run("--days", bad, "--pmset-log", os.path.join(EX, "pmset-sample.log"))
            self.assertEqual(r.returncode, 2)
            self.assertIn("--days must be greater than 0", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_missing_pmset_file(self):
        r = run("--pmset-log", "/nonexistent/pm.txt")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)

    def test_empty_pmset_log(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as f:
            r = run("--pmset-log", f.name)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no pmset log lines found", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_missing_claude_dir(self):
        r = run("--days", "3650", "--pmset-log", os.path.join(EX, "pmset-sample.log"),
                "--projects-dir", "/nonexistent/projects")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Claude Code folder not found", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_example_output(self):
        r = run("--days", "3650", "--pmset-log", os.path.join(EX, "pmset-sample.log"),
                "--projects-dir", os.path.join(EX, "claude-projects"), "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        import json
        p = json.loads(r.stdout)["periods"]
        self.assertEqual(len(p), 1)
        self.assertTrue(p[0]["lid_closed"])
        self.assertEqual((p[0]["darkwake"], p[0]["requests"], p[0]["cache_rewrite_tokens"],
                          p[0]["cache_rewrite_tokens_after_wake"]), (2, 3, 45000, 42000))


if __name__ == "__main__":
    unittest.main()
