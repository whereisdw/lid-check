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

Options: `--days N` (default 7), `--lid-only`, `--min-minutes N` (ignore short sleeps, default 5), `--json`, `--projects-dir DIR` (default `~/.claude/projects`), `--pmset-log FILE`, `--usage` (see below), `--quiet` (no progress messages; they go to stderr, so the table and `--json` on stdout are unchanged).
Works with the python3 that ships with macOS (tests pass on 3.9 and 3.15). Bad input (a missing folder or file, `--days 0`) prints one line saying what is wrong.

Timing, measured once on one busy Mac with a 14 GB `~/.claude/projects` (about 7,700 transcript files, many with multi-MB lines), against a test pmset log with a nightly 8-hour sleep: the transcript scan took 20-30 s for `--days 7` and about 30 s for `--days 30`, down from about 1 to 2 minutes in the first version (run times on that machine varied by a factor of two between runs). With a single sleep period 3 days ago it took about 10 s. Separately, `pmset -g log` alone took 8 s once here and about a minute at another time; macOS builds that log on demand and this tool cannot speed it up. So the first line it prints, on stderr, says it is reading the pmset log. These are single runs on one machine, not benchmarks; a smaller folder is much faster. With no sleep period in the window the transcripts are not read at all. Progress goes to stderr (`--quiet` turns it off), so the table and `--json` on stdout are unchanged. Use `--projects-dir` on a copy of one project to try it quickly.

To stay fast it skips transcript files last modified before the window, files whose first timestamps are more than a day after the last sleep period, and any line timestamped more than 10 minutes away from a sleep period, without parsing them. Transcripts are append-only, so these cannot hold a line inside a period. The numbers are the same as a full read (the tests check that).

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

## Where did the tokens go? (`--usage`)

`--usage` adds up the token counts Claude Code writes into its transcripts and shows which sessions and which subagents used the most, plus how much was recorded while the Mac was asleep (from the same `pmset` log) or between 00:00 and 07:00 local time.

```sh
python3 lid_check.py --usage              # last 7 days
python3 lid_check.py --usage --days 1 --top 20
python3 lid_check.py --usage --redact     # project-1, s1, a1 instead of names and ids
```

Example (**synthetic** transcripts and pmset lines, the same ones the tests use; numbers are made up):

```text
lid-check --usage: last 3650 days. Token counts from Claude Code transcripts (2 session(s), 3 subagent(s)).

                   Requests  Total tok  Input  Output  Cache write  Cache read
-----------------  --------  ---------  -----  ------  -----------  ----------
All requests              8     328.8k     22   1,185        6,000      321.6k
  main sessions           3      21.2k     11     151        1,000       20.0k
  subagents               5     307.6k     11   1,034        5,000      301.6k
While Mac asleep          3     326.2k      5   1,120        5,000      320.1k
Local 00:00-07:00         2     306.1k      4   1,020        5,000      300.1k
(Note: the pmset log starts 2026-11-10T22:00+09:00; sleep before that is not known, so 'While Mac asleep' can be too low)

Top sessions by tokens (a session's subagents are included in its row):
Project     Session         First         Last  Requests  Total tok  Input  Output  Cache write  Cache read  Subagent tok         Model
----------  --------  -----------  -----------  --------  ---------  -----  ------  -----------  ----------  ------------  ------------
dev-webapp  11111111  11-10 21:00  11-11 08:31         7     328.8k     22   1,184        6,000      321.6k        307.6k  opus-test +1
dev-api     99999999  11-10 21:30  11-10 21:30         1         11      0       1            0          10             0     opus-test

Top subagents by tokens:
Project     Session   Agent      Type                   First         Last  Requests  Total tok  Input  Output  Cache write  Cache read        Model
----------  --------  ---------  ---------------  -----------  -----------  --------  ---------  -----  ------  -----------  ----------  -----------
dev-webapp  11111111  aaa111     general-purpose  11-11 00:00  11-11 08:31         3     307.0k      9   1,011        5,000      301.0k  sonnet-test
dev-webapp  11111111  sidechain  -                11-11 08:00  11-11 08:00         1        505      2       3            0         500    opus-test
dev-webapp  11111111  bbb222     workflow         11-11 01:00  11-11 01:00         1        120      0      20            0         100    opus-test
```

- Requests are counted once per API request id (Claude Code writes one line per streamed block). Lines marked `<synthetic>` or as API errors are not counted.
- Subagents are found from `subagents/` transcript paths and the `isSidechain` / `agentId` fields; Type comes from the subagent's `.meta.json` (`agentType` only), or `workflow` for workflow agents without one.
- Project is the transcript folder name with your home-folder part removed. Session and Agent are the first characters of their ids.
- Tokens only. There is no dollar estimate: prices and how usage limits weigh each token type change, and transcript numbers are not billing numbers.
- "While Mac asleep" needs the `pmset` log to cover the window; if it starts later, a note says so. `--no-pmset` skips it. `--json` prints the same numbers.
- On the 14 GB folder described above, `--usage --days 7` (about 3,000 recently modified files) took about 25 s plus the `pmset` read; `--days 1` took about 6 s. Single runs, not benchmarks.

## Free ways to keep the Mac awake

From `man caffeinate`:

- `caffeinate -i <command>` holds an assertion that prevents idle sleep while the command runs, for example `caffeinate -i claude`.
- `caffeinate -s` prevents system sleep, but only while on AC power.

The man page does not say whether these cover closing the lid. Run `lid_check.py` after a test to see.

`sudo pmset -a disablesleep 1` is widely used to stay awake with the lid closed. It is not documented in `man pmset`. Check it with `pmset -g | grep SleepDisabled` and undo it with `sudo pmset -a disablesleep 0`.

## What it reads, and what it never does

- Runs `pmset -g log` (read-only). `--pmset-log FILE` reads a saved copy of that output instead.
- Reads `~/.claude/projects/**/*.jsonl` for only: the timestamp, the request id, the cache token counts, and whether an entry is an API error.
- Does not print or store conversation text, file names, or paths. Project folder names (home-folder part removed) are printed only by `--usage`, and `--redact` hides them.
- With `--usage` it also reads the model name and input/output token counts, and `agentType` from subagent `.meta.json` files.
- No network calls, no files written, no settings changed, no dependencies. `--json` prints the same numbers.
- You can verify this: the file is about 730 lines, and `grep -n "import" lid_check.py` shows the only modules it uses.

Tests: `python3 -m unittest discover -s tests`

## License and author

MIT, see `LICENSE`.

Made by whereisdw, who also makes [Lidnight](https://lidnight.app/?from=lid-check), a Mac app that keeps a MacBook awake with the lid closed while AI coding agents run (free tier, paid Pro for lid-closed mode). lid-check is separate and does not need it.
