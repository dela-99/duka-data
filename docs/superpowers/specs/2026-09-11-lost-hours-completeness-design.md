# Lost-hour completeness: ledger, repair, Sunday sessions, and a real coverage check

**Date:** 2026-09-11
**Issue:** https://github.com/saleem-latif/duka-data/issues/3
**Status:** approved design, awaiting implementation plan

## Problem

`download.py` gives up on an hour file after 4 retries (about 19 s total) on a
non-404 HTTP error, returns an empty frame, and the day completes normally. The
hour vanishes from M5 and therefore from H1/H4/D1, which are resampled from M5.
`days_failed` stays 0 because it counts exceptions, not lost hours.

`check_integrity.py` compares the four timeframes against each other, so a
missing hour is consistent everywhere and 11/11 checks pass. Verified by
injecting 66 gaps into EURUSD: identical ratios, exit 0.

Investigation on 2026-09-11 established four facts that shape the design:

| Fact | Evidence | Consequence |
|---|---|---|
| 503 give-ups are transient | 6/6 lost EURUSD hours re-fetched fine on first try | Repair is viable |
| "No data" is distinguishable from "failed" | Dukascopy returns an empty 200 body for holidays/weekends/after-close; 404 only for future dates | A per-hour ledger can record `empty` vs `failed` |
| 503s are bursty | ~50% of give-ups in the May run sat in bursts of 3+ within 60 s; 4-attempt ladder gives up in 19 s | Longer ladder + end-of-run sweep recovers most |
| Sunday sessions are never fetched | Downloader iterates Mon–Fri; FX has ticks from Sunday 21:00 (summer) / 22:00 (winter) UTC; shipped H1 has 0 Sunday bars | ~2,000–3,000 hours per symbol missing by design, far more than the ~565 hours lost to 503 across 14 symbols |

There is also no repair path: a narrow non-incremental `--start/--end` run
replaces the whole symbol CSV, and `--incremental` only fetches days after the
last date.

## Decisions (made with the maintainer)

1. **Fetch Sundays, keep UTC calendar days.** D1 gets a short Sunday bar each
   week. Documented; consumers can drop or merge it.
2. **Per-hour fetch ledger** is the source of truth for what needs fetching.
3. **Every run merges** into the existing compiled data. `--fresh` opts out.
4. **Warn and exit non-zero** when hours remain lost after the sweep.
5. **Backfill the local dataset and regenerate docs** as part of this work.
6. **Approach:** ledger lives inside `download.py` as a per-symbol CSV; one
   fetch/merge/resample path serves normal, incremental, and repair runs.

## Design

### 1. Session calendar

- Replace `_trading_days(start, end)` (Mon–Fri) with `_session_days(start, end)`
  yielding Sunday through Friday.
- `SUNDAY_FIRST_HOUR = 20`: on Sundays only hours 20–23 are requested. Weekdays
  request all 24 hours as today (Friday 22–23 are cheap empty fetches).
- Resampling is unchanged: UTC calendar days. A Sunday D1 bar covering
  21:00/22:00–23:59 appears each week.

### 2. Per-hour fetch result and retry ladder

- `download_hour_bi5(pair, dt)` returns `FetchResult(status, data)` where
  `status` is one of `"data" | "empty" | "notfound" | "failed"`:
  - HTTP 200 with a non-empty body → `data`
  - HTTP 200 with an empty body → `empty`
  - HTTP 404 → `notfound`
  - any other HTTP error or exception after all attempts → `failed`
- Retry ladder: `RETRY_DELAYS = (2, 4, 8, 16, 32, 60)` seconds, i.e. 7
  attempts, ~2 minutes total. Sized to the observed 60 s burst windows.
- Transient give-ups stay at DEBUG. The final list of lost hours after the sweep
  (section 3) is logged at WARNING.
- `_download_and_decode_hour` returns `(hour, status, tick_df)`.
- `download_day_ticks` returns `(m5_df, tick_count, hour_status: dict[int, str])`.

### 3. Ledger, sweep, merge, repair

**Ledger file.** `ledger/<SYM>.csv`, gitignored, columns `hour,status`, one row
per attempted hour, `hour` as ISO-8601 UTC (`2024-03-05T07:00:00Z`), `status`
as a single letter `D` (data), `E` (empty), `N` (not found), `F` (failed).
Sorted by hour. Loaded into a `dict[Timestamp, str]` at symbol start and written
back in the same step that writes the compiled CSVs. Roughly 175k rows / 4 MB
per symbol for 20 years including Sundays.

**Sweep.** After the main day pool finishes, collect every hour whose status is
`F` from this run and re-fetch each one sequentially (concurrency 1) with the
full ladder. Update statuses. Hours still `F` are "lost".

**Merge.** If `compiled/<SYM>_M5.csv` exists, the new M5 frame is concatenated
with it, deduplicated on `time` keeping the new run's row, sorted, and written.
H1/H4/D1 are re-resampled from the merged M5 as today. `--fresh` deletes the
symbol's compiled files and ledger before downloading. `--incremental` keeps
its meaning: start at the day after the ledger's last `D`/`E` hour (falls back
to `.download_meta.json` when no ledger exists).

**Repair (`--repair`).** For each symbol:

1. If no ledger exists, seed one from `compiled/<SYM>_H1.csv`: every hour with
   an H1 bar → `D`. Nothing else is written, so holidays, Sundays, and lost hours
   are all "never attempted".
2. Covered range = `[ledger min hour, ledger max hour]`, narrowed by
   `--start/--end` if given.
3. Work list = every hour with status `F` + every expected session hour in the
   covered range with no ledger row. Expected session hours are Sunday 20–23
   and Monday–Friday 0–23 (Friday 21–23 included; they come back `E` once and
   are then never re-fetched).
4. Fetch the work list through the same day-grouped pool, sweep, merge, resample,
   write ledger. Nothing else differs from a normal run.

Legacy seeding fetches ~3,500 hours per symbol once (≈1,000 Sundays × 4 hours,
~400 holiday hours, ~50 lost hours); subsequent repairs touch only `F` rows.

**Reporting.** Progress line and `.download_status.json` replace `days_failed`
with `hours_failed` (give-ups during the main pass), `hours_empty`, and
`hours_lost` (still `F` after the sweep). End-of-symbol summary prints
`hours_lost` and, if non-zero, the hour list at WARNING with the hint
`run: python3 download.py --repair --symbols <SYM>`. `run()` returns the total
`hours_lost` across symbols; `main()` exits 1 if it is non-zero.

**CLI additions.**

| Flag | Meaning |
|---|---|
| `--repair` | Fetch only `F` and never-attempted session hours in the covered range |
| `--fresh` | Delete the symbol's compiled files and ledger first, then download |
| `--clear` | Delete the selected symbols' compiled files and ledger, then exit without downloading |

| `--yes` | Skip the destructive-action confirmation (for scripts and the dashboard) |

`--repair` composes with `--symbols`, `--start`, `--end`. `--repair`,
`--fresh`, `--clear`, and `--incremental` are mutually exclusive with each
other. `--clear` ignores `--start/--end`, prints what it deleted, and never
touches `all_pairs_M5.csv` (rebuilt by the next full run).

**Destructive-action confirmation.** `--clear` and `--fresh` both delete data
that took hours to download and cannot be recovered except by re-downloading.
Before deleting, `download.py` prints the exact files it is about to remove
with their sizes and a backup reminder, then asks for confirmation:

```
About to DELETE compiled data and ledger for EURUSD, GBPUSD:
  compiled/EURUSD_M5.csv   98.1 MB
  compiled/EURUSD_H1.csv    8.4 MB
  ...
  ledger/EURUSD.csv         3.9 MB
This cannot be undone. Back up compiled/ and ledger/ first if you need them
(e.g. cp -r compiled ledger ~/duka-backup-2026-09-11).
Type DELETE to continue:
```

Anything other than the literal `DELETE` aborts with exit 2 and no changes.
`--yes` skips the prompt but still prints the file list and reminder. If stdin
is not a TTY and `--yes` is absent, the command aborts with exit 2 and a
message saying to pass `--yes`; it never deletes on a silent default.

### 4. Checker (`check_integrity.py`)

- **New check 12, "Session-hour completeness" (gated).** With a ledger: FAIL if
  any row is `F`, or any expected session hour inside the covered range has no
  row. Print offenders grouped into contiguous runs (`start -> end (Nh)`), first
  40 runs, with a total. Without a ledger: print the missing expected session
  hours the same way as informational, followed by
  `no ledger: run python3 download.py --repair --symbols <SYM> to build one`.
- **Check 4** computes M5/H1/H4/D1 counts over Monday–Friday bars only, so the
  short Sunday bars do not skew H4/D1. Bands unchanged.
- **Check 8** stays weekday-only, informational.
- `--data-dir <path>` overrides the base directory; `COMPILED_DIR` and
  `LEDGER_DIR` derive from it. Symbol discovery moves from import time into
  `main()` so the module is importable in tests.
- Docstring header lists check 12; summary line becomes `N/12`.

### 5. Documentation and backfill

- **USAGE.md:** new sections for the ledger (location, format, statuses),
  `--repair`, `--fresh`, exit codes, the Sunday D1 bar, and "how to know an hour
  was lost" (`hours_lost` in the progress line / status file, WARNING list).
  Update the Performance and Monitoring sections for the new fields.
- **README.md:** tooling table mentions the ledger; Quick start notes the
  non-zero exit and `--repair`; the Dashboard section lists the run modes and
  the Clear button.
- **Backfill (after code lands):** `python3 download.py --repair` for all 14
  symbols; `python3 make_manifest.py`; rewrite `DATA_QUALITY.md` with the new
  completeness numbers (Sunday sessions present, 503 losses repaired, checker
  12/12) and a short account of the 2026-09-11 finding.
- `.gitignore`: add `ledger/`.

### 6. Testing

Add `tests/` (pytest) and `requirements-dev.txt` (`pytest`). No network: a
`fake_feed` fixture monkeypatches `urllib.request.urlopen` with a per-hour
script (`data` → fabricated LZMA bi5 bytes, `empty` → zero-length body,
`notfound` → 404, `fail` → 503, optionally `fail` for the first *k* attempts
then `data`) and stubs `time.sleep`. Tests point `COMPILED_DIR`, `LEDGER_DIR`,
`META_FILE`, `STATUS_FILE` at `tmp_path`.

`tests/test_download.py`:

1. A hour that 503s on every attempt → status `F` in the returned map and the
   ledger; day still completes; `hours_failed == 1`.
2. A hour that 503s three times then succeeds → status `D`, bars present.
3. Sweep recovers a hour that failed in the main pass but succeeds on the
   sweep → ledger `D`, `hours_lost == 0`.
4. Empty hours → `E`; a second run over the same range makes zero requests for
   them.
5. Narrow re-run over one day merges: all original days remain in M5, H1/H4/D1
   re-resampled, no duplicate timestamps.
6. `--fresh` removes existing files and ledger before downloading.
7. Repair with no ledger seeds `D` from H1 and fetches only never-attempted and
   `F` hours; repair with a ledger fetches only `F` rows.
8. Sundays request hours 20–23 only; weekdays request 0–23; Saturdays none.
9. `run()` returns `hours_lost`; `main()` exit code is 1 when non-zero, 0
   otherwise.
10. Any two of `--repair`, `--fresh`, `--clear`, `--incremental` together are
    rejected by the parser.
11. `--clear --yes` deletes the selected symbols' compiled files and ledger,
    leaves other symbols and `all_pairs_M5.csv` alone, makes no HTTP requests,
    and exits 0.
12. `--clear` / `--fresh` on a TTY (stdin monkeypatched): typing `DELETE`
    proceeds; any other input aborts with exit 2 and every file intact. The
    printed prompt lists each file with its size and the backup reminder.
13. `--clear` / `--fresh` with non-TTY stdin and no `--yes` aborts with exit 2,
    deletes nothing, and the message mentions `--yes`.

`tests/test_check_integrity.py` (uses the gapped-EURUSD builder from the
investigation, scaled to a few weeks of synthetic M5):

1. Gapped data + ledger with `F` rows → check 12 FAIL, exit 1.
2. Gapped data, no ledger → check 12 informational, lists the gaps, run passes.
3. Complete data + ledger → check 12 PASS.
4. Check 4 ignores Sunday bars (a dataset with Sunday D1 bars still passes the
   FX ratio bands).
5. `--data-dir` is honoured.

`tests/test_dashboard.py` (`subprocess.Popen` monkeypatched to capture argv;
`running_download_pids` monkeypatched):

1. Each `mode` builds the expected argv: `download` → no flag, `incremental` →
   `--incremental`, `repair` → `--repair`, `fresh` → `--fresh`.
2. `repair` with empty start/end omits `--start/--end`; other modes still
   require valid dates.
3. Unknown `mode` is rejected; missing `mode` defaults to `download`.
4. `/clear` builds `download.py --clear --yes --symbols ...`, rejects invalid
   symbols, and is refused while a download is running.
5. `fresh` mode appends `--yes`; no other mode does.
6. `/files` lists only existing compiled and ledger files for the requested
   symbols with byte sizes, and rejects invalid symbols.

### 7. Dashboard (`dashboard.py`)

The dashboard stays a thin launcher: every action maps to exactly one
`download.py` invocation, and the dashboard never deletes files itself.

**Run mode.** The Incremental checkbox becomes a radio group:

| Mode | argv addition | Notes |
|---|---|---|
| Download (merge) | none | Default. New hours merge into existing data. |
| Incremental | `--incremental` | Days after the last ledger date only. |
| Repair only | `--repair` | Date fields optional; if set they narrow the range. |
| Re-run from scratch | `--fresh --yes` | Backup warning dialog (below) before POST. |

**Clear existing data.** A separate button next to Start, not a run mode. After
the backup warning dialog, it POSTs to a new `/clear` endpoint, which reuses
the symbol validation from `start_download` and spawns
`download.py --clear --yes --symbols ...` detached, logging to the same
`dashboard_run_*.log` pattern. Refused while a download is running, exactly
like `/start`.

**Backup warning dialog.** Both destructive actions (Clear, Re-run from
scratch) open the same in-page modal, not a bare `confirm()`. It lists the
files that will be deleted with sizes (fetched from a new `GET
/files?symbols=...` endpoint that reports existing compiled and ledger files
per symbol), states that the data cannot be recovered without re-downloading,
and shows the backup hint with a copyable command
(`cp -r compiled ledger ~/duka-backup-<date>`). The confirm button is disabled
until the user types `DELETE` into a text field, mirroring the CLI. Cancel
closes the modal with no request sent. The dashboard passes `--yes` because the
detached child has no TTY; the modal is the confirmation.

**Server side.** `start_download` reads `mode` (default `"download"`) and
validates it against the four allowed values; `start`/`end` validation is
skipped for `repair` when both are empty; `fresh` appends `--yes`. The launch
log line includes the mode. `/clear` is a second POST route and `/files` a new
GET route; `do_POST`/`do_GET` dispatch on path. `/files` validates symbols the
same way and returns `{symbol: [{file, bytes}]}` for files that exist.

**Progress panel.** Two new stats, *Hours lost* and *Hours empty*, from the
new status fields. When `state == "completed"` and `hours_lost > 0`, the state
badge reads `completed · N hours lost` in the warning colour and the message
area shows "Run *Repair only* for these symbols". The days-based progress bar
is unchanged; repair runs still report days.

## Out of scope

- Persisting partial progress if a run is killed mid-symbol (existing
  limitation, unchanged).
- Rolling the Sunday session into Monday's D1 bar (decided against).
- Changing concurrency (`DAY_WORKERS`, `HOUR_WORKERS`).
- The `raw/` directory and `all_pairs_M5.csv` generation (unchanged; the
  combined file is rebuilt from the merged per-symbol M5 as today).
- Dashboard changes beyond section 7 (no per-symbol progress, no log
  filtering, no ledger browser).

## Rollout

1. Land code + tests + docs on branch `lost-hours-completeness`.
2. Run `--repair` for all symbols locally; run the checker; regenerate manifest
   and quality report.
3. Reply on issue #3 and to the Medium reader with the `hours_lost` /
   `--repair` guidance.
