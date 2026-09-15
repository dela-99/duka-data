"""
download.py — Direct Dukascopy bi5 tick downloader & OHLC compiler
===================================================================
Downloads raw tick data from Dukascopy's public datafeed, decodes the
LZMA-compressed bi5 files, and resamples into M5/H1/H4/D1 OHLC bars
with bid/ask spread information.

No external trading libraries required — uses only stdlib + pandas + numpy.

Tick bi5 format (per hour file):
- URL: http://datafeed.dukascopy.com/datafeed/{PAIR}/{year}/{month_0idx:02d}/{day:02d}/{hour:02d}h_ticks.bi5
- Month is 0-indexed (January=00, December=11)
- LZMA compressed
- Each tick record is 20 bytes (big-endian):
    int32:   milliseconds offset from start of hour
    int32:   ask price (raw integer, divide by point_divider)
    int32:   bid price (raw integer, divide by point_divider)
    float32: ask volume
    float32: bid volume
- JPY pairs: point_divider = 1000
- Standard pairs: point_divider = 100000

Output: M5, H1, H4, D1 CSVs with columns:
  time, open, high, low, close, volume, spread

Monitoring: writes real-time status to .download_status.json and logs to logs/
Ledger: records every attempted hour in ledger/<SYM>.csv (see ledger.py).

Design: Research data acquisition only. No live trading code.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import lzma
#import resource
import struct
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple, Optional

import numpy as np
import pandas as pd

from ledger import (FETCH_TO_LEDGER, group_runs, hour_ts, last_attempted_date,
                    ledger_path, load_ledger, repair_work, save_ledger, seed_from_h1,
                    session_days, session_hours)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "http://datafeed.dukascopy.com/datafeed"
TICK_RECORD_SIZE = 20  # bytes per tick

JPY_PAIRS = {"USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY"}

# Non-FX instruments (metals, energy, indices). Dukascopy stores these with a
# 1000 point-divider (3 implied decimals), same as JPY pairs — NOT 100000.
# Symbol = Dukascopy datafeed instrument name (verified against the live feed).
DIV_1000_INSTRUMENTS = {
    "XAUUSD", "XAGUSD",                                  # gold, silver
    "LIGHTCMDUSD", "BRENTCMDUSD",                        # WTI, Brent crude
    "USA500IDXUSD", "USA30IDXUSD", "USATECHIDXUSD",      # S&P500, Dow, Nasdaq100
}

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "AUDUSD", "USDCAD", "NZDUSD",       # majors
    "EURGBP", "EURJPY", "GBPJPY",       # crosses
]

DEFAULT_YEARS = 5
DAY_WORKERS = 3    # parallel days downloading at once (bumped from 2 → 3*2=6 concurrent, watch for 503s)
HOUR_WORKERS = 2   # parallel hours within each day (3*2=6 max concurrent requests; ~upper edge of ~5-10 req/s limit)
RETRY_DELAYS = (2, 4, 8, 16, 32, 60)   # seconds between attempts -> 7 attempts, ~2 min (503s come in ~60s bursts)

BASE_DIR     = Path(__file__).parent
RAW_DIR      = BASE_DIR / "raw"
COMPILED_DIR = BASE_DIR / "compiled"
LOG_DIR      = BASE_DIR / "logs"
LEDGER_DIR   = BASE_DIR / "ledger"
META_FILE    = BASE_DIR / ".download_meta.json"
STATUS_FILE  = BASE_DIR / ".download_status.json"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    """Configure file + console logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"download_{datetime.date.today()}.log"

    logger = logging.getLogger("duka")
    logger.setLevel(logging.DEBUG)

    # File handler — detailed
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"
    ))

    # Console handler — progress only
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# Status file (real-time monitoring)
# ---------------------------------------------------------------------------

_status: dict = {}


def status_update(**kwargs):
    """Update .download_status.json with current state."""
    _status.update(kwargs)
    _status["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        STATUS_FILE.write_text(json.dumps(_status, indent=2, default=str))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Performance instrumentation
# ---------------------------------------------------------------------------
# `ru_maxrss` is *peak* process RSS since start. Unit differs by OS: bytes on
# macOS, kilobytes on Linux. We track peak (not current) because the stdlib has
# no portable way to read current RSS; peak deltas between samples still tell
# us when memory grew.
_RU_MAXRSS_TO_BYTES = 1 if sys.platform == "darwin" else 1024


def rss_peak_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RU_MAXRSS_TO_BYTES / (1024 * 1024) # pyright: ignore[reportUndefinedVariable]


class Perf:
    """Threadsafe counters & timers used to debug perf across worker threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.counters: dict[str, float] = {}
        self.day_durations: list[float] = []   # wall-clock seconds per day

    def add(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0.0) + value

    def record_day(self, seconds: float) -> None:
        with self._lock:
            self.day_durations.append(seconds)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            snap = dict(self.counters)
            if self.day_durations:
                ds = sorted(self.day_durations)
                snap["day_p50_s"] = ds[len(ds) // 2]
                snap["day_p95_s"] = ds[int(len(ds) * 0.95)]
                snap["day_max_s"] = ds[-1]
            return snap

    def reset(self) -> None:
        with self._lock:
            self.counters.clear()
            self.day_durations.clear()


PERF = Perf()


def _fmt_bytes(n: float) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n:.0f} B"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_point_divider(pair: str) -> int:
    pair = pair.upper().replace("/", "")
    return 1000 if (pair in JPY_PAIRS or pair in DIV_1000_INSTRUMENTS) else 100000


# ---------------------------------------------------------------------------
# Download & Decode
# ---------------------------------------------------------------------------

class FetchResult(NamedTuple):
    status: str                 # "data" | "empty" | "notfound" | "failed"
    data: Optional[bytes]


def download_hour_bi5(pair: str, dt: datetime.datetime,
                      delays: tuple = RETRY_DELAYS) -> FetchResult:
    """Download one hour's bi5 file. Retries transient errors along `delays`."""
    month_0idx = dt.month - 1
    url = (
        f"{BASE_URL}/{pair}/{dt.year}/{month_0idx:02d}/"
        f"{dt.day:02d}/{dt.hour:02d}h_ticks.bi5"
    )
    attempts = len(delays) + 1
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        t0 = time.perf_counter()
        try:
            PERF.add("http_requests")
            resp = urllib.request.urlopen(req, timeout=30)
            data = resp.read()
            PERF.add("net_seconds", time.perf_counter() - t0)
            if len(data) == 0:
                PERF.add("http_empty")
                return FetchResult("empty", None)
            PERF.add("bytes_downloaded", len(data))
            return FetchResult("data", data)
        except urllib.error.HTTPError as e:
            PERF.add("net_seconds", time.perf_counter() - t0)
            if e.code == 404:
                PERF.add("http_404")
                return FetchResult("notfound", None)
            err = f"HTTP {e.code}"
        except Exception as e:
            PERF.add("net_seconds", time.perf_counter() - t0)
            err = repr(e)
        PERF.add("http_retries")
        if attempt < attempts - 1:
            time.sleep(delays[attempt])
            continue
        PERF.add("http_failures")
        log.debug(f"{err} after {attempts} attempts: {url}")
    return FetchResult("failed", None)


# bi5 tick record laid out as a numpy structured dtype for vectorized decode.
TICK_DTYPE = np.dtype([
    ("ms_offset", ">i4"),
    ("ask_raw",   ">i4"),
    ("bid_raw",   ">i4"),
    ("ask_vol",   ">f4"),
    ("bid_vol",   ">f4"),
])

_EMPTY_TICK_DF = pd.DataFrame({
    "time":    pd.Series(dtype="datetime64[ns, UTC]"),
    "bid":     pd.Series(dtype=np.float64),
    "ask":     pd.Series(dtype=np.float64),
    "bid_vol": pd.Series(dtype=np.float32),
    "ask_vol": pd.Series(dtype=np.float32),
})


def decode_ticks(
    data: bytes,
    hour_start: datetime.datetime,
    point_divider: int,
) -> pd.DataFrame:
    """Decode LZMA bi5 tick data into a DataFrame (vectorized via numpy)."""
    t0 = time.perf_counter()
    try:
        decompressed = lzma.decompress(data)
    except lzma.LZMAError:
        PERF.add("decode_seconds", time.perf_counter() - t0)
        PERF.add("decode_errors")
        return _EMPTY_TICK_DF

    PERF.add("bytes_decompressed", len(decompressed))
    n_ticks = len(decompressed) // TICK_RECORD_SIZE
    if n_ticks == 0:
        PERF.add("decode_seconds", time.perf_counter() - t0)
        return _EMPTY_TICK_DF

    arr = np.frombuffer(decompressed, dtype=TICK_DTYPE, count=n_ticks)

    # Drop ticks with both volumes zero (matches prior behavior).
    mask = (arr["ask_vol"] != 0.0) | (arr["bid_vol"] != 0.0)
    if not mask.all():
        arr = arr[mask]
    if arr.size == 0:
        return _EMPTY_TICK_DF

    # Build timestamps vectorized: hour_start (naive UTC) + ms offsets, then tag UTC.
    hour_ns = np.datetime64(hour_start.replace(tzinfo=None), "ns")
    times = hour_ns + arr["ms_offset"].astype("timedelta64[ms]")

    bid = arr["bid_raw"].astype(np.float64) / point_divider
    ask = arr["ask_raw"].astype(np.float64) / point_divider

    df = pd.DataFrame({
        "time":    pd.DatetimeIndex(times).tz_localize("UTC"),
        "bid":     bid,
        "ask":     ask,
        "bid_vol": np.asarray(arr["bid_vol"], dtype=np.float32).copy(),
        "ask_vol": np.asarray(arr["ask_vol"], dtype=np.float32).copy(),
    })
    PERF.add("ticks_decoded", len(df))
    PERF.add("decode_seconds", time.perf_counter() - t0)
    return df


def _download_and_decode_hour(pair: str, day: datetime.date, hour: int,
                              point_divider: int) -> tuple[int, str, pd.DataFrame]:
    """Download + decode a single hour. Returns (hour, fetch status, ticks)."""
    hour_start = datetime.datetime(day.year, day.month, day.day, hour,
                                   tzinfo=datetime.timezone.utc)
    res = download_hour_bi5(pair, hour_start)
    if res.data is None:
        return hour, res.status, _EMPTY_TICK_DF
    return hour, res.status, decode_ticks(res.data, hour_start, point_divider)


def download_day_ticks(
    pair: str,
    day: datetime.date,
    point_divider: int,
    hours: Optional[list] = None,
) -> tuple[pd.DataFrame, int, dict]:
    """Download the given hours of one day (default: the session hours for that
    weekday) and return (M5 bars, tick_count, {hour: fetch status}).

    Resamples to M5 inside this function so callers never hold raw ticks for
    more than one day at a time — keeps memory flat regardless of date range.
    5-minute bars divide cleanly into a day, so per-day resample is identical
    to a global resample.
    """
    day_t0 = time.perf_counter()
    if hours is None:
        hours = list(session_hours(day))
    hour_frames: list[pd.DataFrame] = []
    status: dict = {}

    if hours:
        with ThreadPoolExecutor(max_workers=HOUR_WORKERS) as pool:
            futures = {
                pool.submit(_download_and_decode_hour, pair, day, h, point_divider): h
                for h in hours
            }
            for future in as_completed(futures):
                h = futures[future]
                try:
                    _, st, tdf = future.result()
                    status[h] = st
                    if not tdf.empty:
                        hour_frames.append(tdf)
                except Exception as e:
                    PERF.add("worker_exceptions")
                    status[h] = "failed"
                    log.debug(f"hour worker exception {pair} {day} {h:02d}h: {e}")

    if not hour_frames:
        PERF.record_day(time.perf_counter() - day_t0)
        return _EMPTY_M5_DF.copy(), 0, status

    ticks_df = pd.concat(hour_frames, ignore_index=True)
    tick_count = len(ticks_df)

    rs_t0 = time.perf_counter()
    m5 = ticks_to_m5(ticks_df)
    PERF.add("resample_seconds", time.perf_counter() - rs_t0)

    del ticks_df, hour_frames
    PERF.record_day(time.perf_counter() - day_t0)
    return m5, tick_count, status


# ---------------------------------------------------------------------------
# Tick -> OHLC Resampling
# ---------------------------------------------------------------------------

_M5_COLUMNS = ["time", "open", "high", "low", "close", "volume", "spread"]
_EMPTY_M5_DF = pd.DataFrame(columns=_M5_COLUMNS)


def ticks_to_m5(df: pd.DataFrame) -> pd.DataFrame:
    """Convert a tick DataFrame to M5 OHLC bars with spread.

    Uses bid price for OHLC, mean (ask - bid) for spread, sum of bid+ask
    volumes for volume.
    """
    if df.empty:
        return _EMPTY_M5_DF.copy()

    df = df.set_index("time").sort_index()
    df["spread"] = df["ask"] - df["bid"]
    df["volume"] = df["bid_vol"] + df["ask_vol"]

    m5 = df.resample("5min").agg({
        "bid":    ["first", "max", "min", "last"],
        "volume": "sum",
        "spread": "mean",
    })
    m5.columns = ["open", "high", "low", "close", "volume", "spread"]
    m5 = m5.dropna(subset=["open"])

    for col in ("open", "high", "low", "close", "spread"):
        m5[col] = m5[col].round(5)
    m5["volume"] = m5["volume"].round(1)

    return m5.reset_index()


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample M5 DataFrame to a higher timeframe."""
    df2 = df.set_index("time")
    resampled = df2.resample(rule).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
        "spread": "mean",
    })
    resampled = resampled.dropna(subset=["open"])
    resampled["spread"] = resampled["spread"].round(5)
    resampled["volume"] = resampled["volume"].round(1)
    return resampled.reset_index().rename(columns={resampled.index.name or "time": "time"})


# ---------------------------------------------------------------------------
# Metadata (for incremental refresh)
# ---------------------------------------------------------------------------

def load_meta() -> dict:
    if META_FILE.exists():
        return json.loads(META_FILE.read_text())
    return {}


def save_meta(meta: dict):
    META_FILE.write_text(json.dumps(meta, indent=2, default=str))


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def _group_by_day(hours: list) -> list:
    """[Timestamp,...] -> [(date, [hour,...]), ...] sorted."""
    by_day: dict = {}
    for ts in hours:
        by_day.setdefault(ts.date(), []).append(ts.hour)
    return [(d, sorted(hs)) for d, hs in sorted(by_day.items())]


def _plan_work(pair: str, start, end, ledger: dict, meta: dict,
               incremental: bool, repair: bool) -> tuple:
    """Decide which (day, hours) to fetch. Returns (work, mode, note)."""
    if repair:
        hours = repair_work(ledger, start, end)
        return _group_by_day(hours), "repair", None
    if incremental:
        last = last_attempted_date(ledger)
        if last is None and pair in meta:
            last = datetime.date.fromisoformat(meta[pair]["last_date"])
        if last is not None:
            if last >= end - datetime.timedelta(days=1):
                return [], "incremental", f"Already up to date ({last})"
            start = last + datetime.timedelta(days=1)
    mode = "incremental" if incremental else "download"
    work = [(d, list(session_hours(d))) for d in session_days(start, end)]
    return work, mode, None


def download_symbol(
    pair: str,
    start: Optional[datetime.date],
    end: Optional[datetime.date],
    incremental: bool = False,
    repair: bool = False,
    symbol_idx: int = 0,
    total_symbols: int = 1,
) -> tuple:
    """Fetch, sweep, merge and save one symbol.

    Returns (merged M5 DataFrame or None, list of hours still lost after the sweep).
    """
    point_divider = get_point_divider(pair)
    meta = load_meta()
    ledger_file = ledger_path(LEDGER_DIR, pair)
    ledger = load_ledger(ledger_file)
    m5_path = COMPILED_DIR / f"{pair}_M5.csv"
    h1_path = COMPILED_DIR / f"{pair}_H1.csv"
    existing_df = pd.read_csv(m5_path, parse_dates=["time"]) if m5_path.exists() else None

    if repair and not ledger:
        if not h1_path.exists():
            log.info(f"  [{pair}] Nothing to repair (no ledger and no compiled data)")
            return existing_df, []
        ledger = seed_from_h1(h1_path)
        log.info(f"  [{pair}] Seeded ledger from H1: {len(ledger):,} hours with data")

    work, mode, note = _plan_work(pair, start, end, ledger, meta, incremental, repair)
    status_update(current_symbol=pair, symbol_progress=f"{symbol_idx}/{total_symbols}",
                  mode=mode)
    if note:
        log.info(f"  [{pair}] {note}")
        status_update(symbol_status="up_to_date")
        return existing_df, []
    if not work:
        log.info(f"  [{pair}] Nothing to fetch")
        status_update(symbol_status="up_to_date")
        if repair:
            save_ledger(ledger_file, ledger)
        return existing_df, []

    n_hours = sum(len(hs) for _, hs in work)
    log.info(f"  [{pair}] {mode}: {len(work)} days / {n_hours:,} hours "
             f"({work[0][0]} -> {work[-1][0]})...")
    status_update(symbol_status="downloading", days_total=len(work), days_completed=0,
                  hours_failed=0, hours_empty=0, hours_lost=0, ticks_total=0)

    PERF.reset()
    rss_at_start_mb = rss_peak_mb()
    rss_at_last_log_mb = rss_at_start_mb
    log.info(f"    [{pair}] RSS at start: {rss_at_start_mb:.0f} MB (peak)")

    m5_frames: list = []
    run_status: dict = {}          # Timestamp -> fetch status for this run
    completed = 0
    ticks_total = 0
    sym_t0 = time.time()

    def _progress(force: bool = False):
        nonlocal rss_at_last_log_mb
        if not (force or completed % 20 == 0):
            return
        elapsed = time.time() - sym_t0
        rate = completed / elapsed if elapsed > 0 else 0
        eta_m = ((len(work) - completed) / rate / 60) if rate > 0 else 0
        snap = PERF.snapshot()
        rss_mb = rss_peak_mb()
        rss_delta = rss_mb - rss_at_last_log_mb
        rss_at_last_log_mb = rss_mb
        mb_dl = snap.get("bytes_downloaded", 0) / (1024 ** 2)
        n_failed = sum(1 for s in run_status.values() if s == "failed")
        n_empty = sum(1 for s in run_status.values() if s == "empty")
        log.info(
            f"    [{pair}] {completed}/{len(work)} days  {ticks_total:,} ticks  "
            f"{rate:.1f} days/s  ETA {eta_m:.0f}m  RSS {rss_mb:.0f}MB (+{rss_delta:.0f})  "
            f"DL {mb_dl:.0f}MB  net/dec/res {snap.get('net_seconds', 0):.0f}/"
            f"{snap.get('decode_seconds', 0):.0f}/{snap.get('resample_seconds', 0):.0f}s  "
            f"retries={int(snap.get('http_retries', 0))} failed_hours={n_failed} "
            f"empty_hours={n_empty}  "
            f"day p95/max {snap.get('day_p95_s', 0):.1f}/{snap.get('day_max_s', 0):.1f}s"
        )
        status_update(
            days_completed=completed, ticks_total=ticks_total,
            rate_days_per_sec=round(rate, 2), eta_minutes=round(eta_m, 1),
            rss_peak_mb=round(rss_mb, 1), bytes_downloaded_mb=round(mb_dl, 1),
            net_seconds=round(snap.get("net_seconds", 0), 1),
            decode_seconds=round(snap.get("decode_seconds", 0), 1),
            resample_seconds=round(snap.get("resample_seconds", 0), 1),
            http_retries=int(snap.get("http_retries", 0)),
            http_failures=int(snap.get("http_failures", 0)),
            hours_failed=n_failed, hours_empty=n_empty,
            day_p95_seconds=round(snap.get("day_p95_s", 0), 2),
            day_max_seconds=round(snap.get("day_max_s", 0), 2),
        )

    with ThreadPoolExecutor(max_workers=DAY_WORKERS) as pool:
        futures = {
            pool.submit(download_day_ticks, pair, day, point_divider, hours): (day, hours)
            for day, hours in work
        }
        for future in as_completed(futures):
            day, hours = futures[future]
            try:
                day_m5, day_ticks, day_status = future.result()
            except Exception as e:                       # defensive: treat as all failed
                log.warning(f"    [{pair}] Day {day} raised {e!r}; marking hours failed")
                day_m5, day_ticks, day_status = _EMPTY_M5_DF.copy(), 0, {h: "failed" for h in hours}
            if not day_m5.empty:
                m5_frames.append(day_m5)
            for h, st in day_status.items():
                run_status[hour_ts(day, h)] = st
            ticks_total += day_ticks
            completed += 1
            _progress(force=(completed == len(work)))
            log.debug(f"    [{pair}] {day}: {day_ticks:,} ticks")

    # --- Sweep: one sequential retry pass over hours that failed in the main pass
    failed_hours = sorted(ts for ts, st in run_status.items() if st == "failed")
    hours_failed = len(failed_hours)
    if failed_hours:
        log.info(f"  [{pair}] Sweep: re-fetching {hours_failed} failed hours sequentially...")
        status_update(symbol_status="sweeping", hours_failed=hours_failed)
        sweep_ticks: list = []
        for ts in failed_hours:
            _, st, tdf = _download_and_decode_hour(pair, ts.date(), ts.hour, point_divider)
            run_status[ts] = st
            if not tdf.empty:
                sweep_ticks.append(tdf)
                ticks_total += len(tdf)
        if sweep_ticks:
            m5_frames.append(ticks_to_m5(pd.concat(sweep_ticks, ignore_index=True)))

    lost = sorted(ts for ts, st in run_status.items() if st == "failed")
    hours_empty = sum(1 for st in run_status.values() if st == "empty")

    # --- Merge with whatever is already on disk
    log.info(f"  [{pair}] Combining {len(m5_frames)} chunks of M5 bars...")
    status_update(symbol_status="combining")
    concat_t0 = time.perf_counter()
    new_m5 = (pd.concat(m5_frames, ignore_index=True) if m5_frames else _EMPTY_M5_DF.copy())
    if existing_df is not None and not new_m5.empty:
        m5_df = pd.concat([existing_df, new_m5], ignore_index=True)
    elif existing_df is not None:
        m5_df = existing_df
    else:
        m5_df = new_m5
    if not m5_df.empty:
        m5_df = (m5_df.drop_duplicates(subset=["time"], keep="last")
                 .sort_values("time").reset_index(drop=True))
    del m5_frames
    concat_seconds = time.perf_counter() - concat_t0

    snap = PERF.snapshot()
    log.info(
        f"    [{pair}] PERF summary: elapsed {(time.time() - sym_t0)/60:.1f}m  "
        f"DL {_fmt_bytes(snap.get('bytes_downloaded', 0))} "
        f"(decompressed {_fmt_bytes(snap.get('bytes_decompressed', 0))})  "
        f"ticks {int(snap.get('ticks_decoded', 0)):,}  "
        f"net {snap.get('net_seconds', 0):.0f}s  decode {snap.get('decode_seconds', 0):.0f}s  "
        f"resample {snap.get('resample_seconds', 0):.0f}s  concat {concat_seconds:.1f}s  "
        f"http: {int(snap.get('http_requests', 0))} req / {int(snap.get('http_404', 0))} 404 / "
        f"{int(snap.get('http_empty', 0))} empty / {int(snap.get('http_retries', 0))} retries / "
        f"{int(snap.get('http_failures', 0))} give-ups  "
        f"hours: {hours_failed} failed in main pass, {len(lost)} lost after sweep  "
        f"RSS peak {rss_peak_mb():.0f}MB (Δ +{rss_peak_mb() - rss_at_start_mb:.0f})"
    )

    # --- Persist ledger regardless of whether we got bars
    for ts, st in run_status.items():
        ledger[ts] = FETCH_TO_LEDGER[st]
    save_ledger(ledger_file, ledger)

    if m5_df.empty:
        log.warning(f"  [{pair}] No data retrieved")
        status_update(symbol_status="completed", hours_failed=hours_failed,
                      hours_empty=hours_empty, hours_lost=len(lost))
        return None, lost

    status_update(symbol_status="saving")
    m5_df.to_csv(m5_path, index=False)
    for tf_name, rule in [("H1", "1h"), ("H4", "4h"), ("D1", "1D")]:
        tf_df = resample_ohlc(m5_df, rule)
        tf_df.to_csv(COMPILED_DIR / f"{pair}_{tf_name}.csv", index=False)
        log.debug(f"  [{pair}] {tf_name}: {len(tf_df):,} bars")

    last_ts = pd.Timestamp(m5_df["time"].max())
    meta[pair] = {"last_date": str(last_ts.date()), "bars_m5": len(m5_df),
                  "updated": str(datetime.date.today())}
    save_meta(meta)

    unique_days = m5_df["time"].dt.date.nunique()
    log.info(f"  [{pair}] Done: {len(m5_df):,} M5 bars, {unique_days} days  "
             f"({m5_df['time'].min()} -> {m5_df['time'].max()})")
    if lost:
        log.warning(f"  [{pair}] {len(lost)} hour(s) LOST after retries and sweep:")
        for a, b, n in group_runs(lost)[:50]:
            log.warning(f"      {a:%Y-%m-%d %H:%M} -> {b:%Y-%m-%d %H:%M}  ({n}h)")
        log.warning(f"  [{pair}] run: python3 download.py --repair --symbols {pair}")
    status_update(symbol_status="completed", bars_m5=len(m5_df), unique_days=unique_days,
                  hours_failed=hours_failed, hours_empty=hours_empty, hours_lost=len(lost))
    return m5_df, lost


def run(
    symbols: list,
    years: int = DEFAULT_YEARS,
    incremental: bool = False,
    start_override: Optional[datetime.date] = None,
    end_override: Optional[datetime.date] = None,
    repair: bool = False,
) -> int:
    """Run the pipeline for all symbols. Returns the total number of lost hours."""
    if repair:
        start, end = start_override, end_override          # None = whole ledger range
        range_txt = f"{start or 'ledger start'} -> {end or 'ledger end'} (repair)"
    else:
        end = end_override or (datetime.date.today() - datetime.timedelta(days=1))
        start = start_override or (end - datetime.timedelta(days=365 * years))
        range_txt = f"{start} -> {end}"
    mode = "repair" if repair else ("incremental" if incremental else "download")

    COMPILED_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)

    header = (
        f"{'='*60}\n"
        f"Dukascopy Direct Tick Downloader\n"
        f"{'='*60}\n"
        f"  Symbols:     {', '.join(symbols)}\n"
        f"  Range:       {range_txt}\n"
        f"  Mode:        {mode}\n"
        f"  Day workers: {DAY_WORKERS}  |  Hour workers: {HOUR_WORKERS}\n"
        f"  Output:      {COMPILED_DIR}/\n"
        f"  Ledger:      {LEDGER_DIR}/\n"
        f"  Log:         {LOG_DIR}/\n"
        f"  Status:      {STATUS_FILE}\n"
        f"{'='*60}"
    )
    log.info(header)
    status_update(state="running", symbols=symbols, date_range=range_txt, mode=mode,
                  incremental=incremental,
                  started=datetime.datetime.now().isoformat(timespec="seconds"),
                  symbols_completed=[], symbols_remaining=list(symbols),
                  hours_lost=0, lost_hours=[])

    frames = []
    t0 = time.time()
    completed_symbols = []
    all_lost: list = []

    for i, sym in enumerate(symbols, 1):
        sym_t0 = time.time()
        status_update(symbols_remaining=[s for s in symbols if s not in completed_symbols and s != sym])
        df, lost = download_symbol(sym, start, end, incremental=incremental, repair=repair,
                                   symbol_idx=i, total_symbols=len(symbols))
        all_lost.extend(lost)
        elapsed = time.time() - sym_t0
        if df is not None:
            frames.append((sym, df))
            completed_symbols.append(sym)
            log.info(f"  [{sym}] Finished in {elapsed/60:.1f}m\n")
        else:
            log.warning(f"  [{sym}] No data\n")
        status_update(symbols_completed=completed_symbols, hours_lost=len(all_lost),
                      lost_hours=[t.strftime("%Y-%m-%dT%H:00:00Z") for t in all_lost[:200]])

    if frames:
        log.info("Building combined all_pairs_M5.csv...")
        combined = pd.concat([df.assign(symbol=sym) for sym, df in frames], ignore_index=True
                             ).sort_values(["symbol", "time"]).reset_index(drop=True)
        combined.to_csv(COMPILED_DIR / "all_pairs_M5.csv", index=False)

    total_m = (time.time() - t0) / 60
    summary_lines = [f"\n{'='*60}", f"COMPLETE  ({total_m:.1f} minutes total)", f"{'='*60}"]
    for sym, df in frames:
        summary_lines.append(
            f"  {sym:<10} {len(df):>9,} M5 bars  {df['time'].dt.date.nunique():>5} days   "
            f"{df['time'].min()} -> {df['time'].max()}")
    if all_lost:
        summary_lines.append(f"\n  WARNING: {len(all_lost)} hour(s) lost after retries and sweep. "
                             f"Run: python3 download.py --repair")
    summary_lines.append(f"\n  Timeframes: M5, H1, H4, D1")
    summary_lines.append(f"  Files in:   {COMPILED_DIR}/")
    log.info("\n".join(summary_lines))

    status_update(state="completed", total_minutes=round(total_m, 1),
                  finished=datetime.datetime.now().isoformat(timespec="seconds"),
                  hours_lost=len(all_lost),
                  lost_hours=[t.strftime("%Y-%m-%dT%H:00:00Z") for t in all_lost[:200]])
    return len(all_lost)


# ---------------------------------------------------------------------------
# Destructive actions (--clear / --fresh)
# ---------------------------------------------------------------------------

def symbol_data_files(pair: str) -> list:
    """Existing compiled CSVs and the ledger for one symbol."""
    paths = [COMPILED_DIR / f"{pair}_{tf}.csv" for tf in ("M5", "H1", "H4", "D1")]
    paths.append(ledger_path(LEDGER_DIR, pair))
    return [p for p in paths if p.exists()]


def confirm_delete(symbols: list, assume_yes: bool, stdin=None, out=print) -> bool:
    """Print what will be deleted plus a backup reminder; require the literal
    word DELETE unless assume_yes. Never deletes on a silent default."""
    stdin = stdin if stdin is not None else sys.stdin
    files = [p for s in symbols for p in symbol_data_files(s)]
    out(f"About to DELETE compiled data and ledger for {', '.join(symbols)}:")
    if not files:
        out("  (no files found)")
    for p in files:
        try:
            rel = p.relative_to(BASE_DIR)
        except ValueError:
            rel = p
        out(f"  {str(rel):<40} {_fmt_bytes(p.stat().st_size):>10}")
    out("This cannot be undone. Back up compiled/ and ledger/ first if you need them")
    out(f"(e.g. cp -r compiled ledger ~/duka-backup-{datetime.date.today()}).")
    if assume_yes:
        out("--yes given: skipping confirmation.")
        return True
    if not getattr(stdin, "isatty", lambda: False)():
        out("stdin is not a terminal; pass --yes to confirm non-interactively. Aborting.")
        return False
    out("Type DELETE to continue:")
    answer = stdin.readline().strip()
    if answer != "DELETE":
        out("Aborted. Nothing was deleted.")
        return False
    return True


def delete_symbol_data(symbols: list) -> list:
    deleted = []
    for s in symbols:
        for p in symbol_data_files(s):
            p.unlink()
            deleted.append(p)
    return deleted


def clear_symbols(symbols: list, assume_yes: bool) -> int:
    """--clear entry point. Returns an exit code."""
    if not confirm_delete(symbols, assume_yes):
        return 2
    deleted = delete_symbol_data(symbols)
    for p in deleted:
        log.info(f"deleted {p}")
    log.info(f"Cleared {len(deleted)} file(s) for {', '.join(symbols)}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Dukascopy tick data and compile to OHLC bars",
        epilog="Exit codes: 0 ok, 1 hours still lost after retries (run --repair), "
               "2 destructive action aborted.",
    )
    parser.add_argument("--symbols", nargs="+", default=SYMBOLS,
                        help=f"Pairs to download (default: {' '.join(SYMBOLS)})")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS,
                        help=f"Years of history (default: {DEFAULT_YEARS})")
    parser.add_argument("--start", type=str, default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None, help="End date, exclusive (YYYY-MM-DD)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--incremental", action="store_true",
                      help="Only download days after the last attempted day in the ledger")
    mode.add_argument("--repair", action="store_true",
                      help="Re-fetch hours that failed or were never attempted (ledger-driven); "
                           "--start/--end narrow the range")
    mode.add_argument("--fresh", action="store_true",
                      help="Delete the symbols' compiled files and ledger first, then download")
    mode.add_argument("--clear", action="store_true",
                      help="Delete the symbols' compiled files and ledger, then exit")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the DELETE confirmation for --fresh/--clear")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    symbols = [s.upper() for s in args.symbols]
    start = datetime.date.fromisoformat(args.start) if args.start else None
    end = datetime.date.fromisoformat(args.end) if args.end else None

    if args.clear:
        return clear_symbols(symbols, args.yes)
    if args.fresh:
        if not confirm_delete(symbols, args.yes):
            return 2
        for p in delete_symbol_data(symbols):
            log.info(f"deleted {p}")

    lost = run(symbols=symbols, years=args.years, incremental=args.incremental,
               start_override=start, end_override=end, repair=args.repair)
    return 1 if lost else 0


if __name__ == "__main__":
    sys.exit(main())
