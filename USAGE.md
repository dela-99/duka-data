# duka-data — Dukascopy Tick Downloader & OHLC Compiler

Direct bi5 tick downloader that fetches raw tick data from Dukascopy's public datafeed, decodes LZMA-compressed bi5 files, and resamples into M5/H1/H4/D1 OHLC bars with bid/ask spread data.

No external trading libraries required — only pandas and numpy.

## Quick Start

```bash
pip install -r requirements.txt
python3 download.py
```

This downloads 5 years of data for 10 major/cross pairs across 4 timeframes.

## CLI Options

```bash
# Full 5-year download (default)
python3 download.py

# Specific pairs
python3 download.py --symbols EURUSD GBPUSD USDJPY

# Custom history length
python3 download.py --years 2

# Custom date range
python3 download.py --start 2020-01-01 --end 2025-01-01

# Incremental update (only fetch new days since last run)
python3 download.py --incremental
```

### Arguments

| Flag | Default | Description |
|------|---------|-------------|
| `--symbols` | All 10 pairs | Space-separated list of pairs to download |
| `--years` | 5 | Years of history to fetch |
| `--start` | Computed from `--years` | Start date (YYYY-MM-DD), overrides `--years` |
| `--end` | Yesterday | End date (YYYY-MM-DD) |
| `--incremental` | Off | Only fetch days after the last attempted day in the ledger |
| `--repair` | Off | Re-fetch hours that failed or were never attempted (ledger-driven); `--start/--end` narrow the range |
| `--fresh` | Off | Delete the symbols' compiled files and ledger, then download (asks for confirmation) |
| `--clear` | Off | Delete the symbols' compiled files and ledger, then exit (asks for confirmation) |
| `--yes` | Off | Skip the `DELETE` confirmation for `--fresh`/`--clear` (scripts, dashboard) |

`--incremental`, `--repair`, `--fresh` and `--clear` are mutually exclusive. `--end` is exclusive.
Exit codes: 0 ok, 1 if any hour was lost after retries (run `--repair`), 2 if a destructive action was aborted.

### Default Symbols

**Majors:** EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD
**Crosses:** EURGBP, EURJPY, GBPJPY

## Output

### Directory Structure

```
duka-data/
├── download.py
├── requirements.txt
├── .download_meta.json      # Tracks last download date per symbol (for --incremental)
├── compiled/
│   ├── EURUSD_M5.csv        # 5-minute bars
│   ├── EURUSD_H1.csv        # 1-hour bars (resampled from M5)
│   ├── EURUSD_H4.csv        # 4-hour bars (resampled from M5)
│   ├── EURUSD_D1.csv        # Daily bars (resampled from M5)
│   ├── ...                  # Same for all other pairs
│   └── all_pairs_M5.csv     # Combined M5 file with symbol column
├── ledger/                  # per-symbol fetch ledger (hour,status) — see Completeness
└── raw/                     # (legacy, not used by new downloader)
```

### CSV Format

All compiled CSVs share the same column format:

```csv
time,open,high,low,close,volume,spread
2025-06-02 00:00:00+00:00,1.13527,1.13604,1.13520,1.13591,2040.8,0.00007
```

| Column | Description |
|--------|-------------|
| `time` | Bar open time in UTC (ISO 8601) |
| `open` | Opening bid price (5 decimal places) |
| `high` | Highest bid price in bar |
| `low` | Lowest bid price in bar |
| `close` | Closing bid price |
| `volume` | Total volume (bid_vol + ask_vol) |
| `spread` | Mean bid-ask spread during bar (5 decimal places) |

### Timeframe Resampling

All higher timeframes (H1, H4, D1) are resampled directly from M5 data:

- **open** = first M5 open in period
- **high** = max M5 high in period
- **low** = min M5 low in period
- **close** = last M5 close in period
- **volume** = sum of M5 volumes
- **spread** = mean of M5 spreads

This guarantees price consistency across all timeframes (no mixed data sources).

## Data Source Details

### Dukascopy bi5 Tick Format

- **URL pattern:** `http://datafeed.dukascopy.com/datafeed/{PAIR}/{year}/{month_0idx}/{day}/{hour}h_ticks.bi5`
- **Month indexing:** 0-based (January = 00, December = 11)
- **Compression:** LZMA (despite .bi5 extension)
- **Record size:** 20 bytes per tick (big-endian)

| Bytes | Type | Field |
|-------|------|-------|
| 0-3 | int32 | Milliseconds offset from hour start |
| 4-7 | int32 | Ask price (raw integer) |
| 8-11 | int32 | Bid price (raw integer) |
| 12-15 | float32 | Ask volume |
| 16-19 | float32 | Bid volume |

**Price conversion:** Divide raw integer by point divider:
- JPY pairs (USDJPY, EURJPY, GBPJPY, etc.): divide by 1,000
- Standard pairs: divide by 100,000

### Coverage

- **History:** Back to 2003+ for major pairs
- **Hours:** Full session, Sunday 20:00 UTC to Friday 23:59 UTC (Friday 21/22–23 come back empty)
- **Weekends:** Saturday is never requested; Sunday fetches hours 20–23
- **Holidays:** recorded as `E` in the ledger and never re-fetched

## Incremental Mode

The `--incremental` flag enables efficient daily updates:

1. Reads the ledger (falls back to `.download_meta.json`) to find the last attempted day per symbol
2. Only fetches days after that date
3. Merges new M5 bars with existing data (deduplicates on timestamp)
4. Re-resamples all higher timeframes from the full M5 dataset
5. Updates the metadata file

Typical workflow:

```bash
# Initial full download
python3 download.py --years 5

# Daily update (e.g., via cron)
python3 download.py --incremental
```

## Completeness: the fetch ledger, lost hours, and repair

Dukascopy intermittently answers `503` in bursts. The downloader retries each
hour 7 times (~2 minutes) and then re-tries every failed hour once more at the
end of the symbol ("sweep"). Hours that still fail are **lost**: they are absent
from M5 and therefore from H1/H4/D1, and nothing in the OHLC files reveals it.

Every attempted hour is recorded in `ledger/<SYMBOL>.csv`:

```
hour,status
2024-03-03T20:00:00Z,E
2024-03-04T00:00:00Z,D
2024-03-04T07:00:00Z,F
```

| Status | Meaning |
|---|---|
| `D` | data received |
| `E` | HTTP 200 with an empty body — holiday, weekend, closed session. Never re-fetched. |
| `N` | HTTP 404 — date not on the feed |
| `F` | gave up after retries and sweep — **lost**, repair will re-fetch it |

How to know an hour was lost:

- the progress line shows `failed_hours=N`; the end-of-symbol summary prints
  `N lost after sweep` and a WARNING with the hour list;
- `.download_status.json` has `hours_failed`, `hours_empty`, `hours_lost`, `lost_hours`;
- the process **exits 1** when any hour is lost (0 otherwise, 2 if a destructive
  action was aborted), so cron and scripts can gate on it;
- `python3 check_integrity.py` check 12 fails on any `F` row or any expected
  session hour with no ledger row.

Repair:

```bash
python3 download.py --repair                       # all symbols, whole ledger range
python3 download.py --repair --symbols EURUSD --start 2014-01-01 --end 2015-01-01
```

Data downloaded before the ledger existed has no ledger. The first `--repair`
seeds one from `<SYMBOL>_H1.csv` (hours with a bar → `D`) and then fetches every
other expected session hour once: Sunday sessions, holidays (recorded `E`), and
any hour previously lost to 503.

### Every run merges

A run never replaces a symbol's files: new bars are merged into the existing M5
(duplicates on `time` resolved in favour of the new run) and H1/H4/D1 are
rebuilt. Re-running a narrow `--start/--end` is therefore a safe way to refresh
a range. To start over, use `--fresh` (delete, then download) or `--clear`
(delete only). Both print the files they will remove with sizes and a backup
reminder, and require you to type `DELETE`:

```
About to DELETE compiled data and ledger for EURUSD:
  compiled/EURUSD_M5.csv                     98.1 MB
  ...
This cannot be undone. Back up compiled/ and ledger/ first if you need them
(e.g. cp -r compiled ledger ~/duka-backup-2026-09-11).
Type DELETE to continue:
```

Pass `--yes` to skip the prompt (the dashboard does this after its own
confirmation dialog). Without a terminal and without `--yes` the command aborts.

### Sunday session

FX opens Sunday 21:00 UTC (summer) / 22:00 UTC (winter). The downloader fetches
Sunday hours 20–23, so every week has a short **Sunday D1 bar** (UTC calendar
days are kept). Drop or merge it in your own pipeline if you want 5 daily bars
per week: `d1 = d1[d1["time"].dt.dayofweek != 6]`.

## Monitoring

### Real-time status file

While running, the downloader writes `.download_status.json` in the project root. Poll it to see progress:

```bash
# One-shot check
cat .download_status.json | python3 -m json.tool

# Live watch (updates every ~20 days of progress)
watch -n5 cat .download_status.json
```

Example status:

```json
{
  "state": "running",
  "current_symbol": "EURUSD",
  "symbol_progress": "1/10",
  "symbol_status": "downloading",
  "days_total": 1303,
  "days_completed": 400,
  "mode": "download",
  "hours_failed": 0,
  "hours_empty": 3,
  "hours_lost": 0,
  "ticks_total": 36501465,
  "rate_days_per_sec": 2.1,
  "eta_minutes": 7.2,
  "symbols_completed": [],
  "symbols_remaining": ["GBPUSD", "USDJPY", "..."]
}
```

### Log files

Detailed logs are written to `logs/download_YYYY-MM-DD.log`:

```bash
# Follow the log
tail -f logs/download_2026-04-01.log
```

The log contains:
- **INFO** — progress milestones (every 20 days), symbol completion, summary
- **DEBUG** — every individual day's tick count, timeframe file writes
- **WARNING** — lost hours (with the exact list) and the `--repair` hint

### Metadata file

`.download_meta.json` is updated after each symbol completes:

```bash
cat .download_meta.json
```

Shows last downloaded date, M5 bar count, and update timestamp per symbol.

## Performance

- **Parallelism:** 8 days downloading in parallel, each day downloads 6 hours in parallel
- **Rate:** ~2-3 days/second (~120-180 days/minute)
- **Storage:** ~5-10 MB per pair-year at M5 resolution
- **Full 5-year run (10 pairs):** ~1-2 hours depending on network

## Dependencies

```
pandas>=2.0
numpy>=1.24
```

All other imports are Python standard library (lzma, struct, urllib, json, concurrent.futures, logging).
