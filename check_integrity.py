"""
check_integrity.py — Comprehensive data integrity checks for compiled CSVs.

Auto-discovers every symbol present in compiled/ (FX pairs *and* the metal /
energy / index CFDs) and validates each across four timeframes.

Checks:
 1. File presence (all discovered symbols x all timeframes)
 2. Date range coverage (informational — history length varies by instrument)
 3. 24-hour coverage per day (M5; informational for CFDs, which have sessions)
 4. Cross-timeframe bar-count ratios (M5:H1:H4:D1)
 5. OHLC sanity (high >= max(o,c,l), low <= min(o,c,h), high >= low)
 6. Spread sanity (no negative spreads; magnitude bounds for FX)
 7. Duplicate timestamps
 8. Trading-day gap detection (D1)
 9. Cross-timeframe price consistency (D1 close == last H1 close of day)
10. M5 -> D1 reconciliation (rebuild daily OHLC from M5, compare to D1 file)
11. Data freshness (informational)
12. Session-hour completeness (gated when a ledger exists; informational otherwise)

Correctness checks (1, 4, 5, 6, 7, 9, 10, 12) gate the exit code; coverage /
session / freshness checks (2, 3, 8, 11) are informational and never fail the
run, because history length, session structure and collection cadence are
properties of the instrument, not defects.

Check 12 is the only one that can see hours the downloader *lost* (e.g. to
HTTP 503): H1/H4/D1 are resampled from M5, so a missing hour is consistent
across all four files and invisible to checks 1-11.
"""

import argparse
import datetime
from pathlib import Path
import sys

import pandas as pd
import numpy as np

from ledger import expected_session_hours, group_runs, ledger_path, load_ledger

BASE_DIR = Path(__file__).parent
COMPILED_DIR = BASE_DIR / "compiled"
LEDGER_DIR = BASE_DIR / "ledger"
TIMEFRAMES = ["M5", "H1", "H4", "D1"]
SYMBOLS: list = []

# FX pairs quote in pips; CFDs (metals/energy/indices) use other conventions,
# so pip-based spread bounds and 24h-session expectations apply to FX only.
FX_SYMBOLS = {
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY",
}

# Expected bars per day per timeframe (full 24h forex session)
EXPECTED_PER_DAY = {"M5": 288, "H1": 24, "H4": 6, "D1": 1}


def discover_symbols():
    """Every symbol with at least one compiled timeframe (excludes all_pairs)."""
    syms = set()
    for csv in COMPILED_DIR.glob("*.csv"):
        stem = csv.stem
        if stem.startswith("all_pairs") or "_" not in stem:
            continue
        sym, tf = stem.rsplit("_", 1)
        if tf in TIMEFRAMES:
            syms.add(sym)
    return sorted(syms)


def configure(data_dir: Path) -> None:
    """Point the checker at <data_dir>/compiled and <data_dir>/ledger."""
    global COMPILED_DIR, LEDGER_DIR, SYMBOLS
    COMPILED_DIR = Path(data_dir) / "compiled"
    LEDGER_DIR = Path(data_dir) / "ledger"
    SYMBOLS = discover_symbols()


def pip_mult(sym):
    return 100 if sym.endswith("JPY") else 10000


def load_df(sym, tf):
    return pd.read_csv(COMPILED_DIR / f"{sym}_{tf}.csv", parse_dates=["time"])


def check_file_presence():
    print("\n[1] File presence check")
    print("-" * 60)
    missing = []
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            if not (COMPILED_DIR / f"{sym}_{tf}.csv").exists():
                missing.append(f"{sym}_{tf}")
    if missing:
        print(f"  FAIL: missing {len(missing)} files: {missing}")
        return False
    print(f"  OK: all {len(SYMBOLS) * len(TIMEFRAMES)} files present "
          f"({len(SYMBOLS)} symbols x {len(TIMEFRAMES)} timeframes)")
    return True


def check_date_coverage():
    print("\n[2] Date range coverage (informational)")
    print("-" * 60)
    print(f"  {'Symbol':<13} {'First':<21} {'Last':<21} {'Days':>6}")
    for sym in SYMBOLS:
        df = load_df(sym, "M5")
        first, last = df["time"].min(), df["time"].max()
        days = df["time"].dt.date.nunique()
        print(f"  {sym:<13} {str(first):<21} {str(last):<21} {days:>6}")
    return True


def check_24h_coverage():
    print("\n[3] 24-hour coverage check (M5) — FX gated, CFDs informational")
    print("-" * 60)
    issues = 0
    for sym in SYMBOLS:
        df = load_df(sym, "M5")
        hours = df["time"].dt.hour.nunique()
        avg_bars = len(df) / df["time"].dt.date.nunique()
        is_fx = sym in FX_SYMBOLS
        flag = " "
        if is_fx and hours != 24:
            flag = "!"
            issues += 1
        tag = "" if is_fx else "  (CFD: session gaps expected)"
        print(f"  {sym:<13} hours={hours}/24  avg_bars/day={avg_bars:.0f}/288  {flag}{tag}")
    return issues == 0  # only FX gates


def check_bar_counts():
    print("\n[4] Cross-timeframe bar count ratios, Mon-Fri bars (M5/H1~12, H1/H4~4, H4/D1~6)")
    print("-" * 60)
    print(f"  {'Symbol':<13} {'M5':>10} {'H1':>9} {'H4':>8} {'D1':>7}  "
          f"{'M5/H1':>6} {'H1/H4':>6} {'H4/D1':>6}")
    issues = 0
    for sym in SYMBOLS:
        counts = {}
        for tf in TIMEFRAMES:
            df = load_df(sym, tf)
            counts[tf] = int((df["time"].dt.dayofweek <= 4).sum())   # short Sunday bars excluded
        r1 = counts["M5"] / counts["H1"]
        r2 = counts["H1"] / counts["H4"]
        r3 = counts["H4"] / counts["D1"]
        # CFDs run leaner ratios due to session structure; widen their band.
        if sym in FX_SYMBOLS:
            ok = (11.5 <= r1 <= 12.5 and 3.7 <= r2 <= 4.1 and 5.7 <= r3 <= 6.1)
        else:
            ok = (11 <= r1 <= 12.5 and 3.4 <= r2 <= 4.1 and 5.2 <= r3 <= 6.1)
        flag = " " if ok else "!"
        issues += 0 if ok else 1
        print(f"  {sym:<13} {counts['M5']:>10,} {counts['H1']:>9,} "
              f"{counts['H4']:>8,} {counts['D1']:>7,}  "
              f"{r1:>6.2f} {r2:>6.2f} {r3:>6.2f} {flag}")
    return issues == 0


def check_ohlc_sanity():
    print("\n[5] OHLC sanity (high >= o,c,l; low <= o,c,h; prices > 0)")
    print("-" * 60)
    issues = 0
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            df = load_df(sym, tf)
            bad_high = ((df["high"] < df["open"]) | (df["high"] < df["close"]) |
                        (df["high"] < df["low"])).sum()
            bad_low = ((df["low"] > df["open"]) | (df["low"] > df["close"])).sum()
            nonpos = ((df[["open", "high", "low", "close"]] <= 0).any(axis=1)).sum()
            if bad_high or bad_low or nonpos:
                print(f"  {sym}_{tf}: bad_high={bad_high}, bad_low={bad_low}, "
                      f"nonpos={nonpos}  !")
                issues += 1
    if issues == 0:
        print(f"  OK: all OHLC relationships valid across "
              f"{len(SYMBOLS) * len(TIMEFRAMES)} files")
    return issues == 0


def check_spread_sanity():
    print("\n[6] Spread sanity (M5) — no negatives; FX magnitude in pips")
    print("-" * 60)
    print(f"  {'Symbol':<13} {'neg':>5} {'zero':>8} {'min_pip':>8} "
          f"{'mean_pip':>9} {'max_pip':>9}")
    issues = 0
    for sym in SYMBOLS:
        df = load_df(sym, "M5")
        sp = df["spread"]
        n_neg = int((sp < 0).sum())
        n_zero = int((sp == 0).sum())
        if n_neg > 0:
            issues += 1
        if sym in FX_SYMBOLS:
            p = sp * pip_mult(sym)
            print(f"  {sym:<13} {n_neg:>5} {n_zero:>8} {p.min():>8.2f} "
                  f"{p.mean():>9.2f} {p.max():>9.2f}"
                  f"{'  !' if n_neg else ''}")
        else:
            print(f"  {sym:<13} {n_neg:>5} {n_zero:>8} {'(CFD — raw price units)':>27}"
                  f"{'  !' if n_neg else ''}")
    return issues == 0  # only negatives gate


def check_duplicates():
    print("\n[7] Duplicate timestamps")
    print("-" * 60)
    issues = 0
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            dups = load_df(sym, tf)["time"].duplicated().sum()
            if dups > 0:
                print(f"  {sym}_{tf}: {dups} duplicates  !")
                issues += 1
    if issues == 0:
        print("  OK: no duplicate timestamps in any file")
    return issues == 0


def check_gaps():
    print("\n[8] Trading-day gap detection (D1, informational)")
    print("-" * 60)
    for sym in SYMBOLS:
        df = load_df(sym, "D1")
        dates = sorted(df["time"].dt.date.unique())
        first, last = dates[0], dates[-1]
        expected = set()
        d = first
        while d <= last:
            if d.weekday() < 5:
                expected.add(d)
            d += datetime.timedelta(days=1)
        missing = len(expected - set(dates))
        print(f"  {sym:<13} {missing} missing weekdays (holidays + any feed gaps)")
    return True


def check_price_consistency():
    print("\n[9] Cross-timeframe price consistency (D1 close ~ last H1 close of day)")
    print("-" * 60)
    issues = 0
    for sym in SYMBOLS:
        d1 = load_df(sym, "D1").set_index("time")
        h1 = load_df(sym, "H1").set_index("time")
        h1_last = h1.resample("1D")["close"].last()
        merged = pd.concat([d1["close"].rename("d1"),
                            h1_last.rename("h1")], axis=1).dropna()
        diffs = (merged["d1"] - merged["h1"]).abs()
        max_diff = diffs.max() if len(diffs) else 0.0
        # scale tolerance to price level (JPY / gold / index quote larger)
        tol = max(abs(d1["close"]).mean() * 1e-6, 1e-6)
        flag = " " if max_diff <= tol else "!"
        issues += 0 if flag == " " else 1
        print(f"  {sym:<13} max_diff={max_diff:.6f}  tol={tol:.2e}  {flag}")
    return issues == 0


def check_reconciliation():
    print("\n[10] M5 -> D1 reconciliation (rebuild daily OHLC from M5)")
    print("-" * 60)
    print(f"  {'Symbol':<13} {'days':>6} {'o!=':>4} {'h!=':>4} {'l!=':>4} "
          f"{'c!=':>4} {'max_rel_err':>12}")
    issues = 0
    for sym in SYMBOLS:
        m5 = load_df(sym, "M5").set_index("time")[["open", "high", "low", "close"]]
        d1 = load_df(sym, "D1").set_index("time")[["open", "high", "low", "close"]]
        agg = m5.resample("1D").agg(open=("open", "first"), high=("high", "max"),
                                    low=("low", "min"), close=("close", "last")).dropna()
        j = d1.join(agg, how="inner", lsuffix="_d", rsuffix="_m").dropna()

        def mm(col):
            tol = np.abs(j[f"{col}_d"]) * 1e-5 + 1e-9
            return int((np.abs(j[f"{col}_d"] - j[f"{col}_m"]) > tol).sum())

        n = {c: mm(c) for c in ("open", "high", "low", "close")}
        rel = (np.abs(j["close_d"] - j["close_m"]) /
               np.abs(j["close_d"]).replace(0, np.nan)).max()
        flag = " " if sum(n.values()) == 0 else "!"
        issues += 0 if flag == " " else 1
        print(f"  {sym:<13} {len(j):>6} {n['open']:>4} {n['high']:>4} "
              f"{n['low']:>4} {n['close']:>4} {rel:>12.2e} {flag}")
    if issues == 0:
        print("  OK: every D1 bar reproduces exactly from its M5 constituents")
    return issues == 0


def check_recent_data():
    print("\n[11] Recent data freshness (informational)")
    print("-" * 60)
    today = datetime.date.today()
    for sym in SYMBOLS:
        last = load_df(sym, "M5")["time"].max().date()
        print(f"  {sym:<13} last={last}  ({(today - last).days} days old)")
    print("  (run `python3 download.py --incremental` to top up; `--repair` to fill gaps)")
    return True


def check_session_completeness():
    print("\n[12] Session-hour completeness (ledger-gated; informational without a ledger)")
    print("-" * 60)
    issues = 0
    for sym in SYMBOLS:
        ledger = load_ledger(ledger_path(LEDGER_DIR, sym))
        if ledger:
            lo, hi = min(ledger), max(ledger)
            offenders = [h for h in expected_session_hours(lo, hi) if ledger.get(h, "F") == "F"]
            n_failed = sum(1 for h in offenders if h in ledger)
            n_never = len(offenders) - n_failed
            flag = " " if not offenders else "!"
            issues += 1 if offenders else 0
            print(f"  {sym:<13} ledger {lo:%Y-%m-%d} .. {hi:%Y-%m-%d}: "
                  f"{n_failed} failed, {n_never} never attempted  {flag}")
        else:
            h1 = load_df(sym, "H1")
            have = set(pd.to_datetime(h1["time"], utc=True))
            offenders = [h for h in expected_session_hours(h1["time"].min(), h1["time"].max())
                         if h not in have]
            print(f"  {sym:<13} no ledger: {len(offenders):,} expected session hours without an "
                  f"H1 bar (informational)")
        runs = group_runs(offenders)
        for a, b, n in runs[:10]:
            print(f"      {a:%Y-%m-%d %H:%M} -> {b:%Y-%m-%d %H:%M}  ({n}h)")
        if len(runs) > 10:
            print(f"      ... and {len(runs) - 10} more gap runs")
        if not ledger:
            print(f"      no ledger: run python3 download.py --repair --symbols {sym} to build one")
    return issues == 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate compiled CSVs and the fetch ledger")
    ap.add_argument("--data-dir", default=str(BASE_DIR),
                    help="directory containing compiled/ and ledger/ (default: repo root)")
    args = ap.parse_args(argv)
    configure(Path(args.data_dir))

    print("=" * 60)
    print("DATA INTEGRITY CHECK")
    print("=" * 60)
    print(f"Discovered {len(SYMBOLS)} symbols: {', '.join(SYMBOLS)}")

    checks = [
        ("File presence",        check_file_presence),
        ("Date coverage",        check_date_coverage),
        ("24h coverage",         check_24h_coverage),
        ("Bar count ratios",     check_bar_counts),
        ("OHLC sanity",          check_ohlc_sanity),
        ("Spread sanity",        check_spread_sanity),
        ("Duplicate timestamps", check_duplicates),
        ("Trading day gaps",     check_gaps),
        ("Price consistency",    check_price_consistency),
        ("M5->D1 reconciliation", check_reconciliation),
        ("Data freshness",       check_recent_data),
        ("Session-hour completeness", check_session_completeness),
    ]

    results = []
    for name, fn in checks:
        try:
            results.append((name, fn()))
        except Exception as e:
            print(f"  ERROR in {name}: {e}")
            results.append((name, False))

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}]  {name}")
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
