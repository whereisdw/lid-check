"""--away: sessions that ran while nobody typed, restarts after a usage limit, tokens.
Synthetic fake home only (Claude Code transcripts + Codex rollouts); the real ~/.claude and ~/.codex are never read."""
import json, os, subprocess, sys, tempfile, unittest
from datetime import datetime, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(ROOT, "lid_check.py")
sys.path.insert(0, ROOT)
import lid_check as L

UTC = timezone.utc
NOW = "2026-11-11T09:00:00Z"
S_NIGHT = "aaaaaaaa-1111-2222-3333-444444444444"     # interactive, ran overnight, hit a limit, auto-continued
S_TYPED = "bbbbbbbb-1111-2222-3333-444444444444"     # hit a limit, you typed, then it went on: not a restart
S_HEAD = "cccccccc-1111-2222-3333-444444444444"      # claude -p run at night
S_OLD = "dddddddd-1111-2222-3333-444444444444"       # older transcript format (no origin field)
CX = "019a0000-0000-7000-8000-00000000c0de"          # Codex interactive session
CX_EXEC = "019b0000-0000-7000-8000-0000000e0ec0"     # codex exec


def z(s):
    return s.replace(" ", "T") + ".000Z"


def user(when, text, session, origin=None, entry="cli", **extra):
    d = {"type": "user", "timestamp": z(when), "sessionId": session, "entrypoint": entry,
         "message": {"role": "user", "content": text}}
    if origin is not None:
        d["origin"] = origin
    d.update(extra)
    return json.dumps(d)


def tool_result(when, session):
    return json.dumps({"type": "user", "timestamp": z(when), "sessionId": session, "entrypoint": "cli",
                       "message": {"role": "user", "content": [{"type": "tool_result", "content": "SECRET-TOOL-OUTPUT"}]},
                       "toolUseResult": {"stdout": "SECRET"}})


def asst(rid, when, session, inp=10, out=20, cw=0, cr=1000, entry="cli", **extra):
    d = {"type": "assistant", "timestamp": z(when), "requestId": rid, "sessionId": session, "entrypoint": entry,
         "message": {"id": "msg_" + rid, "model": "claude-test",
                     "content": [{"type": "text", "text": "SECRET-ANSWER"}],
                     "usage": {"input_tokens": inp, "output_tokens": out,
                               "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}}}
    d.update(extra)
    return json.dumps(d)


def limit(when, session, text):
    return json.dumps({"type": "assistant", "timestamp": z(when), "sessionId": session, "entrypoint": "cli",
                       "isApiErrorMessage": True, "error": "rate_limit",
                       "message": {"id": "e", "model": "<synthetic>", "content": [{"type": "text", "text": text}],
                                   "usage": {"input_tokens": 0, "output_tokens": 0}}})


def cx(when, ptype, **payload):
    return json.dumps({"timestamp": z(when), "type": "event_msg", "payload": dict(type=ptype, **payload)})


R1 = int(datetime(2026, 11, 11, 5, 0, tzinfo=UTC).timestamp())     # 5h window that ends 05:00 UTC
R2 = int(datetime(2026, 11, 11, 10, 0, tzinfo=UTC).timestamp())


def tok(when, total, last, pct, resets=R1, reached=None):
    return cx(when, "token_count",
              info={"total_token_usage": {"total_tokens": total},
                    "last_token_usage": {"input_tokens": last - 5, "output_tokens": 5, "total_tokens": last}},
              rate_limits={"primary": {"used_percent": pct, "window_minutes": 300, "resets_at": resets},
                           "rate_limit_reached_type": reached})


def umsg(when, text):
    return cx(when, "item_completed", item={"type": "UserMessage", "content": [{"type": "text", "text": text}]})


def write(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


HUMAN = {"kind": "human"}


def make_home(home):
    proj = os.path.join(home, ".claude", "projects", "-Users-someone-secretproject")
    write(os.path.join(proj, S_NIGHT + ".jsonl"), [
        user("2026-11-10 21:00:00", "SECRET-PROMPT please refactor", S_NIGHT, HUMAN),
        asst("n1", "2026-11-10 21:00:10", S_NIGHT),
        tool_result("2026-11-10 21:00:20", S_NIGHT),
        asst("n2", "2026-11-10 23:30:00", S_NIGHT, cr=5000),
        asst("n2", "2026-11-10 23:30:01", S_NIGHT, cr=5000, out=40),          # same request id: counted once
        user("2026-11-10 23:40:00", "<task-notification>SECRET</task-notification>", S_NIGHT,
             {"kind": "task-notification", "producer": "session-task"}),
        asst("n3", "2026-11-10 23:45:00", S_NIGHT),
        limit("2026-11-11 01:00:00", S_NIGHT, "You've hit your limit · resets 3am (UTC)"),
        user("2026-11-11 03:00:30", "Your claude.ai usage limit has reset. Continue.", S_NIGHT),
        asst("n4", "2026-11-11 03:00:40", S_NIGHT, cw=20000, cr=0),
        asst("n5", "2026-11-11 03:10:00", S_NIGHT),
        user("2026-11-11 08:00:00", "SECRET-PROMPT good morning", S_NIGHT, HUMAN),
        asst("n6", "2026-11-11 08:00:10", S_NIGHT),
    ])
    sub = os.path.join(proj, S_NIGHT, "subagents", "agent-a1.jsonl")
    write(sub, [
        user("2026-11-11 02:00:00", "SECRET-SUBAGENT-TASK", S_NIGHT),                 # from the parent: not typed
        asst("sa1", "2026-11-11 00:30:00", S_NIGHT, isSidechain=True, agentId="a1"),
    ])
    write(os.path.join(proj, S_TYPED + ".jsonl"), [
        user("2026-11-10 20:00:00", "SECRET", S_TYPED, HUMAN, entry="claude-desktop"),
        limit("2026-11-10 20:05:00", S_TYPED, "You've hit your limit · resets 11pm (UTC)"),
        user("2026-11-10 23:10:00", "SECRET continue", S_TYPED, HUMAN, entry="claude-desktop"),
        asst("t1", "2026-11-10 23:10:05", S_TYPED, entry="claude-desktop"),
    ])
    write(os.path.join(proj, S_HEAD + ".jsonl"), [
        user("2026-11-11 04:00:00", "SECRET-SCRIPT-PROMPT", S_HEAD, entry="sdk-cli"),
        asst("h1", "2026-11-11 04:00:05", S_HEAD, entry="sdk-cli"),
        asst("h2", "2026-11-11 04:01:00", S_HEAD, entry="sdk-cli"),
    ])
    old = os.path.join(home, ".claude", "projects", "-Users-someone-other")
    write(os.path.join(old, S_OLD + ".jsonl"), [
        json.dumps({"type": "user", "timestamp": z("2026-11-10 19:00:00"), "sessionId": S_OLD,
                    "message": {"role": "user", "content": "SECRET old-format prompt"}}),
        asst("o1", "2026-11-10 19:00:05", S_OLD),
        "not json",
    ])
    cdir = os.path.join(home, ".codex", "sessions", "2026", "11", "10")
    write(os.path.join(cdir, f"rollout-2026-11-10T20-00-00-{CX}.jsonl"), [
        json.dumps({"timestamp": z("2026-11-10 20:00:00"), "type": "session_meta",
                    "payload": {"id": CX, "source": "vscode", "originator": "Codex Desktop", "cwd": "/SECRET/path"}}),
        umsg("2026-11-10 20:00:01", "SECRET codex prompt"),
        tok("2026-11-10 20:00:30", 1000, 1000, 10.0),
        tok("2026-11-10 20:00:31", 1000, 1000, 10.0),                              # repeated reading: not a request
        tok("2026-11-11 02:00:00", 3000, 2000, 40.0),
        cx("2026-11-11 04:00:00", "error", message="You've hit your usage limit.",
           codex_error_info="usage_limit_exceeded"),
        umsg("2026-11-11 05:00:00", "Your usage limit has reset. Please continue the task you were working on."),
        tok("2026-11-11 05:00:30", 3500, 500, 2.0, resets=R2),
    ])
    write(os.path.join(cdir, f"rollout-2026-11-10T23-00-00-{CX_EXEC}.jsonl"), [
        json.dumps({"timestamp": z("2026-11-11 01:30:00"), "type": "session_meta",
                    "payload": {"id": CX_EXEC, "source": "exec", "originator": "codex_exec"}}),
        umsg("2026-11-11 01:30:01", "SECRET exec prompt"),
        tok("2026-11-11 01:31:00", 700, 700, 20.0),
    ])


def run(home, *args):
    env = dict(os.environ, HOME=home, TZ="UTC")
    return subprocess.run([sys.executable, SCRIPT, "--away", "--now", NOW, "-q", *args],
                          capture_output=True, text=True, env=env, timeout=60)


def tree(d):
    out = {}
    for base, _, files in os.walk(d):
        for f in files:
            p = os.path.join(base, f)
            st = os.stat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


class AwayCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = cls.tmp.name
        make_home(cls.home)
        cls.before = tree(cls.home)
        cls.text = run(cls.home)
        cls.js = run(cls.home, "--json")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_runs_from_fake_home_defaults(self):
        self.assertEqual(self.text.returncode, 0, self.text.stderr)
        self.assertIn("Looked at: Claude Code 4 session(s); Codex 2 session(s)", self.text.stdout)

    def test_never_prints_text_paths_or_full_ids(self):
        for out in (self.text.stdout + self.text.stderr, self.js.stdout):
            for bad in ("SECRET", "secretproject", "someone", "/path", S_NIGHT, CX, "refactor"):
                self.assertNotIn(bad, out)

    def test_writes_nothing(self):
        self.assertEqual(tree(self.home), self.before)

    def test_no_network_modules(self):
        with open(SCRIPT) as fh:
            src = fh.read()
        for mod in ("socket", "urllib", "http.client", "requests", "ssl"):
            self.assertNotRegex(src, rf"^\s*(import|from)\s+{mod}\b".replace(".", r"\."))

    def test_stretch_and_sessions(self):
        j = json.loads(self.js.stdout)
        st = j["stretches"]
        # typed prompts: 19:00 (old format), 20:00 desktop, 20:00:01 codex, 21:00, 23:10, 08:00. Stretches >= 60 min
        # with requests: 21:00-23:10 and 23:10-08:00 (the 08:00-09:00 one has a request at 08:00:10, ends at now).
        spans = [(s["start"][11:16], s["end"][11:16]) for s in st]
        self.assertIn(("23:10", "08:00"), spans)
        night = next(s for s in st if s["start"][11:16] == "23:10")
        # inside 23:10-08:00: S_NIGHT n2 n3 n4 n5 + subagent sa1 (5), S_TYPED t1 answering the 23:10 prompt (1),
        # S_HEAD h1 h2 (2), Codex 02:00 + 05:00:30 (2), codex exec 01:31 (1)
        self.assertEqual(night["requests"], 11)
        self.assertEqual(night["sessions"], 5)
        self.assertEqual(night["codex_5h"], None)       # the 5h window reset in between: not comparable
        rows = {r["session"]: r for r in j["sessions"]}
        self.assertEqual(rows["aaaaaaaa"]["subagent_requests"], 1)
        self.assertEqual(rows["cccccccc"]["mode"], "headless")
        self.assertEqual(rows["019a0000"]["tool"], "Codex")
        self.assertEqual(rows["aaaaaaaa"]["prompts"], {"auto-continue": 1, "task notice": 1})
        self.assertEqual(rows["cccccccc"]["prompts"], {"claude -p": 1})
        self.assertEqual(rows["019a0000"]["prompts"], {"Lidnight": 1})
        self.assertEqual(rows["019b0000"]["prompts"], {"exec": 1})
        self.assertEqual(rows["dddddddd"]["prompts"], {})

    def test_dedupes_streamed_blocks_and_counts_tokens(self):
        j = json.loads(self.js.stdout)
        st = {s["start"][11:16]: s for s in j["stretches"]}
        first = st["21:00"]        # 21:00 typed -> 23:10 typed: only n1 (21:00:10)
        self.assertEqual(first["requests"], 1)
        self.assertEqual(first["tokens"], 10 + 20 + 0 + 1000)

    def test_headless_and_task_prompts_are_not_typed(self):
        j = json.loads(self.js.stdout)
        self.assertEqual(j["meta"]["typed_prompts_in_window"], 6)

    def test_codex_limit_percent_same_window(self):
        sessions = L.codex_away_sessions(os.path.join(self.home, ".codex", "sessions"),
                                         L.iso("2026-11-09T00:00:00Z"), L.iso(NOW))
        s = sessions[CX]
        self.assertEqual(L._pct_change(s["pct"], L.iso("2026-11-10T21:00:00Z"), L.iso("2026-11-11T03:00:00Z")),
                         (10.0, 40.0))
        self.assertEqual(len([r for r in s["requests"]]), 3)     # repeated reading not counted

    def test_resumes(self):
        j = json.loads(self.js.stdout)
        rs = {r["session"]: r for r in j["resumes"]}
        self.assertNotIn("bbbbbbbb", rs)                   # you typed before it went on
        c = rs["aaaaaaaa"]
        self.assertEqual(c["trigger"], "Claude Code auto-continue")
        self.assertTrue(c["at_reset"])
        self.assertEqual(c["reset"][11:16], "03:00")
        self.assertEqual(c["requests"], 2)                 # n4, n5 until you typed at 08:00
        self.assertEqual(c["tokens"], (10 + 20 + 20000) + (10 + 20 + 1000))
        x = rs["019a0000"]
        self.assertEqual(x["trigger"], "Lidnight resume message")
        self.assertEqual(x["reset"][11:16], "05:00")          # from the last limit reading before the hit
        self.assertTrue(x["at_reset"])
        self.assertEqual((x["requests"], x["tokens"]), (1, 500))

    def test_text_output_shape(self):
        out = self.text.stdout
        self.assertIn("While you were away:", out)
        self.assertIn("Started again after a usage limit, with nothing typed in between: 2", out)
        self.assertIn("Claude Code auto-continue", out)
        self.assertIn("Dollar cost and Claude limit %: unknown", out)
        self.assertTrue(out.rstrip().endswith("https://lidnight.app/?from=lid-check"))
        self.assertEqual(out.count("lidnight.app"), 1)

    def test_missing_dirs(self):
        with tempfile.TemporaryDirectory() as empty:
            r = run(empty)
            self.assertEqual(r.returncode, 1)
            self.assertIn("neither", r.stderr)

    def test_codex_only(self):
        with tempfile.TemporaryDirectory() as h:
            make_home(h)
            r = run(h, "--projects-dir", os.path.join(h, "nope"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Claude Code not found", r.stdout)

    def test_bad_args(self):
        for args in (("--hours", "0"), ("--idle", "-1")):
            r = run(self.home, *args)
            self.assertEqual(r.returncode, 2)


class ReviewFixes(unittest.TestCase):
    """Cases from review: repeated limit markers, mixed transcript formats, bare rate_limit, Codex baseline."""
    T = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)

    def at(self, minutes):
        return self.T + L.timedelta(minutes=minutes)

    def test_repeated_limit_markers_are_one_restart(self):
        s = {"requests": [(self.at(120), 10, False)], "humans": [], "prompts": [],
             "limits": [(self.at(0), None), (self.at(1), self.at(115))]}
        rs = L.find_resumes(s, self.T, self.at(180))
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0]["reset"], self.at(115))
        self.assertTrue(rs[0]["at_reset"])

    def test_origin_human_wins_over_text(self):
        d = {"message": {"content": "Your claude.ai usage limit has reset. Continue."}, "origin": {"kind": "human"}}
        self.assertEqual(L._claude_prompt_kind(d, True, False), "human")

    def test_mixed_format_file(self):
        with tempfile.TemporaryDirectory() as h:
            proj = os.path.join(h, "p")
            write(os.path.join(proj, S_OLD + ".jsonl"), [
                json.dumps({"type": "user", "timestamp": z("2026-11-10 10:00:00"), "sessionId": S_OLD,
                            "message": {"role": "user", "content": "old prompt"}}),
                user("2026-11-10 12:00:00", "new prompt", S_OLD, HUMAN),
                json.dumps({"type": "user", "timestamp": z("2026-11-10 12:30:00"), "sessionId": S_OLD,
                            "message": {"role": "user", "content": "compact summary text"}}),
            ])
            got = L.claude_away_sessions(proj, L.iso("2026-11-10T00:00:00Z"), L.iso(NOW))[S_OLD]
            self.assertEqual([t.hour for t in got["humans"]], [10, 12])
            self.assertEqual(got["prompts"], [(L.iso("2026-11-10T12:30:00Z"), "other")])

    def test_bare_rate_limit_is_not_a_usage_limit(self):
        with tempfile.TemporaryDirectory() as h:
            proj = os.path.join(h, "p")
            write(os.path.join(proj, S_OLD + ".jsonl"), [
                limit("2026-11-10 10:00:00", S_OLD, "API Error: 429 overloaded, retrying"),
                limit("2026-11-10 11:00:00", S_OLD, "You've hit your limit · resets 3pm (UTC)"),
            ])
            got = L.claude_away_sessions(proj, L.iso("2026-11-10T00:00:00Z"), L.iso(NOW))[S_OLD]
            self.assertEqual([(t.hour, r.hour) for t, r in got["limits"]], [(11, 15)])

    def test_codex_reading_repeated_from_before_window_is_not_a_request(self):
        with tempfile.TemporaryDirectory() as h:
            write(os.path.join(h, f"rollout-{CX}.jsonl"), [
                json.dumps({"timestamp": z("2026-11-08 10:00:00"), "type": "session_meta", "payload": {"id": CX}}),
                tok("2026-11-08 10:00:30", 5000, 5000, 10.0),
                tok("2026-11-10 10:00:00", 5000, 5000, 10.0),      # same total again inside the window
                tok("2026-11-10 11:00:00", 5600, 600, 12.0),
            ])
            got = L.codex_away_sessions(h, L.iso("2026-11-10T00:00:00Z"), L.iso(NOW))[CX]
            self.assertEqual([(t.hour, n) for t, n, _ in got["requests"]], [(11, 600)])

    def test_error_message_hides_home(self):
        with tempfile.TemporaryDirectory() as empty:
            r = run(empty)
            self.assertNotIn(empty, r.stderr)
            self.assertIn("~/.claude/projects", r.stderr)


class ResetParse(unittest.TestCase):
    hit = datetime(2026, 11, 10, 13, 0, tzinfo=UTC)     # 22:00 in Seoul

    def test_time_only_rolls_to_next_day(self):
        self.assertEqual(L.parse_reset("You've hit your weekly limit · resets 9am (Asia/Seoul)", self.hit),
                         datetime(2026, 11, 11, 0, 0, tzinfo=UTC))

    def test_with_date(self):
        self.assertEqual(L.parse_reset("You've hit your weekly limit · resets Nov 13 at 9:30am (Asia/Seoul)",
                                       self.hit), datetime(2026, 11, 13, 0, 30, tzinfo=UTC))

    def test_epoch_form(self):
        self.assertEqual(L.parse_reset("Claude AI usage limit reached|1794000000", self.hit),
                         datetime.fromtimestamp(1794000000, UTC))

    def test_unknown(self):
        self.assertIsNone(L.parse_reset("You've reached your Fable limit. Switch to another model", self.hit))
        self.assertIsNone(L.parse_reset("resets 9am (Not/AZone)", self.hit))


class Stretches(unittest.TestCase):
    def test_bounds(self):
        t = lambda h: datetime(2026, 11, 10, h, tzinfo=UTC)
        got = L.away_stretches([t(3), t(4)], t(0), t(10), L.timedelta(minutes=60))
        self.assertEqual([(a.hour, b.hour, oa, ob) for a, b, oa, ob in got],
                         [(0, 3, True, False), (3, 4, False, False), (4, 10, False, True)])


if __name__ == "__main__":
    unittest.main()
