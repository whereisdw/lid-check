#!/usr/bin/env python3
"""lid-check: did Claude Code send requests while your MacBook was asleep / lid closed?

Read-only. Standard library only. No network.
Reads:  `pmset -g log` (sleep / wake / DarkWake events)
        ~/.claude/projects/**/*.jsonl (only timestamps, request ids, token counts)
Prints: times and numbers only. Never prints conversation text, project names, or paths.

  python3 lid_check.py               # last 7 days
  python3 lid_check.py --days 14
  python3 lid_check.py --json
"""
import argparse
import bisect
import glob
import itertools
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
DARKWAKE_DEFAULT = timedelta(seconds=90)   # DarkWake with no later Sleep/Wake: assume 90 s
IDLE_GAP = timedelta(minutes=5)            # prompt-cache lifetime assumed by the rewrite rule
REWRITE_MIN = 20_000                       # tokens
AFTER_WAKE = timedelta(minutes=10)         # look this long after wake for a cache rewrite
CONTEXT = timedelta(days=3)                # extra history read so "previous request" is known
PAD = timedelta(minutes=10)                # only parse lines this close to a sleep period (needs > the 5 min idle gap)

# One pmset line: "2026-09-18 00:46:05 +0800 Sleep<spaces or tab>Entering Sleep state due to 'Clamshell Sleep':..."
# Read loosely (one space or a tab after the type, +09:00 or Z zone, leading blanks, quote marker, trailing spaces/CR),
# but only the three event types we need, so WakeDetails / WakeTime / Assertions / Notification lines never match.
_TS = r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ?([+-]\d\d:?\d\d|Z)"
PM_RE = re.compile(r"^[\s>]*" + _TS + r"\s+(Sleep|Wake|DarkWake)(?![A-Za-z])\s*(.*?)\s*$")
PM_ANY_RE = re.compile(r"^[\s>]*" + _TS + r"\s+\S")
REASON_RE = re.compile(r"due to '([^']*)'")


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    except Exception:
        return ""


# ---------- sleep / wake ----------
def parse_power_events(text):
    """-> (events, first_timestamp). events: [(t, kind, detail)], kind in Sleep/Wake/DarkWake."""
    ev, first = [], None
    for line in text.splitlines():
        m = PM_RE.match(line)
        if m:
            tm, kind, detail = m.group(1, 3, 4)
        else:
            m = PM_ANY_RE.match(line)
            if not m:
                continue
            tm, kind, detail = m.group(1), None, ""
        t = datetime.strptime(f"{tm} {m.group(2)}", "%Y-%m-%d %H:%M:%S %z").astimezone(UTC)
        if first is None or t < first:
            first = t
        if kind is None:
            continue
        # "Entering DarkWake state due to 'Clamshell Sleep'" sits in the Sleep column but the Mac is still
        # running with the screen off (keep-awake apps, or the hop before a real sleep). It is not asleep.
        if kind == "Sleep" and detail.startswith("Entering DarkWake"):
            continue
        ev.append((t, kind, detail))
    ev.sort(key=lambda e: e[0])  # stable: same-second events keep file order
    return ev, first


def lid_reason(detail):
    m = REASON_RE.search(detail)
    return "Clamshell" in (m.group(1) if m else detail)


def sleep_periods(ev, now):
    """A period starts at the first Sleep and ends at the next Wake (a full wake).
    DarkWake and the Sleep that follows each DarkWake stay inside the period."""
    out, cur = [], None
    for t, k, d in ev:
        if k == "Sleep":
            if cur is None:
                cur = {"start": t, "end": None, "lid_closed": lid_reason(d), "darkwake": 0}
        elif k == "DarkWake":
            if cur is not None:
                cur["darkwake"] += 1
        elif k == "Wake":
            if cur is not None:
                cur["end"] = t
                out.append(cur)
                cur = None
    if cur is not None:
        cur["end"] = now
        cur["ongoing"] = True
        out.append(cur)
    return out


def darkwake_windows(ev):
    win = []
    for i, (t, k, _) in enumerate(ev):
        if k != "DarkWake":
            continue
        end = t + DARKWAKE_DEFAULT
        for t2, k2, _ in ev[i + 1:]:
            if k2 in ("Sleep", "Wake"):
                end = t2
                break
        win.append((t, end))
    return win


# ---------- progress (stderr only; stdout stays the table / JSON) ----------
class Progress:
    def __init__(self, quiet):
        self.quiet, self.tty, self.last, self.t0 = quiet, sys.stderr.isatty(), 0.0, time.monotonic()

    def say(self, msg):
        if not self.quiet:
            print(msg, file=sys.stderr, flush=True)

    def tick(self, msg):
        """Overwrite one line about every 2 s on a terminal; print nothing in between (and nothing when piped)."""
        now = time.monotonic()
        if self.quiet or not self.tty or now - self.last < 2:
            return
        self.last = now
        print("\r  " + msg + "   ", end="", file=sys.stderr, flush=True)

    def done(self, msg):
        if self.tty and not self.quiet:
            print("\r" + " " * 70 + "\r", end="", file=sys.stderr)
        self.say(f"{msg} ({time.monotonic() - self.t0:.0f}s)")


# ---------- Claude Code transcripts ----------
# Cheap checks on the raw line, so most lines (often MBs of message text) are never json-parsed.
# Every check only ever decides "skip"; it keeps a line whenever it is unsure, so results match a full parse.
# Lines are read as bytes (no UTF-8 decoding of the lines we skip); only a line that is parsed is decoded.
ASSISTANT_RE = re.compile(rb'"type"\s*:\s*"assistant"')
TS_VALUE_RE = re.compile(rb'"timestamp"\s*:\s*"([^"]*)"')   # all matches: a nested "timestamp" key may also match


def _utc_key(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def claude_requests(root, since, until, spans=None, progress=None):
    """-> (requests, errors). Reads only timestamp, requestId, usage numbers, error markers.
    spans: [(start, end)] sleep periods. When given, only lines timestamped within PAD of a period
    (+ AFTER_WAKE) are parsed; the numbers in the table are the same as with spans=None (see below)."""
    reqs, errors = {}, []
    lo_win, hi_win = _utc_key(since - CONTEXT), _utc_key(until)
    cutoff = (since - CONTEXT).timestamp()
    los = his = None
    if spans:
        rng = sorted((_utc_key(a - PAD - timedelta(seconds=1)), _utc_key(b + AFTER_WAKE + PAD + timedelta(seconds=1)))
                     for a, b in spans)
        los, his = [r[0] for r in rng], [r[1] for r in rng]
        his = [max(his[:i + 1]) for i in range(len(his))]    # running max: periods may overlap after sorting
        # Transcripts are append-only, so a file last modified before the earliest period (minus a day of slack
        # for clock skew) has no line inside any period and is never opened.
        cutoff = max(cutoff, (min(a for a, _ in spans) - PAD - timedelta(days=1)).timestamp())
        # Likewise a file whose first timestamps are over a day after the last period ended starts too late.
        starts_too_late = _utc_key(max(b for _, b in spans) + AFTER_WAKE + PAD + timedelta(days=1))
    files = []
    for f in glob.iglob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(f) >= cutoff:
                files.append(f)
        except OSError:
            continue
    if progress:
        progress.say(f"Scanning {len(files):,} transcript file(s) for requests (a large ~/.claude can take minutes)...")
    for n, f in enumerate(files, 1):
        if progress:
            progress.tick(f"scanned {n - 1:,}/{len(files):,} files")
        try:
            fh = open(f, "rb")
        except OSError:
            continue
        if los is not None:
            first = [k for ln in itertools.islice(fh, 20) for v in TS_VALUE_RE.findall(ln)
                     for k in [v.decode("ascii", "replace")[:19]] if len(v) >= 20 and v[-1:] == b"Z"]
            if first and min(first) > starts_too_late:
                fh.close()
                continue
            fh.seek(0)
        ghost = None    # earliest skipped request of this file that is inside the window but outside every span
        with fh:
            for line in fh:
                if b"assistant" not in line and b"api_error" not in line:
                    continue
                if los is not None:
                    vals = TS_VALUE_RE.findall(line)
                    vals = [v.decode("ascii", "replace") for v in vals]
                    keys = [v[:19] for v in vals if len(v) >= 20 and v[10] == "T" and v[-1] == "Z"]
                    if vals and len(keys) == len(vals):         # parse anyway when any timestamp looks unusual
                        i = [bisect.bisect_right(los, k) - 1 for k in keys]
                        if not any(j >= 0 and k <= his[j] for j, k in zip(i, keys)):
                            # Outside every period. It still tells the first-request-of-a-file rule that an
                            # earlier request exists, so remember the earliest one (in the window, a real request).
                            inw = [k for k in keys if lo_win <= k <= hi_win]
                            if inw and (ghost is None or min(inw) < ghost) and ASSISTANT_RE.search(line) \
                                    and b"isApiErrorMessage" not in line:
                                ghost = min(inw)
                            continue
                try:
                    d = json.loads(line.decode("utf-8", "ignore"))
                except ValueError:
                    continue
                if not isinstance(d, dict) or not d.get("timestamp"):
                    continue
                try:
                    t = iso(d["timestamp"])
                except ValueError:
                    continue
                if t < since - CONTEXT or t > until:
                    continue
                if d.get("type") == "system" and d.get("subtype") == "api_error":
                    errors.append(t)
                    continue
                if d.get("type") != "assistant":
                    continue
                if d.get("isApiErrorMessage"):
                    errors.append(t)
                    continue
                msg = d.get("message") or {}
                rid = d.get("requestId") or msg.get("id") or d.get("uuid")
                u = msg.get("usage") or {}
                r = reqs.get(rid)
                if r is None:
                    r = reqs[rid] = {"t": t, "chain": f, "cw": 0, "cr": 0}
                r["t"] = min(r["t"], t)
                r["cw"] = max(r["cw"], u.get("cache_creation_input_tokens") or 0)
                r["cr"] = max(r["cr"], u.get("cache_read_input_tokens") or 0)
        if ghost:
            # A stand-in for the skipped requests, so the first parsed request of this file is not mistaken for
            # the file's first. It lies > PAD from every period, so it is never counted or a 5-minute neighbour.
            reqs[("ghost", f)] = {"t": iso(ghost + "Z"), "chain": f, "cw": 0, "cr": 0}
    if progress:
        progress.done(f"Scanned {len(files):,} file(s)")
    return list(reqs.values()), errors


def mark_rewrites(reqs):
    """Rewrite = request after >=5 min idle in the same transcript, cache_read < 10% of
    (cache_read + cache_write) and cache_write >= 20,000 tokens. First request of a transcript is skipped."""
    by_chain = defaultdict(list)
    for r in reqs:
        by_chain[r["chain"]].append(r)
    for rs in by_chain.values():
        rs.sort(key=lambda r: r["t"])
        for i, r in enumerate(rs):
            r["rewrite"] = 0
            if i == 0 or r["t"] - rs[i - 1]["t"] < IDLE_GAP:
                continue
            tot = r["cr"] + r["cw"]
            if r["cw"] >= REWRITE_MIN and r["cr"] < 0.10 * max(tot, 1):
                r["rewrite"] = r["cw"]


# ---------- per period ----------
def summarize(p, reqs, errors, dw):
    a, b = p["start"], p["end"]
    wins = [(x, y) for x, y in dw if x < b and y > a]
    inside = [r for r in reqs if a <= r["t"] < b]
    after = [r for r in reqs if b <= r["t"] < b + AFTER_WAKE]
    in_dw = lambda t: any(x <= t < y for x, y in wins)
    errs = [t for t in errors if a <= t < b]
    return {
        "start": a.astimezone().isoformat(timespec="minutes"),
        "end": b.astimezone().isoformat(timespec="minutes"),
        "minutes": round((b - a).total_seconds() / 60, 1),
        "lid_closed": p["lid_closed"],
        "ongoing": bool(p.get("ongoing")),
        "darkwake": p["darkwake"],
        "requests": len(inside),
        "requests_in_darkwake": sum(1 for r in inside if in_dw(r["t"])) + sum(1 for t in errs if in_dw(t)),
        "errors": len(errs),
        "cache_rewrite_tokens": sum(r["rewrite"] for r in inside),
        "cache_rewrite_tokens_after_wake": sum(r["rewrite"] for r in after),
    }


COLS = [("start", "Start (local)"), ("minutes", "Min"), ("lid", "Lid"), ("darkwake", "DarkWakes"),
        ("requests", "Requests"), ("requests_in_darkwake", "In DarkWake"), ("errors", "Errors"),
        ("cache_rewrite_tokens", "Rewrite tok"), ("cache_rewrite_tokens_after_wake", "Rewrite tok <10m after wake")]


def fmt_row(r):
    cells = []
    for k, _ in COLS:
        if k == "start":
            cells.append(r["start"].replace("T", " ")[:16])
        elif k == "lid":
            cells.append("closed" if r["lid_closed"] else "no")
        else:
            v = r[k]
            cells.append(f"{v:,}" if isinstance(v, int) else str(v))
    return cells


def print_table(rows):
    head = [h for _, h in COLS]
    body = [fmt_row(r) for r in rows]
    w = [max(len(head[i]), *(len(b[i]) for b in body)) for i in range(len(head))]
    line = lambda cells: "  ".join(c.rjust(w[i]) if i else c.ljust(w[i]) for i, c in enumerate(cells))
    print(line(head))
    print("  ".join("-" * x for x in w))
    for b in body:
        print(line(b))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="List the times your Mac was asleep (from `pmset -g log`) and count the Claude Code requests "
                    "recorded inside them (from ~/.claude/projects). Read-only: no network, no files written.",
        epilog="examples:\n  python3 lid_check.py                      last 7 days\n"
               "  python3 lid_check.py --days 14 --lid-only only sleeps that began with a lid close\n"
               "  python3 lid_check.py --json                same numbers as JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=7, help="how many days back to look (default 7)")
    ap.add_argument("--min-minutes", type=float, default=5, help="ignore sleep periods shorter than this (default 5)")
    ap.add_argument("--lid-only", action="store_true", help="only periods that began with a lid-close sleep")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument("--projects-dir", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"),
                    help="Claude Code transcript folder (default ~/.claude/projects)")
    ap.add_argument("-q", "--quiet", action="store_true", help="no progress messages on stderr")
    ap.add_argument("--pmset-log", help="read this saved `pmset -g log` text file instead of running pmset")
    a = ap.parse_args(argv)
    if not a.days > 0:
        ap.error("--days must be greater than 0 (for example --days 7)")
    if a.min_minutes < 0:
        ap.error("--min-minutes must be 0 or more")

    prog = Progress(a.quiet)
    now = datetime.now(UTC)
    since = now - timedelta(days=a.days)

    if a.pmset_log:
        try:
            with open(a.pmset_log, errors="ignore") as fh:
                text = fh.read()
        except OSError as e:
            print(f"lid-check: cannot read --pmset-log file: {e.strerror or e}", file=sys.stderr)
            return 1
    else:
        prog.say("Reading the pmset log... (macOS builds it on demand; it can take a minute)")
        text = run(["pmset", "-g", "log"])
        prog.done("Read the pmset log")
    ev, log_first = parse_power_events(text)
    if log_first is None:
        print("lid-check: no pmset log lines found. `pmset -g log` printed nothing readable "
              "(this tool needs macOS). To use a saved copy: pmset -g log > pm.txt; "
              "python3 lid_check.py --pmset-log pm.txt", file=sys.stderr)
        return 1

    periods = [p for p in sleep_periods(ev, now) if p["end"] > since]
    periods = [p for p in periods if (p["end"] - p["start"]) >= timedelta(minutes=a.min_minutes)]
    if a.lid_only:
        periods = [p for p in periods if p["lid_closed"]]

    # Only read transcripts when there is a sleep period to look at (they can be many GB).
    if periods and not os.path.isdir(a.projects_dir):
        print(f"lid-check: Claude Code folder not found: {a.projects_dir} "
              "(use --projects-dir if yours is elsewhere)", file=sys.stderr)
        return 1
    if periods:
        first = min(p["start"] for p in periods)
        reqs, errors = claude_requests(a.projects_dir, first, now, [(p["start"], p["end"]) for p in periods], prog)
    else:
        reqs, errors = [], []
    mark_rewrites(reqs)
    dw = darkwake_windows(ev)
    rows = [summarize(p, reqs, errors, dw) for p in periods]

    meta = {
        "days": a.days,
        "pmset_log_starts": log_first.astimezone().isoformat(timespec="minutes") if log_first else None,
        "sleep_events_in_log": sum(1 for e in ev if e[1] == "Sleep"),
        "darkwake_events_in_log": sum(1 for e in ev if e[1] == "DarkWake"),
    }
    if a.json:
        print(json.dumps({"meta": meta, "periods": rows}, indent=1))
        return 0

    print(f"lid-check: last {a.days:g} days. pmset log starts {meta['pmset_log_starts'] or 'unknown'}.")
    if not rows:
        print("\nNo sleep periods found in that window.")
        print("That means the pmset log has no Sleep events there. Either the Mac did not sleep, or the log")
        print("does not reach back that far. If you keep the Mac awake with the lid closed, there is no")
        print("sleep period for this tool to measure.")
        return 0
    print()
    print_table(rows)
    tot = lambda k: sum(r[k] for r in rows)
    print(f"\n{len(rows)} sleep period(s), {sum(1 for r in rows if r['lid_closed'])} began with a lid-close sleep. "
          f"Requests recorded inside them: {tot('requests'):,} "
          f"({tot('requests_in_darkwake'):,} inside DarkWake windows).")
    print("Notes:")
    print("- Requests = Claude Code transcript entries timestamped inside the period. Time is when it was recorded.")
    print("- In DarkWake = recorded between a DarkWake and the next Sleep/Wake (90 s if none follows).")
    print("- Rewrite tok = cache-write tokens of a request that came after 5+ idle minutes and re-sent a 20,000+ token")
    print("  prompt without reading it from cache. This also happens when the Mac never slept.")
    print("- This shows what was recorded. It does not show how much of any usage limit was used.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
