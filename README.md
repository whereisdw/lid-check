# lid-check

Check whether Claude Code requests were recorded while your MacBook was asleep or the lid was closed.

One file, Python standard library only, macOS. It reads two local sources and prints times and counts. It does not touch the network, write files, or change any setting.

## What it checks

1. Finds sleep periods in `pmset -g log`: from a `Sleep` event to the next full `Wake`, counting the `DarkWake` events in between. A period is marked `closed` in the Lid column when its first Sleep was a lid-close sleep ("Clamshell Sleep").
2. For each period, counts Claude Code requests recorded in `~/.claude/projects/**/*.jsonl` with a timestamp inside it, how many fall inside a DarkWake window, and how many tokens went to re-writing the prompt cache.

## Run it

Download, read, then run:

```sh
curl -fsSLO https://raw.githubusercontent.com/whereisdw/lid-check/main/lid_check.py
less lid_check.py
python3 lid_check.py
```

Or in one step, if you trust the source:

```sh
curl -fsSL https://raw.githubusercontent.com/whereisdw/lid-check/main/lid_check.py | python3 -
curl -fsSL https://raw.githubusercontent.com/whereisdw/lid-check/main/lid_check.py | python3 - --days 14 --lid-only
```

Options: `--days N` (default 7), `--lid-only`, `--min-minutes N` (ignore short sleeps, default 5), `--json`, `--projects-dir DIR` (default `~/.claude/projects`), `--pmset-log FILE`.
Works with the python3 that ships with macOS (tests pass on 3.9 and 3.15). Bad input (a missing folder or file, `--days 0`) prints one line saying what is wrong.

Timing, measured once on one Mac with a 14 GB `~/.claude/projects` (about 7,700 transcript files): when sleep periods exist, reading the transcripts took about 4 minutes for `--days 7` and about 6.5 minutes for `--days 30` (60,000 and 138,000 requests read). Separately, `pmset -g log` alone took about a minute there. With no sleep period in the window the transcripts are not read at all. These are single runs on one machine, not benchmarks; a smaller folder is much faster. Use `--projects-dir` on a copy of one project to try it quickly.

## Example output

The `pmset` lines below are real: a block from a public issue report ([AprilNEA/OpenLogi#1281](https://github.com/AprilNEA/OpenLogi/issues/1281)), saved unchanged in `examples/pmset-sample.log`. The Claude Code side is **synthetic**: five made-up transcript entries in `examples/claude-projects/` (request id, timestamp and cache token counts only, no message text), because no real transcripts are public. So the pmset lines are real and the transcript side is synthetic; the table shows how to read the output, not what a typical Mac does.

```sh
TZ=UTC python3 lid_check.py --pmset-log examples/pmset-sample.log --projects-dir examples/claude-projects --days 3650
```

```text
lid-check: last 3650 days. pmset log starts 2026-09-17T16:46+00:00.

Start (local)       Min     Lid  DarkWakes  Requests  In DarkWake  Errors  Rewrite tok  Rewrite tok <10m after wake
----------------  -----  ------  ---------  --------  -----------  ------  -----------  ---------------------------
2026-09-17 16:46  495.4  closed          2         3            3       0       45,000                       42,000

1 sleep period(s), 1 began with a lid-close sleep. Requests recorded inside them: 3 (3 inside DarkWake windows).
Notes:
- Requests = Claude Code transcript entries timestamped inside the period. Time is when it was recorded.
- In DarkWake = recorded between a DarkWake and the next Sleep/Wake (90 s if none follows).
- Rewrite tok = cache-write tokens of a request that came after 5+ idle minutes and re-sent a 20,000+ token
  prompt without reading it from cache. This also happens when the Mac never slept.
- This shows what was recorded. It does not show how much of any usage limit was used.
```

The report's author cut the overnight Sleep/DarkWake cycles out of that log, so the last DarkWake (00:47) is followed directly by the final Wake (09:01). The "In DarkWake" window therefore runs for hours here; in an unedited log it is a few seconds to a few minutes. (`TZ=UTC` just makes the times reproducible; without it times are shown in your local zone, and `--days 3650` is needed only because the sample is old.)

## Reading the output

| Column | Meaning |
|---|---|
| Start, Min | When the sleep period began (local time) and how long it lasted |
| Lid | `closed` if the first Sleep was caused by closing the lid |
| DarkWakes | `DarkWake` events inside the period (brief wakes the Mac makes while "asleep") |
| Requests | Claude Code requests recorded with a timestamp inside the period. The timestamp is when the transcript entry was written |
| In DarkWake | Of those, requests (and API errors) recorded inside a DarkWake window: from a DarkWake to the next Sleep or Wake, or 90 s if none follows |
| Errors | API error/retry entries recorded inside the period |
| Rewrite tok | Cache-write tokens of requests that came after 5+ idle minutes and re-sent a 20,000+ token prompt with under 10% read from cache |
| Rewrite tok < 10m after wake | The same count for requests in the 10 minutes after the period ended |

What this tells you: whether the transcripts contain requests stamped inside the times your Mac was recorded as asleep.

What it does not tell you:

- How much of your usage limit was used. Token counts here are transcript numbers, not billing numbers.
- Why a request happened. Rewrite tokens also occur when the Mac never slept, after any 5+ minute idle gap.
- Anything about periods the `pmset` log no longer covers (it only keeps a limited history), or about a Mac that was kept awake with the lid closed: no sleep, no period.
- A guarantee that every `pmset` line is read correctly. The parser is tested against real log lines taken from public issue reports (`tests/fixtures/`), but it has had few end-to-end runs on Macs that actually slept. If a period looks wrong, please open an issue with the lines around it.

If it prints "No sleep periods found", the log has no Sleep events in that window.

## Free ways to keep the Mac awake

From `man caffeinate`:

- `caffeinate -i <command>` holds an assertion that prevents idle sleep while the command runs, for example `caffeinate -i claude`.
- `caffeinate -s` prevents system sleep, but only while on AC power.

The man page does not say whether these cover closing the lid. Run `lid_check.py` after a test to see.

`sudo pmset -a disablesleep 1` is widely used to stay awake with the lid closed. It is not documented in `man pmset`. Check it with `pmset -g | grep SleepDisabled` and undo it with `sudo pmset -a disablesleep 0`.

## What it reads, and what it never does

- Runs `pmset -g log` (read-only). `--pmset-log FILE` reads a saved copy of that output instead.
- Reads `~/.claude/projects/**/*.jsonl` for only: the timestamp, the request id, the cache token counts, and whether an entry is an API error.
- Does not print or store conversation text, project names, file names, or paths.
- No network calls, no files written, no settings changed, no dependencies. `--json` prints the same numbers.
- You can verify this: the file is about 330 lines, and `grep -n "import" lid_check.py` shows the only modules it uses.

Tests: `python3 -m unittest discover -s tests`

## License and author

MIT, see `LICENSE`.

Made by whereisdw, who also makes [Lidnight](https://lidnight.app/), a Mac app that keeps a MacBook awake with the lid closed while AI coding agents run (free tier, paid Pro for lid-closed mode). lid-check is separate and does not need it.
