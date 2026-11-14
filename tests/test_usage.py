"""--usage: token counts per session and subagent, from synthetic transcripts only."""
import json, os, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(ROOT, "lid_check.py")
sys.path.insert(0, ROOT)
import lid_check as L

UTC = timezone.utc
SID = "11111111-2222-3333-4444-555555555555"
SID2 = "99999999-8888-7777-6666-555555555555"
# One lid-close sleep 2026-11-10 13:00 .. 22:00 UTC (22:00 .. 07:00 JST)
PM = """PM ASL data store: /var/log/powermanagement
2026-11-10 22:00:00 +0900 Sleep               \tEntering Sleep state due to 'Clamshell Sleep':TCPKeepAlive=enabled Using Batt
2026-11-11 07:00:00 +0900 Wake                \tWake from Normal Sleep [CDNVA] : due to LID0 Using BATT
"""


def z(s):
    return s.replace(" ", "T") + ".500Z"


def asst(rid, when, inp=0, out=0, cw=0, cr=0, model="claude-opus-test", session=SID, side=None, agent=None, **extra):
    d = {"type": "assistant", "timestamp": z(when), "requestId": rid, "sessionId": session,
         "message": {"id": "msg_" + rid, "model": model,
                     "content": [{"type": "text", "text": "SECRET-CONVERSATION-TEXT"}],
                     "usage": {"input_tokens": inp, "output_tokens": out,
                               "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
                               "cache_creation": {"ephemeral_5m_input_tokens": 999999}}}}
    if side is not None:
        d["isSidechain"] = side
    if agent:
        d["agentId"] = agent
    d.update(extra)
    return json.dumps(d)


def write(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def make_tree(d):
    """Synthetic ~/.claude/projects with one main session, two subagents, and noise that must not count."""
    proj = os.path.join(d, "projects", "-Users-someone-secretproject")
    write(os.path.join(proj, SID + ".jsonl"), [
        json.dumps({"type": "user", "timestamp": z("2026-11-10 12:00:00"), "sessionId": SID,
                    "message": {"content": "SECRET-PROMPT /Users/someone/secret/path"}}),
        asst("r1", "2026-11-10 12:00:01", inp=10, out=5, cw=1000, cr=0),            # awake (21:00 JST)
        asst("r1", "2026-11-10 12:00:02", inp=10, out=50, cw=1000, cr=0),           # same request: max, counted once
        asst("r2", "2026-11-10 14:00:00", inp=1, out=100, cw=0, cr=20000),          # asleep, night JST
        asst("syn", "2026-11-10 14:00:01", model="<synthetic>"),                    # local placeholder: skipped
        asst("err", "2026-11-10 14:00:02", out=7, isApiErrorMessage=True),          # error entry: skipped
        asst("old", "2000-01-01 00:00:00", out=123456),                             # outside --days window
        # older format: a sidechain entry inside the main session file
        asst("sc1", "2026-11-10 23:00:00", inp=2, out=3, cw=0, cr=500, side=True),  # awake, 08:00 JST
        "not json at all",
    ])
    sub = os.path.join(proj, SID, "subagents")
    write(os.path.join(sub, "agent-aaa111.jsonl"), [
        asst("s1", "2026-11-10 15:00:00", inp=4, out=1000, cw=5000, cr=300000, model="claude-sonnet-test",
             side=True, agent="aaa111"),                                            # asleep, night JST
        asst("s2", "2026-11-10 23:30:00", inp=4, out=10, cw=0, cr=1000, model="claude-sonnet-test",
             side=True, agent="aaa111"),                                            # awake
        # nested timestamp far outside the window must not hide the line
        json.dumps({"type": "assistant", "timestamp": z("2026-11-10 23:31:00"), "requestId": "s3",
                    "sessionId": SID, "isSidechain": True, "agentId": "aaa111",
                    "message": {"model": "claude-sonnet-test", "content": [{"type": "tool_use", "input": {
                        "timestamp": "1999-01-01T00:00:00.000Z"}}],
                                "usage": {"input_tokens": 1, "output_tokens": 1}}}),
    ])
    with open(os.path.join(sub, "agent-aaa111.meta.json"), "w") as fh:
        json.dump({"agentType": "general-purpose", "description": "SECRET-TASK-DESCRIPTION"}, fh)
    write(os.path.join(sub, "workflows", "wf_x", "agent-bbb222.jsonl"), [
        asst("w1", "2026-11-10 16:00:00", out=20, cr=100, side=True, agent="bbb222"),  # asleep, no meta file
    ])
    # a second, smaller session in another project
    write(os.path.join(d, "projects", "-Users-someone-other", SID2 + ".jsonl"), [
        asst("x1", "2026-11-10 12:30:00", out=1, cr=10, session=SID2),
    ])
    return os.path.join(d, "projects")


def run(*args, home="/Users/someone"):
    env = dict(os.environ, TZ="Asia/Tokyo", HOME=home)
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env)


class Usage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.projects = make_tree(self.d)
        self.pm = os.path.join(self.d, "pm.txt")
        with open(self.pm, "w") as fh:
            fh.write(PM)

    def tearDown(self):
        self.tmp.cleanup()

    def json_run(self, *extra):
        r = run("--usage", "--days", "3650", "--json", "-q", "--projects-dir", self.projects,
                "--pmset-log", self.pm, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout, json.loads(r.stdout)

    def test_totals_dedupe_and_skips(self):
        _, rep = self.json_run()
        t = rep["totals"]
        # r1 (once, max) + r2 + sc1 + s1 + s2 + s3 + w1 + x1 = 8 requests
        self.assertEqual(t["requests"], 8)
        self.assertEqual(t["output"], 50 + 100 + 3 + 1000 + 10 + 1 + 20 + 1)
        self.assertEqual(t["input"], 10 + 1 + 2 + 4 + 4 + 1)
        self.assertEqual(t["cache_write"], 1000 + 5000)
        self.assertEqual(t["cache_read"], 20000 + 500 + 300000 + 1000 + 100 + 10)
        self.assertEqual(t["total"], sum(t[k] for k in ("input", "output", "cache_write", "cache_read")))
        self.assertEqual(rep["main_sessions"]["requests"], 3)          # r1, r2, x1
        self.assertEqual(rep["subagents"]["requests"], 5)              # sc1, s1, s2, s3, w1

    def test_asleep_and_night(self):
        _, rep = self.json_run()
        self.assertEqual(rep["meta"]["sleep_periods"], 1)
        self.assertEqual(rep["while_asleep"]["requests"], 3)           # r2, s1, w1
        self.assertEqual(rep["while_asleep"]["output"], 100 + 1000 + 20)
        self.assertEqual(rep["night_local_00_07"]["requests"], 2)      # s1 00:00 JST, w1 01:00 JST (r2 is 23:00)

    def test_sessions_and_subagents(self):
        _, rep = self.json_run()
        s = rep["top_sessions"]
        self.assertEqual([x["project"] for x in s], ["secretproject", "other"])
        self.assertEqual(s[0]["session"], SID[:8])
        self.assertEqual(s[0]["requests"], 7)
        self.assertEqual(s[0]["subagents"], 3)                         # aaa111, bbb222, old-style sidechain
        self.assertEqual(s[0]["models"], "opus-test +1")
        self.assertEqual(s[0]["first"][:16], "2026-11-10T21:00")       # local (JST)
        a = {x["agent"]: x for x in rep["top_subagents"]}
        self.assertEqual(a["aaa111"]["agent_type"], "general-purpose")
        self.assertEqual(a["aaa111"]["requests"], 3)
        self.assertEqual(a["bbb222"]["agent_type"], "workflow")
        self.assertEqual(a["sidechain"]["requests"], 1)
        self.assertEqual(rep["top_subagents"][0]["agent"], "aaa111")    # most tokens first

    def test_no_content_paths_or_home_leak(self):
        for extra in ((), ("--redact",)):
            raw_json, _ = self.json_run(*extra)
            r = run("--usage", "--days", "3650", "--projects-dir", self.projects, "--pmset-log", self.pm, *extra)
            self.assertEqual(r.returncode, 0, r.stderr)
            for raw in (raw_json, r.stdout):
                for bad in ("SECRET", "someone", self.d, "/Users", "description"):
                    self.assertNotIn(bad, raw)
        r = run("--usage", "--days", "3650", "--projects-dir", self.projects, "--pmset-log", self.pm)
        self.assertIn("secretproject", r.stdout)                         # folder name, home part removed

    def test_redact(self):
        raw, rep = self.json_run("--redact")
        self.assertNotIn("secretproject", raw)
        self.assertNotIn("other", json.dumps(rep["top_sessions"]))
        self.assertNotIn(SID[:8], raw)
        self.assertNotIn("aaa111", raw)
        self.assertEqual([x["project"] for x in rep["top_sessions"]], ["project-1", "project-2"])
        self.assertEqual(rep["top_sessions"][0]["session"], "s1")

    def test_table_and_top(self):
        r = run("--usage", "--days", "3650", "--top", "1", "--projects-dir", self.projects, "--pmset-log", self.pm)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertIn("While Mac asleep", out)
        self.assertIn("Local 00:00-07:00", out)
        self.assertNotIn("$", out)                                      # no dollar estimates
        sess = out.split("Top sessions")[1].split("Top subagents")[0]
        self.assertEqual(sess.count("secretproject"), 1)
        self.assertNotIn("other", sess)

    def test_no_pmset(self):
        r = run("--usage", "--days", "3650", "--no-pmset", "--projects-dir", self.projects)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("While Mac asleep: not shown", r.stdout)
        _, rep = self.json_run("--no-pmset")
        self.assertIsNone(rep["while_asleep"])

    def test_errors(self):
        r = run("--usage", "--no-pmset", "--projects-dir", "/nonexistent/projects")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Claude Code folder not found", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        r = run("--usage", "--no-pmset", "--top", "0", "--projects-dir", self.projects)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--top must be 1 or more", r.stderr)

    def test_window_filter_matches_full_parse(self):
        """The timestamp shortcut on raw lines must give the same requests as parsing every line."""
        since = datetime(2026, 11, 10, 13, 0, tzinfo=UTC)
        until = datetime(2026, 11, 10, 23, 30, 30, tzinfo=UTC)
        got = sorted((r["t"], r["output"]) for r in L.usage_requests(self.projects, since, until))
        want = []
        for root, _, fs in os.walk(self.projects):
            for f in fs:
                if not f.endswith(".jsonl"):
                    continue
                for ln in open(os.path.join(root, f)):
                    try:
                        d = json.loads(ln)
                    except ValueError:
                        continue
                    if d.get("type") != "assistant" or d.get("isApiErrorMessage") \
                            or d["message"].get("model") == "<synthetic>":
                        continue
                    t = L.iso(d["timestamp"])
                    if since <= t <= until:
                        want.append((t, d["message"]["usage"].get("output_tokens", 0)))
        self.assertEqual(got, sorted(want))
        self.assertEqual(len(got), 5)    # r2, s1, w1, sc1, s2 (23:30:00.5); s3 at 23:31 is out


class Labels(unittest.TestCase):
    def test_project_label(self):
        self.assertEqual(L.project_label("-Users-me-dev-app", "/Users/me"), "dev-app")
        self.assertEqual(L.project_label("-Users-me", "/Users/me"), "~")
        self.assertEqual(L.project_label("-Users-me2-x", "/Users/me"), "Users-me2-x")
        self.assertEqual(L.project_label("-Users-first-last-x", "/Users/first.last"), "x")
        self.assertEqual(L.project_label("-private-tmp-x", "/Users/me"), "private-tmp-x")

    def test_fmt_tok(self):
        self.assertEqual([L.fmt_tok(n) for n in (0, 9999, 12345, 2_500_000, 7_100_000_000)],
                         ["0", "9,999", "12.3k", "2.5M", "7.1B"])


if __name__ == "__main__":
    unittest.main()
