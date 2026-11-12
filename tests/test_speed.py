"""The speed shortcuts must not change results: skipped files, skipped lines, quiet progress on stderr."""
import json, os, subprocess, sys, tempfile, time, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
import lid_check as L

UTC = timezone.utc
T0 = datetime(2026, 11, 10, 23, 0, 0, tzinfo=UTC)           # one sleep period: T0 .. T0+8h
SPANS = [(T0, T0 + timedelta(hours=8))]
SINCE, UNTIL = T0, T0 + timedelta(days=2)


def z(dt, ms=True):
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + (".123Z" if ms else "Z")


def line(rid, dt, cw=0, cr=0, style=0, typ="assistant", extra=""):
    usage = {"cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}
    d = {"type": typ, "timestamp": z(dt), "requestId": rid, "message": {"id": "m" + rid, "usage": usage}}
    if style == 1:      # timestamp first, type last, spaces everywhere
        d = {"timestamp": d["timestamp"], "message": d["message"], "requestId": rid, "type": typ}
        return json.dumps(d, indent=None, separators=(" , ", " : ")).replace("}", "}") + extra
    if style == 2:      # pretty-keys with a nested "timestamp" key that looks far outside the period
        d["message"]["content"] = [{"type": "tool_use", "input": {"timestamp": "2020-01-01T00:00:00.000Z"}}]
    if style == 3:      # offset timestamp instead of Z
        d["timestamp"] = dt.astimezone(timezone(timedelta(hours=9))).isoformat()
    return json.dumps(d) + extra


def write(path, lines, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def canon(reqs, errors):
    L.mark_rewrites(reqs)
    key = lambda r: (r["t"], r["cw"], r["cr"], r["chain"])
    real = [r for r in reqs if r["t"] >= SPANS[0][0] - timedelta(minutes=30)]   # ghosts sit far outside
    return sorted((r["t"], r["cw"], r["cr"], r["rewrite"]) for r in real
                  if r["t"] < SPANS[0][1] + timedelta(minutes=30)), sorted(errors)


class SpeedTests(unittest.TestCase):
    def both(self, root):
        full = canon(*L.claude_requests(root, SINCE, UNTIL))
        fast = canon(*L.claude_requests(root, SINCE, UNTIL, SPANS))
        return full, fast

    def test_old_mtime_file_not_opened_but_window_file_read(self):
        with tempfile.TemporaryDirectory() as d:
            old, new = os.path.join(d, "p", "old.jsonl"), os.path.join(d, "p", "new.jsonl")
            write(old, [line("a", T0 + timedelta(hours=1), cw=5)], mtime=time.time() - 40 * 86400)
            write(new, [line("b", T0 + timedelta(hours=1), cw=7)])
            opened = []
            real_open = open

            def spy(p, *a, **k):
                opened.append(os.path.basename(str(p)))
                return real_open(p, *a, **k)
            since = datetime.fromtimestamp(time.time(), UTC) - timedelta(days=1)
            spans = [(since, since + timedelta(hours=1))]
            with mock.patch.object(L, "open", spy, create=True):
                reqs, _ = L.claude_requests(d, since, since + timedelta(days=1), spans)
            self.assertIn("new.jsonl", opened)
            self.assertNotIn("old.jsonl", opened)
            # a file modified inside the window is always read, even if its lines are old
            os.utime(old, None)
            opened.clear()
            with mock.patch.object(L, "open", spy, create=True):
                L.claude_requests(d, since, since + timedelta(days=1), spans)
            self.assertIn("old.jsonl", opened)

    def test_line_filter_matches_full_parse(self):
        with tempfile.TemporaryDirectory() as d:
            big = "x" * 3_000_000
            lines = [
                line("r0", T0 - timedelta(hours=3), cw=30000, style=2),           # far before: skipped, ghost
                line("r0b", T0 - timedelta(hours=2), cw=1, style=1),
                line("big", T0 - timedelta(hours=1), cw=2, extra=""),
                json.dumps({"type": "user", "timestamp": z(T0 + timedelta(hours=2)), "text": "assistant " + big}),
                line("p1", T0 + timedelta(minutes=30), cw=100, cr=50000, style=0),
                line("p2", T0 + timedelta(hours=2), cw=40000, cr=0, style=1),     # rewrite candidate, spaced JSON
                line("p3", T0 + timedelta(hours=3), cw=40000, cr=0, style=2),     # nested far timestamp
                line("p4", T0 + timedelta(hours=4), cw=40000, cr=0, style=3),     # +09:00 offset, no Z
                json.dumps({"type": "system", "subtype": "api_error", "timestamp": z(T0 + timedelta(hours=5))}),
                json.dumps({"type": "assistant", "isApiErrorMessage": True, "timestamp": z(T0 + timedelta(hours=6))}),
                line("p1", T0 + timedelta(minutes=30, seconds=5), cw=100, cr=50000),   # duplicate id
                line("w1", T0 + timedelta(hours=8, minutes=3), cw=45000, cr=0),         # after wake
                line("far", T0 + timedelta(hours=30), cw=9, cr=0),                      # outside the period
                "{not json but says assistant",
            ]
            write(os.path.join(d, "a", "s.jsonl"), lines)
            write(os.path.join(d, "b", "t.jsonl"), [line("q", T0 + timedelta(hours=1), cw=50000), line("q2", T0 + timedelta(hours=1, minutes=1), cw=3)])
            write(os.path.join(d, "c", "late.jsonl"), ["{}", line("l1", T0 + timedelta(days=5), cw=9),
                                                       line("l2", T0 + timedelta(days=5, seconds=9), cw=9)])
            full, fast = self.both(d)
            self.assertEqual(full, fast)
            self.assertTrue(any(r[3] for r in fast[0]))      # rewrites really exist in this fixture
            self.assertEqual(len(fast[1]), 2)

    def test_first_request_rule_survives_skipped_history(self):
        # p is the 2nd request of its file (a earlier one lies outside the period): it counts as a rewrite.
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "a", "s.jsonl"), [line("a", T0 - timedelta(hours=5), cw=1),
                                                    line("p", T0 + timedelta(hours=2), cw=40000, cr=0)])
            full, fast = self.both(d)
            self.assertEqual(full, fast)
            self.assertEqual([r[3] for r in fast[0]], [40000])


class Progress(unittest.TestCase):
    def run_cli(self, *extra):
        ex = os.path.join(ROOT, "examples")
        return subprocess.run([sys.executable, os.path.join(ROOT, "lid_check.py"), "--days", "3650", "--json",
                               "--pmset-log", os.path.join(ex, "pmset-sample.log"),
                               "--projects-dir", os.path.join(ex, "claude-projects"), *extra],
                              capture_output=True, text=True, env=dict(os.environ, TZ="UTC"))

    def test_stderr_progress_keeps_stdout_clean(self):
        loud, quiet = self.run_cli(), self.run_cli("--quiet")
        self.assertEqual(loud.returncode, 0, loud.stderr)
        self.assertEqual(loud.stdout, quiet.stdout)
        json.loads(loud.stdout)
        self.assertIn("Scanning", loud.stderr)
        self.assertEqual(len(loud.stderr.strip().splitlines()), 2)    # not a terminal: start and end line only
        self.assertNotIn("\r", loud.stderr)
        self.assertEqual(quiet.stderr, "")

    def test_table_mode_stdout_has_no_progress(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "lid_check.py"), "--days", "3650",
                            "--pmset-log", os.path.join(ROOT, "examples", "pmset-sample.log"),
                            "--projects-dir", os.path.join(ROOT, "examples", "claude-projects")],
                           capture_output=True, text=True, env=dict(os.environ, TZ="UTC"))
        self.assertNotIn("Scanning", r.stdout)
        self.assertTrue(r.stdout.startswith("lid-check: last 3650 days"))


if __name__ == "__main__":
    unittest.main()
