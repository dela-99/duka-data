"""
ledger.py — Session calendar and per-symbol hour ledger.

The ledger records every hour the downloader has *attempted* for a symbol:
    D  data      HTTP 200 with ticks
    E  empty     HTTP 200, zero-length body (holiday / closed session)
    N  notfound  HTTP 404 (date not on the feed)
    F  failed    gave up after all retries (transient error, e.g. 503)

File: ledger/<SYM>.csv with columns `hour,status`, hour as YYYY-MM-DDTHH:00:00Z,
sorted ascending. Importing this module has no side effects.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import pandas as pd

SUNDAY_FIRST_HOUR = 20
FETCH_TO_LEDGER = {"data": "D", "empty": "E", "notfound": "N", "failed": "F"}
ATTEMPTED = ("D", "E", "N")

Ledger = Dict[pd.Timestamp, str]


def hour_ts(day: datetime.date, hour: int) -> pd.Timestamp:
    return pd.Timestamp(datetime.datetime(day.year, day.month, day.day, hour), tz="UTC")


def session_hours(day: datetime.date) -> range:
    wd = day.weekday()
    if wd == 6:
        return range(SUNDAY_FIRST_HOUR, 24)
    if wd == 5:
        return range(0)
    return range(24)


def session_days(start: datetime.date, end: datetime.date) -> Iterator[datetime.date]:
    """Sunday..Friday in [start, end)."""
    d = start
    while d < end:
        if d.weekday() != 5:
            yield d
        d += datetime.timedelta(days=1)


def expected_session_hours(first: pd.Timestamp, last: pd.Timestamp) -> pd.DatetimeIndex:
    """Every session hour h with first <= h <= last (hour-floored, UTC)."""
    first = pd.Timestamp(first)
    last = pd.Timestamp(last)
    if first.tzinfo is None:
        first = first.tz_localize("UTC")
    if last.tzinfo is None:
        last = last.tz_localize("UTC")
    idx = pd.date_range(first.floor("h"), last.floor("h"), freq="h")
    wd, hr = idx.dayofweek, idx.hour
    return idx[(wd <= 4) | ((wd == 6) & (hr >= SUNDAY_FIRST_HOUR))]


def ledger_path(ledger_dir: Path, pair: str) -> Path:
    return Path(ledger_dir) / f"{pair}.csv"


def load_ledger(path: Path) -> Ledger:
    path = Path(path)
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype={"status": str})
    if df.empty:
        return {}
    ts = pd.to_datetime(df["hour"], utc=True, format="%Y-%m-%dT%H:%M:%SZ")
    return dict(zip(ts, df["status"]))


def save_ledger(path: Path, ledger: Ledger) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write("hour,status\n")
        for ts in sorted(ledger):
            f.write(f"{ts.strftime('%Y-%m-%dT%H:00:00Z')},{ledger[ts]}\n")


def seed_from_h1(h1_csv: Path) -> Ledger:
    """Legacy data has no ledger: mark every hour that has an H1 bar as D."""
    df = pd.read_csv(h1_csv, usecols=["time"], parse_dates=["time"])
    times = pd.to_datetime(df["time"], utc=True).dt.floor("h")
    return {ts: "D" for ts in times}


def last_attempted_date(ledger: Ledger) -> Optional[datetime.date]:
    attempted = [ts for ts, st in ledger.items() if st in ATTEMPTED]
    if not attempted:
        return None
    return max(attempted).date()


def repair_work(ledger: Ledger, start: Optional[datetime.date] = None,
                end: Optional[datetime.date] = None) -> List[pd.Timestamp]:
    """Hours to (re)fetch: F rows plus never-attempted session hours in the
    covered range [min, max] of the ledger, optionally narrowed to [start, end)."""
    if not ledger:
        return []
    lo, hi = min(ledger), max(ledger)
    if start is not None:
        lo = max(lo, hour_ts(start, 0))
    if end is not None:
        hi = min(hi, hour_ts(end, 0) - pd.Timedelta(hours=1))
    if lo > hi:
        return []
    return [h for h in expected_session_hours(lo, hi) if ledger.get(h, "F") == "F"]


def group_runs(hours: Iterable[pd.Timestamp]) -> List[Tuple[pd.Timestamp, pd.Timestamp, int]]:
    runs: List[List[pd.Timestamp]] = []
    for t in sorted(hours):
        if runs and t - runs[-1][1] == pd.Timedelta(hours=1):
            runs[-1][1] = t
        else:
            runs.append([t, t])
    return [(a, b, int((b - a) / pd.Timedelta(hours=1)) + 1) for a, b in runs]
