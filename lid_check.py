#!/usr/bin/env python3
"""lid-check: did Claude Code send requests while your MacBook was asleep / lid closed?

Read-only. Standard library only. No network.
Reads:  `pmset -g log` (sleep / wake / DarkWake events)
        ~/.claude/projects/**/*.jsonl (only timestamps, ids, model names, token counts)
        --away also reads ~/.codex/sessions/**/*.jsonl (timestamps, ids, token and limit numbers)
Prints: times and numbers only. Never prints conversation text or paths. Project folder names appear only
        with --usage (home-folder part removed; --redact hides them too).

  python3 lid_check.py               # last 7 days
  python3 lid_check.py --days 14
  python3 lid_check.py --json
  python3 lid_check.py --usage       # token counts per session / subagent
  python3 lid_check.py --away        # what ran while you were away (last 24 h, Claude Code + Codex)
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


def _recent_files(root, cutoff):
    """Transcript files last modified at or after `cutoff` (epoch seconds). Transcripts are append-only,
    so an older file has no line after the cutoff and is never opened."""
    files = []
    for f in glob.iglob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(f) >= cutoff:
                files.append(f)
        except OSError:
            continue
    return files


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
    files = _recent_files(root, cutoff)
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


# ---------- --usage: token counts per session and subagent ----------
TOK = ("input", "output", "cache_write", "cache_read")
_USAGE_KEYS = (("input", "input_tokens"), ("output", "output_tokens"),
               ("cache_write", "cache_creation_input_tokens"), ("cache_read", "cache_read_input_tokens"))
NIGHT_HOURS = (0, 7)                       # local 00:00-07:00 counts as "night"


def project_label(folder, home=None):
    """Claude Code names a project folder after its path with every non-alphanumeric character turned into '-'
    (/Users/me/dev/app -> -Users-me-dev-app). Drop the home-folder part so the user name is not printed."""
    home = home if home is not None else os.path.expanduser("~")
    enc = re.sub(r"[^A-Za-z0-9]", "-", home)
    if enc and folder == enc:
        return "~"
    if enc and folder.startswith(enc + "-"):
        return folder[len(enc) + 1:]
    return folder.lstrip("-") or folder


def _agent_type(path, cache):
    """agentType from the subagent's sibling .meta.json (only that one field is read)."""
    meta = path[:-len(".jsonl")] + ".meta.json"
    if meta not in cache:
        t = None
        try:
            with open(meta, encoding="utf-8", errors="ignore") as fh:
                v = json.load(fh).get("agentType")
            if isinstance(v, str):
                t = v[:40]
        except (OSError, ValueError, AttributeError):
            pass
        cache[meta] = t
    return cache[meta]


def usage_requests(root, since, until, progress=None):
    """-> [request]. One entry per API request (streamed blocks share a requestId; each usage field takes the
    max over them). Reads only: timestamp, ids, model name, usage numbers, sidechain markers, agentType."""
    lo, hi = _utc_key(since), _utc_key(until)
    files = _recent_files(root, since.timestamp())
    if progress:
        progress.say(f"Scanning {len(files):,} transcript file(s) for token usage (a large ~/.claude can take minutes)...")
    reqs, meta_cache = {}, {}
    for n, f in enumerate(files, 1):
        if progress:
            progress.tick(f"scanned {n - 1:,}/{len(files):,} files")
        rel = os.path.relpath(f, root).split(os.sep)
        proj = rel[0] if len(rel) > 1 else ""
        in_sub = "subagents" in rel[:-1]
        path_session = rel[rel.index("subagents") - 1] if in_sub and rel.index("subagents") >= 1 \
            else rel[-1][:-len(".jsonl")]
        try:
            fh = open(f, "rb")
        except OSError:
            continue
        with fh:
            for line in fh:
                if b'"usage"' not in line or b"assistant" not in line:
                    continue
                vals = [v.decode("ascii", "replace") for v in TS_VALUE_RE.findall(line)]
                keys = [v[:19] for v in vals if len(v) >= 20 and v[10] == "T" and v[-1] == "Z"]
                if vals and len(keys) == len(vals) and (max(keys) < lo or min(keys) > hi):
                    continue                     # every timestamp on the line is outside the window
                try:
                    d = json.loads(line.decode("utf-8", "ignore"))
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("type") != "assistant" or d.get("isApiErrorMessage"):
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                    continue
                model = msg.get("model")
                if model == "<synthetic>":       # made locally by Claude Code, not an API request
                    continue
                try:
                    t = iso(d.get("timestamp") or "")
                except ValueError:
                    continue
                if t < since or t > until:
                    continue
                side = in_sub or d.get("isSidechain") is True
                session = d.get("sessionId") if isinstance(d.get("sessionId"), str) else path_session
                agent = None
                if side:
                    agent = d.get("agentId") if isinstance(d.get("agentId"), str) else \
                        (rel[-1][:-len(".jsonl")] if in_sub else "sidechain")
                rid = d.get("requestId") or msg.get("id") or d.get("uuid")
                key = (f, rid) if rid else (f, len(reqs))
                r = reqs.get(key)
                if r is None:
                    r = reqs[key] = {"t": t, "project": proj, "session": session, "agent": agent,
                                     "agent_type": _agent_type(f, meta_cache) if in_sub else None,
                                     "workflow": in_sub and "workflows" in rel[:-1],
                                     "model": model if isinstance(model, str) else None,
                                     "input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
                r["t"] = min(r["t"], t)
                u = msg["usage"]
                for k, src in _USAGE_KEYS:
                    v = u.get(src)
                    if isinstance(v, int) and v > r[k]:
                        r[k] = v
    if progress:
        progress.done(f"Scanned {len(files):,} file(s)")
    return list(reqs.values())


def _tok_total(r):
    return sum(r[k] for k in TOK)


def _bucket():
    return {"requests": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0}


def _add(b, r):
    b["requests"] += 1
    for k in TOK:
        b[k] += r[k]
    b["total"] += _tok_total(r)


def _models(counter):
    names = [m.replace("claude-", "", 1) for m, _ in sorted(counter.items(), key=lambda x: (-x[1], x[0])) if m]
    if not names:
        return "?"
    return names[0] + (f" +{len(names) - 1}" if len(names) > 1 else "")


def usage_report(reqs, spans=None, top=10, redact=False, home=None):
    """Group requests by session and by subagent. spans: [(start, end)] sleep periods, or None if unknown."""
    tot, main_b, sub_b, night_b = _bucket(), _bucket(), _bucket(), _bucket()
    sleep_b = _bucket() if spans is not None else None
    sp = sorted(spans or [])
    starts = [a for a, _ in sp]
    sessions, agents = {}, {}
    for r in sorted(reqs, key=lambda r: r["t"]):
        _add(tot, r)
        _add(sub_b if r["agent"] else main_b, r)
        if NIGHT_HOURS[0] <= r["t"].astimezone().hour < NIGHT_HOURS[1]:
            _add(night_b, r)
        if sleep_b is not None:
            i = bisect.bisect_right(starts, r["t"]) - 1
            # periods do not overlap (each ends at a Wake), but check the previous one too in case they do
            if any(0 <= j and sp[j][0] <= r["t"] < sp[j][1] for j in (i, i - 1)):
                _add(sleep_b, r)
        sk = (r["project"], r["session"])
        s = sessions.get(sk)
        if s is None:
            s = sessions[sk] = dict(_bucket(), project=r["project"], session=r["session"], first=r["t"], last=r["t"],
                                    subagent_tokens=0, subagents=set(), _models=defaultdict(int))
        _add(s, r)
        s["last"] = r["t"]
        s["_models"][r["model"]] += 1
        if r["agent"]:
            s["subagent_tokens"] += _tok_total(r)
            s["subagents"].add(r["agent"])
            ak = sk + (r["agent"],)
            a = agents.get(ak)
            if a is None:
                a = agents[ak] = dict(_bucket(), project=r["project"], session=r["session"], agent=r["agent"],
                                      agent_type=r["agent_type"] or ("workflow" if r["workflow"] else None),
                                      first=r["t"], last=r["t"], _models=defaultdict(int))
            _add(a, r)
            a["last"] = r["t"]
            a["_models"][r["model"]] += 1

    by_tok = lambda x: (-x["total"], x["first"])
    srows, arows = sorted(sessions.values(), key=by_tok), sorted(agents.values(), key=by_tok)
    names = {}
    for s in srows:                                     # project-1 = the project with the most tokens
        names.setdefault(s["project"], f"project-{len(names) + 1}")
    sids = {}

    def label(p):
        return names[p] if redact else project_label(p, home)

    def sid(x):
        if not redact:
            return x[:8]
        return sids.setdefault(x, f"s{len(sids) + 1}")

    def out(x, extra):
        o = {"project": label(x["project"]), "session": sid(x["session"]),
             "first": x["first"].astimezone().isoformat(timespec="minutes"),
             "last": x["last"].astimezone().isoformat(timespec="minutes"),
             "models": _models(x["_models"])}
        o.update({k: x[k] for k in ("requests", "total") + TOK})
        o.update(extra)
        return o

    sess_out = [out(s, {"subagents": len(s["subagents"]), "subagent_tokens": s["subagent_tokens"]})
                for s in srows[:top]]
    agent_out = [out(a, {"agent": (f"a{i}" if redact else a["agent"][:12]), "agent_type": a["agent_type"]})
                 for i, a in enumerate(arows[:top], 1)]
    return {"totals": tot, "main_sessions": main_b, "subagents": sub_b,
            "while_asleep": sleep_b, "night_local_00_07": night_b,
            "sessions_in_window": len(sessions), "subagents_in_window": len(agents),
            "top_sessions": sess_out, "top_subagents": agent_out}


def fmt_tok(n):
    if n < 10_000:
        return f"{n:,}"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            return f"{n / div:.1f}{suf}"
    return str(n)


def print_grid(head, body, left=1, also_left=()):
    """First `left` columns (and the columns in `also_left`) left-aligned, the rest right-aligned."""
    w = [max(len(head[i]), *(len(b[i]) for b in body)) if body else len(head[i]) for i in range(len(head))]
    line = lambda cells: "  ".join(c.ljust(w[i]) if i < left or i in also_left else c.rjust(w[i]) for i, c in enumerate(cells)).rstrip()
    print(line(head))
    print("  ".join("-" * x for x in w))
    for b in body:
        print(line(b))


def print_usage(rep, days, sleep_note):
    short = lambda s: s[5:16].replace("T", " ")          # MM-DD HH:MM
    tok_cells = lambda x: [f"{x['requests']:,}", fmt_tok(x["total"])] + [fmt_tok(x[k]) for k in TOK]
    tok_head = ["Requests", "Total tok", "Input", "Output", "Cache write", "Cache read"]
    print(f"lid-check --usage: last {days:g} days. Token counts from Claude Code transcripts "
          f"({rep['sessions_in_window']:,} session(s), {rep['subagents_in_window']:,} subagent(s)).")
    print()
    rows = [("All requests", rep["totals"]), ("  main sessions", rep["main_sessions"]),
            ("  subagents", rep["subagents"])]
    if rep["while_asleep"] is not None:
        rows.append(("While Mac asleep", rep["while_asleep"]))
    rows.append(("Local 00:00-07:00", rep["night_local_00_07"]))
    print_grid([""] + tok_head, [[n] + tok_cells(b) for n, b in rows])
    if rep["while_asleep"] is None:
        print(f"(While Mac asleep: not shown, {sleep_note})")
    elif sleep_note:
        print(f"(Note: {sleep_note})")
    if not rep["top_sessions"]:
        print("\nNo Claude Code requests with token counts in that window.")
        return
    print("\nTop sessions by tokens (a session's subagents are included in its row):")
    print_grid(["Project", "Session", "First", "Last"] + tok_head + ["Subagent tok", "Model"],
               [[s["project"], s["session"], short(s["first"]), short(s["last"])] + tok_cells(s)
                + [fmt_tok(s["subagent_tokens"]), s["models"]] for s in rep["top_sessions"]], left=2)
    if rep["top_subagents"]:
        print("\nTop subagents by tokens:")
        print_grid(["Project", "Session", "Agent", "Type", "First", "Last"] + tok_head + ["Model"],
                   [[a["project"], a["session"], a["agent"], a["agent_type"] or "-", short(a["first"]), short(a["last"])]
                    + tok_cells(a) + [a["models"]] for a in rep["top_subagents"]], left=4)
    print("\nNotes:")
    print("- Total tok = input + output + cache write + cache read, as recorded in the transcripts. Cache reads are")
    print("  usually most of it, so compare the columns, not only the total.")
    print("- These are transcript numbers, not billing numbers, and no dollar estimate is made.")
    print("- While Mac asleep = recorded inside a pmset sleep period. Local 00:00-07:00 is by clock time only;")
    print("  the two can overlap.")
    print("- First/Last = first and last request of that session or subagent inside the window (local time).")


# ---------- --away: what ran while nobody was typing ----------
# Away stretch = a stretch of at least --idle minutes in which you typed nothing into Claude Code or Codex.
# Inside it: which sessions still made requests, whether any session started again on its own after a usage
# limit, and the token counts. Prints session-id prefixes, times and numbers only, never message text or paths.
AWAY_CONTEXT = timedelta(hours=24)          # read this much before the window, to find limit hits that came earlier
RESUME_GAP = timedelta(minutes=5)           # a request this long after a limit message counts as "started again"
AT_RESET = (timedelta(minutes=-2), timedelta(minutes=20))   # resumed within this range of the stated reset time
CLAUDE_CONTINUE = ("Your claude.ai usage limit has reset.", "Your claude.ai usage is available again")
LIDNIGHT_RESUME = "Your usage limit has reset. Please continue the task"
TRIGGERS = {"claude-continue": "Claude Code auto-continue", "lidnight": "Lidnight resume message",
            "codex-script": "codex exec prompt", "script": "claude -p prompt", "scheduled": "scheduled task",
            "task": "task notification", "peer": "message from another session", "other": "non-typed prompt",
            None: "no prompt (unknown)"}
SHORT_TRIGGER = {"claude-continue": "auto-continue", "lidnight": "Lidnight", "codex-script": "exec",
                 "script": "claude -p", "scheduled": "scheduled", "task": "task notice", "peer": "other session",
                 "other": "other"}
LIDNIGHT_LINE = ("Lidnight, a Mac menu bar app, shows which agents are running and what they use, live; "
                 "resuming after a limit reset is a per-agent switch.")
LIDNIGHT_URL = "https://lidnight.app/?from=lid-check"
USER_LINE_RE = re.compile(rb'"type"\s*:\s*"user"')
CODEX_LINE_RE = re.compile(rb'"type"\s*:\s*"(token_count|user_message|error|turn_aborted)"|"UserMessage"')
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                        "dec"), 1)}
RESET_RE = re.compile(r"resets\s+(?:(?:on\s+)?([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s+at)?\s+)?(\d{1,2})(?::(\d\d))?\s*"
                      r"([ap]m)\b(?:\s*\(([A-Za-z_]+/[A-Za-z_/+-]+|UTC)\))?", re.I)


def _in_window(line, lo, hi):
    """False only when every timestamp on the raw line is outside [lo, hi] (keys are UTC 'YYYY-MM-DDTHH:MM:SS')."""
    vals = [v.decode("ascii", "replace") for v in TS_VALUE_RE.findall(line)]
    keys = [v[:19] for v in vals if len(v) >= 20 and v[10] == "T" and v[-1] == "Z"]
    return not (vals and len(keys) == len(vals) and (max(keys) < lo or min(keys) > hi))


def parse_reset(text, hit):
    """Reset time named in a Claude Code limit message, as UTC, or None. Two forms are known:
    'Claude AI usage limit reached|<epoch seconds>' and "...limit · resets 9am (Asia/Seoul)" / 'resets Oct 9 at 9am'."""
    m = re.search(r"limit reached\|(\d{9,11})", text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), UTC)
    m = RESET_RE.search(text)
    if not m:
        return None
    mon, day, hh, mm, ap, zone = m.groups()
    tz = hit.astimezone().tzinfo
    if zone:
        try:
            from zoneinfo import ZoneInfo
            tz = UTC if zone == "UTC" else ZoneInfo(zone)
        except Exception:
            return None
    h = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    base = hit.astimezone(tz)
    try:
        if mon:
            if mon.lower()[:3] not in _MONTHS:
                return None
            t = base.replace(month=_MONTHS[mon.lower()[:3]], day=int(day), hour=h, minute=int(mm or 0), second=0,
                             microsecond=0)
            if t < base - timedelta(days=1):
                t = t.replace(year=t.year + 1)
        else:
            t = base.replace(hour=h, minute=int(mm or 0), second=0, microsecond=0)
            if t < base - timedelta(minutes=1):
                t += timedelta(days=1)
    except ValueError:
        return None
    return t.astimezone(UTC)


def _claude_prompt_kind(d, has_origin, headless):
    """What sent a user line: 'human' (typed by you), another trigger name, or None (tool result / internal)."""
    if d.get("isMeta") or d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly") \
            or d.get("toolUseResult") is not None:
        return None
    c = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        text = " ".join(x.get("text") or "" for x in c if isinstance(x, dict) and x.get("type") == "text")
    elif isinstance(c, str):
        text = c
    else:
        return None
    s = text.lstrip()
    o = d.get("origin")
    if isinstance(o, dict) and o.get("kind") == "human":
        return "script" if headless else "human"
    # Claude Code writes its own auto-continue prompt without an origin (seen in real transcripts)
    if s.startswith(CLAUDE_CONTINUE):
        return "claude-continue"
    if s.startswith(LIDNIGHT_RESUME):
        return "lidnight"
    if isinstance(o, dict):
        k = o.get("kind")
        if k == "task-notification":
            return "scheduled" if o.get("subkind") == "scheduled-trigger" else "task"
        return "peer" if k == "peer" else "other"
    if headless:
        return "script"
    if has_origin:              # once a transcript marks typed prompts with origin, an unmarked line is internal
        return "other"
    # Older transcripts have no origin field: a plain prompt or a typed /command counts as yours.
    if s.startswith("<") and not s.startswith(("<command-name>", "<command-message>")):
        return "other"
    return "human" if (s or isinstance(c, list)) else None


def claude_away_sessions(root, lo_t, hi_t, progress=None):
    """-> {session_id: {...}} from Claude Code transcripts between lo_t and hi_t (UTC datetimes).
    Each session: tool, mode, requests [(t, tokens)], humans [t], prompts [(t, kind)], limits [(t, reset)].
    Reads only: timestamps, ids, entrypoint, origin kind, usage numbers, error markers, and the start of a
    prompt or limit message (to tell an auto-continue or a reset time apart). Nothing of it is printed."""
    lo, hi = _utc_key(lo_t), _utc_key(hi_t)
    files = _recent_files(root, lo_t.timestamp()) if os.path.isdir(root) else []
    if progress:
        progress.say(f"Scanning {len(files):,} Claude Code transcript file(s)...")
    out = {}
    for n, f in enumerate(files, 1):
        if progress:
            progress.tick(f"scanned {n - 1:,}/{len(files):,} files")
        rel = os.path.relpath(f, root).split(os.sep)
        in_sub = "subagents" in rel[:-1]
        path_session = rel[rel.index("subagents") - 1] if in_sub and rel.index("subagents") >= 1 \
            else rel[-1][:-len(".jsonl")]
        try:
            fh = open(f, "rb")
        except OSError:
            continue
        reqs, users, limits, sess, headless = {}, [], [], None, False
        with fh:
            for line in fh:
                is_user = not in_sub and USER_LINE_RE.search(line) is not None
                is_req = b'"usage"' in line and b"assistant" in line
                is_err = not in_sub and b"isApiErrorMessage" in line
                if not (is_user or is_req or is_err):
                    continue
                if is_user and not is_err and b'"origin"' not in line and b'"tool_result"' in line:
                    continue                     # a tool result, not a prompt (most user lines)
                if not _in_window(line, lo, hi):
                    continue
                try:
                    d = json.loads(line.decode("utf-8", "ignore"))
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                try:
                    t = iso(d.get("timestamp") or "")
                except ValueError:
                    continue
                if t < lo_t or t > hi_t:
                    continue
                if isinstance(d.get("sessionId"), str):
                    sess = sess or d["sessionId"]
                if d.get("entrypoint") == "sdk-cli":
                    headless = True
                if d.get("type") == "user":
                    users.append((t, d))
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                if d.get("isApiErrorMessage"):
                    text = " ".join(x.get("text") or "" for x in (msg.get("content") or [])
                                    if isinstance(x, dict))[:400] if isinstance(msg.get("content"), list) else ""
                    # Only a usage limit, named as such; a bare `rate_limit` error can also be a short retry.
                    if re.search(r"usage limit|hit your .{0,30}limit|reached your .{0,30}limit|limit reached\|",
                                 text, re.I):
                        limits.append((t, parse_reset(text, t)))
                    continue
                u = msg.get("usage")
                if not isinstance(u, dict) or msg.get("model") == "<synthetic>":
                    continue
                rid = d.get("requestId") or msg.get("id") or d.get("uuid") or len(reqs)
                r = reqs.setdefault(rid, {"t": t, "tok": dict.fromkeys(TOK, 0)})
                r["t"] = min(r["t"], t)
                for k, src in _USAGE_KEYS:
                    v = u.get(src)
                    if isinstance(v, int) and v > r["tok"][k]:
                        r["tok"][k] = v
        sid = sess or path_session
        s = out.setdefault(sid, {"tool": "Claude", "mode": "interactive", "requests": [], "humans": [],
                                 "prompts": [], "limits": []})
        if headless:
            s["mode"] = "headless"
        s["requests"] += [(r["t"], sum(r["tok"].values()), in_sub) for r in reqs.values()]
        s["limits"] += limits
        # A transcript can span a Claude Code update: lines before its first origin-marked prompt use the old rule.
        first_origin = min((t for t, d in users if isinstance(d.get("origin"), dict)), default=None)
        for t, d in users:
            k = _claude_prompt_kind(d, first_origin is not None and t >= first_origin, headless)
            if k == "human":
                s["humans"].append(t)
            elif k:
                s["prompts"].append((t, k))
    if progress:
        progress.done(f"Scanned {len(files):,} Claude Code file(s)")
    return out


def codex_away_sessions(root, lo_t, hi_t, progress=None):
    """-> {session_id: {...}} from Codex rollout files (~/.codex/sessions). Reads only: session id and source
    from the first line, timestamps, token counts and usage-limit numbers, limit error markers, and whether a
    prompt is Lidnight's resume message. Nothing of the message text is printed."""
    lo, hi = _utc_key(lo_t), _utc_key(hi_t)
    files = _recent_files(root, lo_t.timestamp()) if os.path.isdir(root) else []
    if progress:
        progress.say(f"Scanning {len(files):,} Codex session file(s)...")
    out = {}
    for f in files:
        try:
            fh = open(f, "rb")
        except OSError:
            continue
        sid, scripted = os.path.basename(f)[:-len(".jsonl")], False
        reqs, humans, prompts, limits, pct = [], [], [], [], []
        prev_total = None
        with fh:
            for i, line in enumerate(fh):
                if i == 0 and b"session_meta" in line:
                    try:
                        p = json.loads(line.decode("utf-8", "ignore")).get("payload") or {}
                        sid = p.get("id") or p.get("session_id") or sid
                        scripted = p.get("source") == "exec" or p.get("originator") == "codex_exec"
                    except (ValueError, AttributeError):
                        pass
                    continue
                if not CODEX_LINE_RE.search(line):
                    continue
                # token_count lines are small; read them all so the running total has a baseline before the window
                if b"token_count" not in line and not _in_window(line, lo, hi):
                    continue
                try:
                    d = json.loads(line.decode("utf-8", "ignore"))
                    t = iso(d.get("timestamp") or "")
                except (ValueError, AttributeError):
                    continue
                if t > hi_t or d.get("type") != "event_msg" or not isinstance(d.get("payload"), dict):
                    continue
                p = d["payload"]
                pt = p.get("type")
                if t < lo_t and pt != "token_count":
                    continue
                if pt == "token_count":
                    info = p.get("info") if isinstance(p.get("info"), dict) else {}
                    tot = (info.get("total_token_usage") or {}).get("total_tokens")
                    last = (info.get("last_token_usage") or {})
                    if isinstance(tot, int):
                        if (prev_total is None and tot > 0) or (prev_total is not None and tot != prev_total):
                            used = tot - prev_total if prev_total is not None and tot > prev_total else \
                                (last.get("input_tokens") or 0) + (last.get("output_tokens") or 0)
                            if t >= lo_t:
                                reqs.append((t, used, False))
                        prev_total = tot
                    if t < lo_t:
                        continue
                    rl = p.get("rate_limits") if isinstance(p.get("rate_limits"), dict) else {}
                    prim = rl.get("primary") if isinstance(rl.get("primary"), dict) else {}
                    if isinstance(prim.get("used_percent"), (int, float)):
                        pct.append((t, float(prim["used_percent"]), prim.get("resets_at")))
                    if rl.get("rate_limit_reached_type") or (isinstance(prim.get("used_percent"), (int, float))
                                                            and prim["used_percent"] >= 100):
                        ra = prim.get("resets_at")
                        limits.append((t, datetime.fromtimestamp(ra, UTC) if isinstance(ra, (int, float)) else None))
                elif pt in ("error", "turn_aborted"):
                    info = json.dumps(p.get("codex_error_info") or "")
                    if "usage_limit" in info or "hit your usage limit" in str(p.get("message") or ""):
                        limits.append((t, None))
                elif pt == "user_message" or (pt == "item_completed" and isinstance(p.get("item"), dict)
                                              and p["item"].get("type") == "UserMessage"):
                    c = p.get("message") if pt == "user_message" else p["item"].get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text") or "" for x in c if isinstance(x, dict))
                    if isinstance(c, str) and c.lstrip().startswith(LIDNIGHT_RESUME):
                        prompts.append((t, "lidnight"))
                    elif scripted:
                        prompts.append((t, "codex-script"))
                    else:
                        humans.append(t)
        # A limit hit has no reset time of its own; take the latest primary-window reset seen before it.
        limits = [(t, r or next((datetime.fromtimestamp(ra, UTC) for tt, _, ra in reversed(pct)
                                 if tt <= t and isinstance(ra, (int, float))), None)) for t, r in limits]
        # Several limit markers in a row are one hit: keep the first of each run with no request in between.
        keep = []
        for t, r in sorted(limits):
            if keep and not any(keep[-1][0] < rt <= t for rt, _, _ in reqs):
                continue
            keep.append((t, r))
        s = out.setdefault(sid, {"tool": "Codex", "mode": "headless" if scripted else "interactive",
                                 "requests": [], "humans": [], "prompts": [], "limits": [], "pct": []})
        s["requests"] += reqs
        s["humans"] += humans
        s["prompts"] += prompts
        s["limits"] += keep
        s["pct"] += pct
    if progress:
        progress.say(f"Scanned {len(files):,} Codex file(s)")
    return out


def away_stretches(humans, since, now, idle):
    """Stretches of at least `idle` with no typed prompt, inside [since, now]. -> [(start, end, open_start, open_end)]"""
    pts = sorted(t for t in humans if since <= t <= now)
    bounds = [since] + pts + [now]
    out = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        if b - a >= idle:
            out.append((a, b, i == 0, i == len(bounds) - 2))
    return out


def find_resumes(s, since, now):
    """Limit hits after which the session made a request again, RESUME_GAP or more later, with no prompt typed
    in between. -> [{hit, resumed, reset, at_reset, trigger, requests, tokens}] (only resumes inside the window)."""
    out = []
    reqs = sorted(s["requests"])
    humans = sorted(s["humans"])
    hits = []
    for hit, reset in sorted(s["limits"]):    # limit markers in a row with no request between are one hit
        if hits and not any(hits[-1][0] < r[0] <= hit for r in reqs):
            if hits[-1][1] is None and reset is not None:
                hits[-1] = (hits[-1][0], reset)
            continue
        hits.append((hit, reset))
    for hit, reset in hits:
        nxt = next((r for r in reqs if r[0] > hit), None)
        if nxt is None or nxt[0] - hit < RESUME_GAP or not (since <= nxt[0] <= now):
            continue
        if any(hit < h <= nxt[0] for h in humans):
            continue                              # you typed something before it started again
        trig = [k for t, k in sorted(s["prompts"]) if hit < t <= nxt[0]]
        until = next((h for h in humans if h > nxt[0]), now)
        nxt_hit = next((t for t, _ in hits if t > nxt[0]), None)
        if nxt_hit is not None:
            until = min(until, nxt_hit)
        after = [r for r in reqs if nxt[0] <= r[0] <= until]
        at_reset = None if reset is None else AT_RESET[0] <= nxt[0] - reset <= AT_RESET[1]
        out.append({"hit": hit, "resumed": nxt[0], "reset": reset, "at_reset": at_reset,
                    "trigger": trig[-1] if trig else None, "requests": len(after),
                    "tokens": sum(r[1] for r in after)})
    return out


def _pct_change(pct, a, b):
    """Codex 5-hour limit: (percent at or before a, percent at the last reading before b), only if both readings
    belong to the same limit window (same reset time). None when that cannot be told."""
    pct = sorted(pct, key=lambda x: x[0])
    before = [p for p in pct if p[0] <= a]
    inside = [p for p in pct if a < p[0] <= b]
    if not before or not inside:
        return None
    p0, p1 = before[-1], inside[-1]
    if p0[2] is None or p0[2] != p1[2]:
        return None
    return (p0[1], p1[1])


def away_report(sessions, since, now, idle, top=10):
    humans = [t for s in sessions.values() for t in s["humans"]]
    stretches = away_stretches(humans, since, now, idle)
    srows, sess_rows = [], {}
    all_pct = [p for s in sessions.values() if s["tool"] == "Codex" for p in s.get("pct", [])]
    for a, b, oa, ob in stretches:
        active, nreq, ntok, nsub = set(), 0, 0, 0
        for sid, s in sessions.items():
            rs = [r for r in s["requests"] if a < r[0] < b]
            if not rs:
                continue
            active.add(sid)
            nreq += len(rs)
            ntok += sum(r[1] for r in rs)
            row = sess_rows.setdefault(sid, {"tool": s["tool"], "mode": s["mode"], "session": sid[:8],
                                             "first": rs[0][0], "last": rs[0][0], "requests": 0, "tokens": 0,
                                             "subagent_requests": 0, "prompts": defaultdict(int)})
            row["first"] = min(row["first"], min(r[0] for r in rs))
            row["last"] = max(row["last"], max(r[0] for r in rs))
            row["requests"] += len(rs)
            row["tokens"] += sum(r[1] for r in rs)
            row["subagent_requests"] += sum(1 for r in rs if r[2])
            for t, k in s["prompts"]:
                if a < t < b:
                    row["prompts"][k] += 1
        if not active:
            continue
        # The 5-hour percentage belongs to the account, so read it across all Codex sessions together.
        srows.append({"start": a, "end": b, "open_start": oa, "open_end": ob, "sessions": len(active),
                      "requests": nreq, "tokens": ntok, "codex_5h": _pct_change(all_pct, a, b)})
    resumes = []
    for sid, s in sessions.items():
        for r in find_resumes(s, since, now):
            r.update(tool=s["tool"], session=sid[:8], mode=s["mode"])
            resumes.append(r)
    resumes.sort(key=lambda r: r["resumed"])
    rows = sorted(sess_rows.values(), key=lambda r: (-r["tokens"], r["first"]))
    for r in rows:
        r["prompts"] = {SHORT_TRIGGER.get(k, k): v for k, v in sorted(r["prompts"].items(), key=lambda x: (-x[1], x[0]))}
    tools = sorted({s["tool"] for s in sessions.values()})
    return {"stretches": srows, "sessions": rows[:top], "sessions_total": len(rows), "resumes": resumes,
            "typed_prompts": len([t for t in humans if since <= t <= now]), "tools_seen": tools,
            "tokens_total": sum(r["tokens"] for r in rows), "requests_total": sum(r["requests"] for r in rows)}


def _prompts(d):
    """{'task notice': 12, 'auto-continue': 1} -> '12 task notice, 1 auto-continue' (at most two kinds)."""
    if not d:
        return "-"
    items = list(d.items())
    out = ", ".join(f"{v} {k}" for k, v in items[:2])
    return out + (f" +{sum(v for _, v in items[2:])}" if len(items) > 2 else "")


def _loc(t, fmt="%m-%d %H:%M"):
    return t.astimezone().strftime(fmt)


def _dur(td):
    m = int(td.total_seconds() // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def print_away(rep, hours, idle, sources):
    print(f"lid-check --away: last {hours:g} h. An away stretch = {int(idle.total_seconds() // 60)}+ minutes with no "
          f"prompt typed into Claude Code or Codex.")
    print("Looked at: " + "; ".join(f"{k} {v}" for k, v in sources) +
          f" (files written in the last {hours + 24:g} h). Session ids are cut to 8 characters.")
    print()
    st = rep["stretches"]
    if not st:
        print("No session made a request while nobody was typing in that window.")
    else:
        tot_min = sum((s["end"] - s["start"]).total_seconds() for s in st) / 60
        print(f"While you were away: {rep['sessions_total']} session(s) made {rep['requests_total']:,} request(s), "
              f"{fmt_tok(rep['tokens_total'])} tokens, across {len(st)} stretch(es) ({_dur(timedelta(minutes=tot_min))}).")
        print()
        print_grid(["Away from", "Until", "Length", "Sessions", "Requests", "Tokens", "Codex 5h limit"],
                   [[_loc(s["start"]) + ("*" if s["open_start"] else ""), "now" if s["open_end"] else _loc(s["end"]),
                     _dur(s["end"] - s["start"]), str(s["sessions"]), f"{s['requests']:,}", fmt_tok(s["tokens"]),
                     (f"{s['codex_5h'][0]:g}% -> {s['codex_5h'][1]:g}%" if s["codex_5h"] else "unknown")]
                    for s in st])
        if any(s["open_start"] for s in st):
            print("* = the window starts here; you may have left earlier.")
        print()
        print("Sessions that made requests while you were away:")
        print_grid(["Tool", "Mode", "Session", "First", "Last", "Requests", "Subagent", "Tokens", "Prompts not typed"],
                   [[r["tool"], r["mode"], r["session"], _loc(r["first"]), _loc(r["last"]), f"{r['requests']:,}",
                     f"{r['subagent_requests']:,}", fmt_tok(r["tokens"]), _prompts(r["prompts"])]
                    for r in rep["sessions"]], left=3, also_left=(8,))
        if rep["sessions_total"] > len(rep["sessions"]):
            print(f"(top {len(rep['sessions'])} of {rep['sessions_total']} by tokens; --top N for more)")
    print()
    rs = rep["resumes"]
    if not rs:
        print("Started again after a usage limit, with nothing typed in between: none found.")
    else:
        print(f"Started again after a usage limit, with nothing typed in between: {len(rs)}")
        print_grid(["Tool", "Session", "Limit hit", "Reset", "Started again", "At reset?", "Trigger", "Requests",
                    "Tokens"],
                   [[r["tool"], r["session"], _loc(r["hit"]), _loc(r["reset"]) if r["reset"] else "unknown",
                     _loc(r["resumed"]), {True: "yes", False: "no", None: "unknown"}[r["at_reset"]],
                     TRIGGERS.get(r["trigger"], r["trigger"]), f"{r['requests']:,}", fmt_tok(r["tokens"])]
                    for r in rs], left=2, also_left=(6,))
        print("Requests/Tokens = from that restart until you next typed in that session (or the next limit, or now).")
    print()
    print("Notes:")
    print("- Typed prompt = a message you sent. Claude Code marks these in its transcripts (origin \"human\"); prompts")
    print("  from scheduled tasks, other sessions, `claude -p` and `codex exec` are not counted as typed. In Codex, a")
    print("  prompt in an interactive session counts as typed, so a Codex app automation can end a stretch early.")
    print("- Prompts not typed = prompts in the stretch that did not come from you (task notices, scheduled tasks,")
    print("  other sessions, auto-continue, scripts). Each can start new turns.")
    print("- Tokens = input + output + cache write + cache read for Claude Code; input (incl. cached) + output for Codex.")
    print("  Transcript numbers, not your bill. Dollar cost and Claude limit %: unknown (not in these files).")
    print("- Codex 5h limit = the percentage Codex itself recorded before and during the stretch; unknown when it")
    print("  reset in between or was not recorded.")
    print()
    print(f"{LIDNIGHT_LINE} {LIDNIGHT_URL}")


def main_away(a, prog, now):
    hours = a.hours
    since = now - timedelta(hours=hours)
    idle = timedelta(minutes=a.idle)
    sources, sessions = [], {}
    for name, root, fn in (("Claude Code", a.projects_dir, claude_away_sessions),
                           ("Codex", a.codex_dir, codex_away_sessions)):
        if not os.path.isdir(root):
            sources.append((name, "not found"))
            continue
        got = fn(root, since - AWAY_CONTEXT, now, prog)
        sources.append((name, f"{len(got):,} session(s)"))
        for sid, s in got.items():
            s.setdefault("pct", [])
            key = sid
            while key in sessions:
                key += "'"
            sessions[key] = s
    if all(v == "not found" for _, v in sources):
        home = os.path.expanduser("~")
        tilde = lambda x: "~" + x[len(home):] if home and x.startswith(home + os.sep) else x
        print(f"lid-check: neither {tilde(a.projects_dir)} nor {tilde(a.codex_dir)} exists "
              "(use --projects-dir / --codex-dir if yours are elsewhere)", file=sys.stderr)
        return 1
    rep = away_report(sessions, since, now, idle, top=a.top)
    if a.json:
        iso_ = lambda t: t.astimezone().isoformat(timespec="minutes") if t else None
        j = {"meta": {"hours": hours, "idle_minutes": a.idle, "sources": dict(sources),
                      "typed_prompts_in_window": rep["typed_prompts"]},
             "stretches": [dict(s, start=iso_(s["start"]), end=iso_(s["end"])) for s in rep["stretches"]],
             "sessions": [dict(r, first=iso_(r["first"]), last=iso_(r["last"])) for r in rep["sessions"]],
             "sessions_total": rep["sessions_total"], "requests_total": rep["requests_total"],
             "tokens_total": rep["tokens_total"],
             "resumes": [dict(r, hit=iso_(r["hit"]), resumed=iso_(r["resumed"]), reset=iso_(r["reset"]),
                              trigger=TRIGGERS.get(r["trigger"], r["trigger"])) for r in rep["resumes"]],
             "lidnight": LIDNIGHT_URL}
        print(json.dumps(j, indent=1))
        return 0
    print_away(rep, hours, idle, sources)
    return 0


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


def main_usage(a, prog, ev, log_first, since, now):
    if not os.path.isdir(a.projects_dir):
        print(f"lid-check: Claude Code folder not found: {a.projects_dir} "
              "(use --projects-dir if yours is elsewhere)", file=sys.stderr)
        return 1
    spans, note = None, ""
    if a.no_pmset:
        note = "--no-pmset was given"
    elif log_first is None:
        note = "no pmset log was readable"
    else:
        if log_first > since:   # the log does not reach back to the start of the window
            note = f"the pmset log starts {log_first.astimezone().isoformat(timespec='minutes')}; " \
                   "sleep before that is not known, so 'While Mac asleep' can be too low"
        ps = [p for p in sleep_periods(ev, now) if p["end"] > since]
        spans = [(p["start"], p["end"]) for p in ps if (p["end"] - p["start"]) >= timedelta(minutes=a.min_minutes)]
    reqs = usage_requests(a.projects_dir, since, now, prog)
    rep = usage_report(reqs, spans, top=a.top, redact=a.redact)
    if a.json:
        rep["meta"] = {"days": a.days, "sleep_periods": len(spans) if spans is not None else None,
                       "sleep_note": note or None}
        print(json.dumps(rep, indent=1))
        return 0
    print_usage(rep, a.days, note)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="List the times your Mac was asleep (from `pmset -g log`) and count the Claude Code requests "
                    "recorded inside them (from ~/.claude/projects). Read-only: no network, no files written.",
        epilog="examples:\n  python3 lid_check.py                      last 7 days\n"
               "  python3 lid_check.py --days 14 --lid-only only sleeps that began with a lid close\n"
               "  python3 lid_check.py --json                same numbers as JSON\n"
               "  python3 lid_check.py --usage --days 3     tokens per session and subagent, last 3 days\n"
               "  python3 lid_check.py --away               sessions that ran while you typed nothing, last 24 h",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=7, help="how many days back to look (default 7)")
    ap.add_argument("--min-minutes", type=float, default=5, help="ignore sleep periods shorter than this (default 5)")
    ap.add_argument("--lid-only", action="store_true", help="only periods that began with a lid-close sleep")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument("--projects-dir", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"),
                    help="Claude Code transcript folder (default ~/.claude/projects)")
    ap.add_argument("-q", "--quiet", action="store_true", help="no progress messages on stderr")
    ap.add_argument("--pmset-log", help="read this saved `pmset -g log` text file instead of running pmset")
    g = ap.add_argument_group("token usage (--usage)")
    g.add_argument("--usage", action="store_true",
                   help="token counts per session and subagent instead of the sleep table (shows project folder names)")
    g.add_argument("--top", type=int, default=10, help="rows in each --usage table (default 10)")
    g.add_argument("--redact", action="store_true", help="--usage: print project-1, s1, a1 instead of names and ids")
    g.add_argument("--no-pmset", action="store_true", help="--usage: skip the pmset log (no 'While Mac asleep' row)")
    w = ap.add_argument_group("while you were away (--away)")
    w.add_argument("--away", action="store_true",
                   help="sessions that made requests while you typed nothing, restarts after a usage limit, tokens "
                        "(Claude Code and Codex; no pmset needed)")
    w.add_argument("--hours", type=float, default=24, help="--away: how many hours back to look (default 24)")
    w.add_argument("--idle", type=float, default=60,
                   help="--away: minutes with no typed prompt that count as away (default 60)")
    w.add_argument("--codex-dir", default=os.path.join(os.path.expanduser("~"), ".codex", "sessions"),
                   help="Codex session folder (default ~/.codex/sessions)")
    ap.add_argument("--now", help=argparse.SUPPRESS)     # tests: fixed 'now' (ISO time)
    a = ap.parse_args(argv)
    if not a.days > 0:
        ap.error("--days must be greater than 0 (for example --days 7)")
    if a.min_minutes < 0:
        ap.error("--min-minutes must be 0 or more")
    if a.top < 1:
        ap.error("--top must be 1 or more")
    if not a.hours > 0:
        ap.error("--hours must be greater than 0 (for example --hours 24)")
    if not a.idle > 0:
        ap.error("--idle must be greater than 0 (for example --idle 60)")

    prog = Progress(a.quiet)
    now = iso(a.now) if a.now else datetime.now(UTC)
    since = now - timedelta(days=a.days)
    if a.away:
        return main_away(a, prog, now)

    if a.usage and a.no_pmset:
        text = ""
    elif a.pmset_log:
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
    if a.usage:
        return main_usage(a, prog, ev, log_first, since, now)
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
