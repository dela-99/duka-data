# Data Quality Report

_Last validated 2026-09-13 against `compiled/` (14 symbols × M5/H1/H4/D1 = 56 files, ~1.4 GiB)
plus `ledger/` (14 fetch ledgers). Reproduce with `python3 check_integrity.py` (12/12 checks
passing)._

## Verdict

| Dimension | Grade | Summary |
|---|---|---|
| **Completeness** | A | Full ~20-year FX history incl. Sunday sessions; every expected session hour is accounted for in the fetch ledger (data / empty / not-found), none failed. |
| **Accuracy** | A | Every D1 bar reproduces **exactly** from its M5 constituents; all price outliers map to real market events. |
| **Quality** | A | No duplicate/misordered/negative/NaN values across all 56 files after cleaning. |

The 11 FX pairs are backtest-grade over 2006-05-24 → 2026-05-18 (Sunday 21/22:00 UTC open through Friday close). The three CFDs
(XAUUSD, LIGHTCMDUSD, USA500IDXUSD) are sound but carry the structural caveats
listed below (shorter history, session gaps, unreliable volume).

## What was validated

- **Structure** — all 56 files present; timestamps strictly increasing, UTC, no
  duplicates, no NaNs in OHLC.
- **OHLC invariants** — `high ≥ max(o,c,l)`, `low ≤ min(o,c,h)`, all prices > 0.
- **Cross-timeframe reconciliation** — daily OHLC rebuilt from raw M5 matches the
  shipped `*_D1.csv` with **zero mismatches and 0.0 relative error** for all 14
  symbols. D1/H4/H1 are faithful aggregations of one tick base.
- **Outliers are real** — the largest 5-min moves correspond to documented events:
  SNB franc de-peg (2015-01-15), sterling flash crash (2016-10-06), Brexit
  (2016-06-24), JPY flash crash (2019-01-02), WTI negative-oil (2020-04-21), COVID
  crash (2020-03-16). No spike-and-revert bad-tick artifacts.
- **FX spreads** are realistic institutional levels (EURUSD mean 0.62 pip,
  USDJPY 0.76 pip).

## Completeness audit (2026-09-11 → 2026-09-13)

A reader of the Medium write-up noticed that runs with many 503 retries still
passed the integrity check. Investigation confirmed two silent gaps in the
2026-07-06 dataset that the checker could not see, because H1/H4/D1 are
resampled from M5 and a missing hour is therefore consistent across all files:

- **~565 hours lost to HTTP 503** across the 14 symbols (USDJPY 87, AUDUSD 78,
  USDCAD 66, EURUSD 53, …). The downloader gave up after 4 retries (~19 s) and
  moved on; `days_failed` stayed 0 because it only counted exceptions.
- **No Sunday sessions at all.** The downloader iterated Mon–Fri only, so the
  Sunday 21:00/22:00–23:59 UTC session (~2,750 hours per FX symbol over 20
  years) was never fetched.

Both are fixed (GitHub issue #3):

- `download.py` keeps a per-hour **fetch ledger** (`ledger/<SYM>.csv`, statuses
  D/E/N/F), retries 7 times over ~2 minutes, re-tries failed hours in an
  end-of-symbol sweep, merges into existing data instead of overwriting,
  fetches Sunday 20–23, reports `hours_lost`, and exits 1 when hours remain lost.
  `--repair` re-fetches failed / never-attempted hours.
- `check_integrity.py` check 12 fails on any ledger row marked failed or any
  expected session hour with no ledger row.

The dataset was backfilled with `python3 download.py --repair` (44 h wall clock,
~132k requests, 42 main-pass failures all recovered by the sweep, 0 lost).
Per-symbol ledgers now cover every expected session hour; the census of
"expected hours without an H1 bar" equals the count of ledger-confirmed empty
hours exactly. Before/after evidence: `docs/superpowers/validation/`.

## Cleaning applied (2026-09-13)

1. **AUDUSD M5 — 1 negative spread clipped to 0** (2007-05-13 23:55 UTC, a
   Sunday-session bar fetched by the backfill; −0.4 pip, same early-Dukascopy
   bid/ask crossing class as the 2026-07-06 cleaning). H1/H4/D1 rebuilt from M5
   with the pipeline's own resampler; the same row patched in `all_pairs_M5.csv`.
2. **`all_pairs_M5.csv` rebuilt** by the repair run from the merged per-symbol
   M5 files (all 14 symbols).
3. **`DATA_MANIFEST.md` regenerated.**

## Cleaning applied (2026-07-06)

1. **AUDUSD M5 — 19 negative spreads clipped to 0.** All fell in 2007–2009
   (early-Dukascopy bid/ask crossings, ≤11 pips). Only `AUDUSD_M5.csv` was
   affected; H1/H4/D1 use aggregated means that were already non-negative, and
   OHLC reconciliation is unaffected. The surgical rewrite left every other line
   byte-identical.
2. **`all_pairs_M5.csv` rebuilt.** It had previously contained only the last
   download run's symbols (LIGHTCMDUSD + USA500IDXUSD). It is now a proper
   long-format combination of **all 14 symbols** (19,195,808 rows, sorted by
   `symbol` then `time`, with a `symbol` column). Note: it mixes FX and CFDs —
   filter on `symbol` and do not aggregate across instruments blindly.
3. **`DATA_MANIFEST.md` regenerated** — the old copy was stale (listed 11 symbols
   and AUDJPY/GBPJPY at ~1,300 D1 bars; both are now backfilled to 5,212).
4. **`check_integrity.py` upgraded** — auto-discovers all symbols (incl. CFDs),
   adds the M5→D1 reconciliation check, and separates correctness gates from
   informational coverage/session/freshness reporting.

## Known caveats (inherent — not defects to fix)

- **CFD volume is unreliable.** Zero-volume bar share: LIGHTCMDUSD ~49%,
  USA500IDXUSD ~16%, XAUUSD ~14%. Do **not** build volume-based signals on the
  CFDs. FX volume is clean (0% zero).
- **CFD session structure.** Metals/energy/indices are not 24×5. They show many
  intraday session gaps by design and shorter history (XAUUSD from 2006,
  USA500IDXUSD from 2012, LIGHTCMDUSD from 2013). USA500/LIGHTCMD also have
  multi-week gaps in their early 2013 data.
- **Isolated FX feed gap.** USDJPY/EURJPY have a single ~6.7-day gap in June 2009
  (a Dukascopy feed outage, not a compiler issue).
- **Sunday D1 bar.** Each week now has a short Sunday bar (UTC calendar days
  are kept). Filter `dayofweek != 6` if you want 5 bars/week.
- **Friday 21:00–23:59 and Sunday 20:00 UTC** are requested and recorded as
  empty in the ledger (session closed); they are not gaps.
- **Freshness.** Data ends 2026-05-18 (FX) / 2026-05-22 (CFD). Collection is
  paused; run `python3 download.py --incremental` to top up to the present.
