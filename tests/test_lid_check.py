import json, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lid_check as L

PM = """PM ASL data store: /var/log/powermanagement
2026-11-10 22:00:00 +0900 Assertions          \tPID 1(foo) Created NoIdleSleepAssertion "x" 00:00:00
2026-11-10 22:00:10 +0900 Sleep               \tEntering Sleep state due to 'Clamshell Sleep':TCPKeepAlive=enabled Using Batt (Charge:80%) \t 40 secs
2026-11-10 22:30:00 +0900 DarkWake            \tDarkWake from Normal Sleep [CDNPB] : due to EC.LidOpen/ Using BATT
2026-11-10 22:30:20 +0900 Sleep               \tEntering Sleep state due to 'Maintenance Sleep':TCPKeepAlive=enabled Using Batt
2026-11-10 23:00:00 +0900 DarkWake            \tDarkWake from Normal Sleep [CDNPB] : due to RTC/Maintenance Using BATT
2026-11-10 23:00:30 +0900 Sleep               \tEntering Sleep state due to 'Maintenance Sleep':TCPKeepAlive=enabled Using Batt
2026-11-11 07:00:00 +0900 Wake                \tWake from Normal Sleep [CDNVA] : due to LID0 Using BATT
2026-11-11 09:00:00 +0900 Sleep               \tEntering Sleep state due to 'Idle Sleep':TCPKeepAlive=enabled Using AC
2026-11-11 09:02:00 +0900 Wake                \tWake from Normal Sleep [CDNVA] : due to UserActivity Using AC
"""
JST = timezone(timedelta(hours=9))


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def asst(rid, when, cw=0, cr=0):
    return json.dumps({"type": "assistant", "timestamp": ts(when), "requestId": rid,
                       "message": {"content": [{"type": "text", "text": "SECRET-CONVERSATION-TEXT"}],
                                   "usage": {"cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}}})


class T(unittest.TestCase):
    def test_parse_periods(self):
        ev, first = L.parse_power_events(PM)
        self.assertEqual([k for _, k, _ in ev].count("DarkWake"), 2)
        self.assertEqual(first.astimezone(JST).strftime("%H:%M:%S"), "22:00:00")
        ps = L.sleep_periods(ev, datetime(2026, 11, 12, tzinfo=timezone.utc))
        self.assertEqual(len(ps), 2)  # 22:00-07:00 (lid) and 09:00-09:02
        self.assertTrue(ps[0]["lid_closed"])
        self.assertEqual(ps[0]["darkwake"], 2)
        self.assertFalse(ps[1]["lid_closed"])

    def test_darkwake_windows(self):
        ev, _ = L.parse_power_events(PM)
        w = L.darkwake_windows(ev)
        self.assertEqual([(y - x).total_seconds() for x, y in w], [20, 30])

    def test_unfinished_sleep_is_ongoing(self):
        ev, _ = L.parse_power_events(PM.split("2026-11-11 07:00:00")[0])
        ps = L.sleep_periods(ev, datetime(2026, 11, 11, tzinfo=timezone.utc))
        self.assertTrue(ps[0]["ongoing"])

    def test_end_to_end_no_content_leak(self):
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, "projects", "-Users-someone-secretproject")
            os.makedirs(proj)
            lines = [
                asst("r0", "2026-11-10 21:00:00", cw=30000, cr=0),            # before sleep, first of chain
                asst("r1", "2026-11-10 22:30:10", cw=100, cr=50000),           # inside DarkWake window
                asst("r1", "2026-11-10 22:30:11", cw=100, cr=50000),           # same request id, counted once
                asst("r2", "2026-11-10 23:10:00", cw=40000, cr=0),             # inside sleep, not DarkWake; idle>5m, rewrite
                json.dumps({"type": "system", "subtype": "api_error", "timestamp": ts("2026-11-11 03:00:00")}),
                asst("r3", "2026-11-11 07:03:00", cw=50000, cr=100),           # 3 min after wake, rewrite
                "not json at all",
            ]
            with open(os.path.join(proj, "secret-session-name.jsonl"), "w") as fh:
                fh.write("\n".join(lines) + "\n")
            pm = os.path.join(d, "pm.txt")
            open(pm, "w").write(PM)
            import io, contextlib
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                L.main(["--days", "36500", "--json", "--projects-dir", os.path.join(d, "projects"), "--pmset-log", pm])
            raw = out.getvalue()
            self.assertNotIn("SECRET", raw)
            self.assertNotIn("secretproject", raw)
            self.assertNotIn(d, raw)
            data = json.loads(raw)
            p = data["periods"][0]
            self.assertEqual(p["requests"], 2)                      # r1 once + r2
            self.assertEqual(p["requests_in_darkwake"], 1)
            self.assertEqual(p["errors"], 1)
            self.assertEqual(p["cache_rewrite_tokens"], 40000)
            self.assertEqual(p["cache_rewrite_tokens_after_wake"], 50000)
            self.assertEqual(p["darkwake"], 2)
            # table mode too
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                L.main(["--days", "36500", "--projects-dir", os.path.join(d, "projects"), "--pmset-log", pm])
            self.assertNotIn("SECRET", out.getvalue())
            self.assertNotIn("secretproject", out.getvalue())

    def test_no_periods_message(self):
        with tempfile.TemporaryDirectory() as d:
            pm = os.path.join(d, "pm.txt")
            open(pm, "w").write("PM ASL data store: x\n2026-11-10 22:00:00 +0900 Notification   Display is turned on\n")
            import io, contextlib
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                L.main(["--pmset-log", pm, "--projects-dir", d])
            self.assertIn("No sleep periods", out.getvalue())


if __name__ == "__main__":
    unittest.main()
