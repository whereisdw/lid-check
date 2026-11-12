"""lid_check against real `pmset -g log` lines copied from public GitHub issues.
See fixtures/real_pmset_lines.txt for the source URL of every block.
Expected values below were worked out by hand from the lines, not from lid_check output."""
import io, json, os, re, sys, tempfile, unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lid_check as L

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "real_pmset_lines.txt")
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
STRICT = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}\s+(Sleep|Wake|DarkWake)\s")


def blocks():
    out, cur = {}, None
    with open(FIX, encoding="utf-8", newline="") as fh:
        for line in fh.read().split("\n"):
            m = re.match(r"# === (\S+) \| (\S+) \|", line)
            if m:
                cur = out[m.group(1)] = []
            elif cur is not None and line and not line.startswith("#"):
                cur.append(line)
    return {k: v for k, v in out.items() if v}  # comment-only blocks (inline quotes) carry no raw lines


B = blocks()


def parse(name):
    ev, first = L.parse_power_events("\n".join(B[name]) + "\n")
    return ev, first, L.sleep_periods(ev, NOW)


def utc(s):  # "2026-09-08 15:08:46 -0600"
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc)


class RealLines(unittest.TestCase):
    def test_fixture_is_there(self):
        self.assertGreaterEqual(len(B), 14)
        self.assertGreaterEqual(sum(len(v) for v in B.values()), 55)
        for name, lines in B.items():
            self.assertTrue(lines, name)

    def test_every_event_line_is_read_except_entering_darkwake(self):
        for name, lines in B.items():
            strict = [l for l in lines if STRICT.match(l)]
            skipped = [l for l in strict if "Entering DarkWake state" in l]
            ev, _, _ = parse(name)
            self.assertEqual(len(ev), len(strict) - len(skipped), name)

    def test_other_line_types_are_not_events(self):
        # Notification / Assertions / WakeDetails / HibernateStats / WakeTime lines carry a timestamp too.
        for name in ("openlogi-clamshell", "batfi-usbc-chime", "kint-fullwake-2022", "rigger-darkwake-run"):
            ev, _, _ = parse(name)
            self.assertTrue(all(k in ("Sleep", "Wake", "DarkWake") for _, k, _ in ev))
        ev, _, _ = parse("batfi-usbc-chime")
        self.assertEqual([k for _, k, _ in ev], ["DarkWake", "Wake"])
        ev, _, _ = parse("kint-fullwake-2022")
        self.assertEqual([k for _, k, _ in ev], ["Wake", "Wake"])

    def test_clamshell_then_lid_wake(self):  # OpenLogi #1281
        ev, first, ps = parse("openlogi-clamshell")
        self.assertEqual(first, utc("2026-09-08 15:08:41 -0600"))  # the Notification line counts for the log start
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0]["start"], utc("2026-09-08 15:08:46 -0600"))
        self.assertEqual(ps[0]["end"], utc("2026-09-08 15:18:34 -0600"))
        self.assertTrue(ps[0]["lid_closed"])

    def test_overnight_cycle(self):  # OpenLogi #1281, second block (cycles between lines were cut by the reporter)
        ev, _, ps = parse("openlogi-overnight")
        self.assertEqual(len(ps), 1)
        self.assertTrue(ps[0]["lid_closed"])
        self.assertEqual(ps[0]["darkwake"], 2)
        self.assertEqual(ps[0]["end"], utc("2026-09-18 09:01:28 +0800"))
        w = L.darkwake_windows(ev)
        self.assertEqual((w[0][1] - w[0][0]).total_seconds(), 45)  # 00:46:09 -> Sleep 00:46:54

    def test_darkwake_window_equals_logged_tail(self):  # the "16 secs" at the end of the DarkWake line
        ev, _, _ = parse("kinlace-maintenance")
        w = L.darkwake_windows(ev)
        self.assertEqual([(y - x).total_seconds() for x, y in w], [16])

    def test_sleep_length_equals_logged_tail(self):  # "266 secs" at the end of the Sleep line (Intel, Wake from Hibernate)
        ev, _, ps = parse("gregertw-hibernate-2021")
        self.assertEqual(len(ps), 1)
        self.assertTrue(ps[0]["lid_closed"])
        self.assertEqual((ps[0]["end"] - ps[0]["start"]).total_seconds(), 266 + 0)  # 19:34:17 -> 19:38:43

    def test_maintenance_and_sleep_service_stay_one_period(self):  # BatteryOptimizer #4
        _, _, ps = parse("batteryoptimizer-sleepservice")
        self.assertEqual(len(ps), 1)
        self.assertFalse(ps[0]["lid_closed"])
        self.assertEqual(ps[0]["start"], utc("2024-10-29 01:14:06 +0800"))
        self.assertEqual(ps[0]["end"], utc("2024-10-29 01:42:05 +0800"))

    def test_two_clamshell_lines_make_one_open_period(self):  # nalin #238
        _, _, ps = parse("nalin-clamshell-battery")
        self.assertEqual(len(ps), 1)
        self.assertTrue(ps[0]["lid_closed"])
        self.assertTrue(ps[0]["ongoing"])
        self.assertEqual(ps[0]["start"], utc("2026-09-10 14:11:48 -0700"))

    def test_entering_darkwake_is_not_a_sleep(self):
        # Sleep column, but the Mac is running with the screen off. Each of these is followed by a DarkWake to
        # FullWake (or DarkWake) line, never by an 'Entering Sleep state' line.
        for name in ("bigsurface-entering-darkwake-clamshell", "x1c6-entering-darkwake-clamshell",
                     "samsung-software-sleep-darkwake", "rigger-darkwake-run"):
            ev, _, ps = parse(name)
            self.assertEqual(ps, [], name)
            self.assertNotIn("Sleep", [k for _, k, _ in ev], name)

    def test_software_and_idle_sleep_are_not_lid(self):
        _, _, ps = parse("nvidia-software-sleep")
        self.assertEqual(len(ps), 1)
        self.assertFalse(ps[0]["lid_closed"])
        self.assertEqual(ps[0]["darkwake"], 1)
        self.assertEqual((ps[0]["end"] - ps[0]["start"]).total_seconds(), 20)
        _, _, ps = parse("ocm3800-idle-sleep")
        self.assertTrue(all(not p["lid_closed"] for p in ps))

    def test_whitespace_and_zone_variants(self):
        # Same real lines, only the whitespace / zone spelling changed (synthetic, to prove the loose reading).
        base = "\n".join(B["openlogi-clamshell"])
        for text in (base.replace("\n", "\r\n"),                                  # CRLF
                     re.sub(r"  +", " ", base),                                   # single spaces
                     re.sub(r"(Sleep|Wake|DarkWake) +", r"\1\t", base),            # tab after the type
                     base.replace("-0600", "-06:00"),                              # colon in the zone
                     "".join("> " + l + "\n" for l in base.split("\n")),           # quoted reply
                     "   " + base.replace("\n", "   \n   ")):                      # padding
            ev, _ = L.parse_power_events(text)
            self.assertEqual([k for _, k, _ in ev], ["Sleep", "Wake"], repr(text[:80]))
            self.assertEqual(ev[0][0], utc("2026-09-08 15:08:46 -0600"))
            self.assertTrue(L.sleep_periods(ev, NOW)[0]["lid_closed"])

    def test_no_false_matches(self):
        for junk in ("2026-09-08 15:08:46 -0600 WakeTime              WakeTime: 0.016 sec",
                     "2026-09-08 15:08:46 -0600 WakeDetails           DriverReason:smc.70070000",
                     "2026-09-08 15:08:46 -0600 Sleeping  something",
                     "2026-09-08 15:08:46 -0600 Notification   Sleep",
                     "Sleep   Entering Sleep state due to 'Clamshell Sleep'",
                     "2026-09-08 Sleep Entering Sleep state"):
            self.assertEqual(L.parse_power_events(junk)[0], [], junk)

    def test_cli_on_real_lines(self):
        text = "\n".join("\n".join(v) for v in B.values()) + "\n"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pmset.txt")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = L.main(["--pmset-log", p, "--days", "3000", "--projects-dir", d, "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(out.getvalue())
        strict = [l for l in text.split("\n") if STRICT.match(l)]
        n_sleep = sum(1 for l in strict if STRICT.match(l).group(1) == "Sleep" and "Entering DarkWake" not in l)
        n_dark = sum(1 for l in strict if STRICT.match(l).group(1) == "DarkWake")
        self.assertEqual(data["meta"]["sleep_events_in_log"], n_sleep)
        self.assertEqual(data["meta"]["darkwake_events_in_log"], n_dark)
        self.assertGreater(len(data["periods"]), 0)


if __name__ == "__main__":
    unittest.main()
