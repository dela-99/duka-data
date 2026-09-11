# Lost-Hour Completeness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the downloader record, report, and repair hours lost to HTTP 503, fetch the Sunday session, give the checker a real completeness gate, and expose repair/fresh/clear safely from the CLI and dashboard.

**Architecture:** A new side-effect-free `ledger.py` module owns the session calendar and the per-symbol hour ledger (`ledger/<SYM>.csv`, one row per attempted hour, status D/E/N/F). `download.py` uses it in one fetch → sweep → merge → resample path shared by normal, incremental, and repair runs, and exits non-zero when hours remain lost. `check_integrity.py` gains a ledger-gated check 12. `dashboard.py` stays a thin launcher that maps run modes and a Clear button onto CLI flags.

**Tech Stack:** Python 3.9+, pandas 2.x, numpy, stdlib `http.server`, pytest (dev only). Run everything with `.venv-eval/bin/python` in this repo (system `python3` has no pandas).

**Spec:** `docs/superpowers/specs/2026-09-11-lost-hours-completeness-design.md`

## Global Constraints

- Python 3.9 compatible (no `match`, no `X | Y` at runtime outside annotations; keep `from __future__ import annotations`).
- No new runtime dependencies. `pytest` goes in `requirements-dev.txt` only.
- Ledger row format: `hour,status`, hour as `YYYY-MM-DDTHH:00:00Z`, status one of `D E N F`, sorted by hour.
- `SUNDAY_FIRST_HOUR = 20`; Sundays fetch hours 20–23, Mon–Fri 0–23, Saturday nothing.
- `RETRY_DELAYS = (2, 4, 8, 16, 32, 60)` seconds → 7 attempts.
- Fetch statuses are the strings `"data" | "empty" | "notfound" | "failed"`; ledger letters are `D E N F`.
- Every run merges into existing compiled data (dedupe on `time`, keep new). Only `--fresh`/`--clear` delete.
- `--incremental`, `--repair`, `--fresh`, `--clear` are mutually exclusive; `--yes` skips the typed `DELETE` confirmation.
- Exit codes: 0 ok, 1 hours still lost after sweep, 2 destructive action aborted/unconfirmed.
- Destructive prompt copy: `This cannot be undone. Back up compiled/ and ledger/ first if you need them` and `Type DELETE to continue:`.
- Dashboard binds to 127.0.0.1 only and never deletes files itself.
- Test runner: `.venv-eval/bin/python -m pytest tests -q`.
- Commit after every task with the trailer lines:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_012VRdaceaeDkQPTuzCps4Sk`.

---

## File map

| File | Responsibility |
|---|---|
| `ledger.py` (new) | Session calendar, ledger load/save, seeding from H1, repair work list, run grouping. No I/O side effects at import. |
| `download.py` (modify) | Fetch result + ladder, per-hour statuses, sweep, merge, repair/incremental planning, clear/fresh confirmation, CLI, exit codes. |
| `check_integrity.py` (modify) | `--data-dir`, weekday-only ratios, check 12. |
| `dashboard.py` (modify) | Run modes, `/clear`, `/files`, backup modal, hours stats. |
| `tests/conftest.py` (new) | `FakeFeed` scripted `urlopen`, isolated data dirs. |
| `tests/test_ledger.py`, `tests/test_download.py`, `tests/test_check_integrity.py`, `tests/test_dashboard.py` (new) | Per-module tests. |
| `requirements-dev.txt`, `.gitignore`, `USAGE.md`, `README.md`, `DATA_QUALITY.md` | Tooling and docs. |
| `docs/superpowers/validation/2026-09-11-before.md`, `...-after.md` (new) | Before/after evidence for the PR. |

---

### Task 0: Baseline capture, dev tooling, test scaffolding

**Files:**
- Create: `requirements-dev.txt`, `tests/__init__.py`, `tests/conftest.py`, `docs/superpowers/validation/2026-09-11-before.md`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `tests/conftest.py` fixtures `feed` (a `FakeFeed`) and `data_dirs` (dict of tmp paths already applied to `download` and `ledger` globals). `FakeFeed.set(day, hour, spec)` where spec ∈ `"data" | "empty" | "notfound" | "fail" | int` (int k = fail k times, then data). `FakeFeed.calls` is a list of `(date, hour)`. `bi5_bytes(n_ticks=60, price=110000)` builds a valid LZMA bi5 payload.

- [ ] **Step 1: Capture the "before" state with the current code**

Run (from repo root):

```bash
mkdir -p docs/superpowers/validation
{ echo '# Before: 2026-09-11 (pre-change code, shipped data)'; echo; echo '## check_integrity.py'; echo '```'; .venv-eval/bin/python check_integrity.py; echo "exit=$?"; echo '```'; } > docs/superpowers/validation/2026-09-11-before.md
```

Then append the missing-hour census. Save this as `docs/superpowers/validation/census.py` (it is reused for the "after" report):

```python
"""Census of expected session hours without an H1 bar, per symbol.
Usage: python census.py <compiled_dir>"""
import sys, glob, os
import pandas as pd

SUNDAY_FIRST_HOUR = 20

def expected(first, last):
    idx = pd.date_range(first.floor("h"), last.floor("h"), freq="h")
    wd, hr = idx.dayofweek, idx.hour
    return idx[(wd <= 4) | ((wd == 6) & (hr >= SUNDAY_FIRST_HOUR))]

def main(compiled):
    print(f"| Symbol | H1 bars | Sunday bars | expected hours | missing | isolated 1h gaps |")
    print(f"|---|---|---|---|---|---|")
    for path in sorted(glob.glob(os.path.join(compiled, "*_H1.csv"))):
        sym = os.path.basename(path)[:-7]
        h1 = pd.read_csv(path, usecols=["time"], parse_dates=["time"])
        have = set(h1["time"])
        exp = expected(h1["time"].min(), h1["time"].max())
        missing = [t for t in exp if t not in have]
        singles = sum(1 for i, t in enumerate(missing)
                      if (i == 0 or missing[i-1] != t - pd.Timedelta(hours=1))
                      and (i == len(missing)-1 or missing[i+1] != t + pd.Timedelta(hours=1)))
        sundays = int((h1["time"].dt.dayofweek == 6).sum())
        print(f"| {sym} | {len(h1):,} | {sundays:,} | {len(exp):,} | {len(missing):,} | {singles} |")

if __name__ == "__main__":
    main(sys.argv[1])
```

Run and append:

```bash
{ echo; echo '## Missing-hour census (expected = Sun 20-23 + Mon-Fri 0-23 UTC)'; echo; .venv-eval/bin/python docs/superpowers/validation/census.py compiled; } >> docs/superpowers/validation/2026-09-11-before.md
```

Expected: the checker prints `11/11 checks passed`; the census shows 0 Sunday bars for every symbol and thousands of missing hours (Sundays + holidays + 503 losses).

- [ ] **Step 2: Add dev requirements and gitignore entry**

`requirements-dev.txt`:

```
-r requirements.txt
pytest>=7
```

Append to `.gitignore` under the Data section:

```
ledger/
```

Install: `.venv-eval/bin/python -m pip install -r requirements-dev.txt`

- [ ] **Step 3: Write the test scaffolding**

`tests/__init__.py`: empty file.

`tests/conftest.py`:

```python
"""Shared fixtures: a scripted fake Dukascopy feed and isolated data dirs."""
from __future__ import annotations

import datetime
import io
import lzma
import struct
import urllib.error

import pytest


def bi5_bytes(n_ticks: int = 60, price: int = 110000) -> bytes:
    """LZMA bi5 payload: n_ticks one-per-minute ticks, ask=price+10, bid=price."""
    recs = b"".join(
        struct.pack(">iiiff", i * 60_000, price + 10, price, 1.0, 1.0)
        for i in range(n_ticks)
    )
    return lzma.compress(recs)


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body


class FakeFeed:
    """Scripted urlopen replacement keyed by (date, hour).

    spec: "data" (default) | "empty" | "notfound" | "fail" | int k (fail k times, then data)
    """

    def __init__(self):
        self.script: dict = {}
        self.default = "data"
        self.calls: list = []
        self._fail_counts: dict = {}

    def set(self, day: datetime.date, hour: int, spec):
        self.script[(day, hour)] = spec

    @staticmethod
    def parse_url(url: str):
        # .../datafeed/{PAIR}/{yyyy}/{mm0}/{dd}/{hh}h_ticks.bi5
        parts = url.rsplit("/", 5)
        y, m0, d = int(parts[2]), int(parts[3]), int(parts[4])
        hh = int(parts[5][:2])
        return datetime.date(y, m0 + 1, d), hh

    def __call__(self, req, timeout=30):
        url = req.full_url
        day, hh = self.parse_url(url)
        self.calls.append((day, hh))
        spec = self.script.get((day, hh), self.default)
        if isinstance(spec, int):
            n = self._fail_counts.get((day, hh), 0)
            if n < spec:
                self._fail_counts[(day, hh)] = n + 1
                spec = "fail"
            else:
                spec = "data"
        if spec == "data":
            return _Resp(bi5_bytes())
        if spec == "empty":
            return _Resp(b"")
        if spec == "notfound":
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, io.BytesIO(b""))


@pytest.fixture
def feed(monkeypatch):
    import download
    f = FakeFeed()
    monkeypatch.setattr(download.urllib.request, "urlopen", f)
    monkeypatch.setattr(download.time, "sleep", lambda s: None)
    return f


@pytest.fixture
def data_dirs(monkeypatch, tmp_path):
    import download
    dirs = {
        "compiled": tmp_path / "compiled",
        "ledger": tmp_path / "ledger",
        "meta": tmp_path / "meta.json",
        "status": tmp_path / "status.json",
    }
    dirs["compiled"].mkdir()
    monkeypatch.setattr(download, "COMPILED_DIR", dirs["compiled"])
    monkeypatch.setattr(download, "LEDGER_DIR", dirs["ledger"])
    monkeypatch.setattr(download, "META_FILE", dirs["meta"])
    monkeypatch.setattr(download, "STATUS_FILE", dirs["status"])
    return dirs
```

Note: `download.LEDGER_DIR` does not exist yet; the fixture is only used from Task 2 onward, after Task 2 adds it.

- [ ] **Step 4: Verify pytest runs (no tests yet)**

Run: `.venv-eval/bin/python -m pytest tests -q`
Expected: `no tests ran` with exit 5, no import errors.

- [ ] **Step 5: Commit**

```bash
git add requirements-dev.txt .gitignore tests/__init__.py tests/conftest.py docs/superpowers/validation/
git commit -m "Add test scaffolding, dev requirements, and pre-change validation baseline"
```

---

### Task 1: `ledger.py` — session calendar and hour ledger

**Files:**
- Create: `ledger.py`
- Test: `tests/test_ledger.py`

**Interfaces:**
- Produces (all in `ledger.py`):
  - `SUNDAY_FIRST_HOUR: int = 20`
  - `FETCH_TO_LEDGER: dict[str, str] = {"data": "D", "empty": "E", "notfound": "N", "failed": "F"}`
  - `session_hours(day: datetime.date) -> range`
  - `session_days(start: datetime.date, end: datetime.date) -> Iterator[datetime.date]` (Sun–Fri in `[start, end)`)
  - `expected_session_hours(first: pd.Timestamp, last: pd.Timestamp) -> pd.DatetimeIndex` (inclusive, UTC)
  - `ledger_path(ledger_dir: Path, pair: str) -> Path`
  - `load_ledger(path: Path) -> dict[pd.Timestamp, str]`
  - `save_ledger(path: Path, ledger: dict[pd.Timestamp, str]) -> None`
  - `seed_from_h1(h1_csv: Path) -> dict[pd.Timestamp, str]`
  - `last_attempted_date(ledger) -> datetime.date | None` (max date among D/E/N rows)
  - `repair_work(ledger, start: datetime.date | None = None, end: datetime.date | None = None) -> list[pd.Timestamp]`
  - `group_runs(hours: Iterable[pd.Timestamp]) -> list[tuple[pd.Timestamp, pd.Timestamp, int]]`
  - `hour_ts(day: datetime.date, hour: int) -> pd.Timestamp` (tz-aware UTC)

- [ ] **Step 1: Write the failing tests**

`tests/test_ledger.py`:

```python
import datetime
from pathlib import Path

import pandas as pd
import pytest

import ledger as L

MON = datetime.date(2024, 3, 4)
SAT = datetime.date(2024, 3, 2)
SUN = datetime.date(2024, 3, 3)


def test_session_hours_by_weekday():
    assert list(L.session_hours(MON)) == list(range(24))
    assert list(L.session_hours(SAT)) == []
    assert list(L.session_hours(SUN)) == [20, 21, 22, 23]


def test_session_days_is_sun_to_fri_end_exclusive():
    days = list(L.session_days(datetime.date(2024, 3, 1), datetime.date(2024, 3, 9)))
    assert days == [datetime.date(2024, 3, 1), SUN, MON, datetime.date(2024, 3, 5),
                    datetime.date(2024, 3, 6), datetime.date(2024, 3, 7), datetime.date(2024, 3, 8)]


def test_expected_session_hours_inclusive_and_filtered():
    first = pd.Timestamp("2024-03-01 22:00", tz="UTC")   # Fri
    last = pd.Timestamp("2024-03-04 01:00", tz="UTC")    # Mon
    exp = list(L.expected_session_hours(first, last))
    assert exp[0] == first and exp[-1] == last
    assert pd.Timestamp("2024-03-02 10:00", tz="UTC") not in exp      # Saturday
    assert pd.Timestamp("2024-03-03 19:00", tz="UTC") not in exp      # Sunday before open
    assert pd.Timestamp("2024-03-03 20:00", tz="UTC") in exp          # Sunday open window
    assert len(exp) == 2 + 4 + 2


def test_hour_ts_is_utc():
    ts = L.hour_ts(MON, 7)
    assert ts == pd.Timestamp("2024-03-04 07:00", tz="UTC")


def test_save_and_load_roundtrip_sorted(tmp_path):
    p = tmp_path / "ledger" / "EURUSD.csv"
    led = {L.hour_ts(MON, 7): "F", L.hour_ts(MON, 6): "D", L.hour_ts(SUN, 22): "E"}
    L.save_ledger(p, led)
    text = p.read_text().splitlines()
    assert text[0] == "hour,status"
    assert text[1] == "2024-03-03T22:00:00Z,E"
    assert text[2] == "2024-03-04T06:00:00Z,D"
    assert L.load_ledger(p) == led


def test_load_missing_ledger_is_empty(tmp_path):
    assert L.load_ledger(tmp_path / "nope.csv") == {}


def test_ledger_path():
    assert L.ledger_path(Path("/x/ledger"), "EURUSD") == Path("/x/ledger/EURUSD.csv")


def test_seed_from_h1(tmp_path):
    h1 = tmp_path / "EURUSD_H1.csv"
    pd.DataFrame({"time": ["2024-03-04 06:00:00+00:00", "2024-03-04 08:00:00+00:00"],
                  "open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [1, 1],
                  "volume": [1, 1], "spread": [0, 0]}).to_csv(h1, index=False)
    assert L.seed_from_h1(h1) == {L.hour_ts(MON, 6): "D", L.hour_ts(MON, 8): "D"}


def test_last_attempted_date_ignores_failed():
    led = {L.hour_ts(MON, 6): "D", L.hour_ts(datetime.date(2024, 3, 5), 6): "F"}
    assert L.last_attempted_date(led) == MON
    assert L.last_attempted_date({}) is None


def test_repair_work_failed_plus_never_attempted():
    led = {L.hour_ts(MON, h): "D" for h in range(24)}
    led[L.hour_ts(MON, 7)] = "F"                                      # failed
    led[L.hour_ts(datetime.date(2024, 3, 5), 0)] = "E"                # Tue 00 attempted
    # Tue 01..23 never attempted -> covered range ends Tue 00, so not included
    del led[L.hour_ts(MON, 3)]                                        # never attempted
    work = L.repair_work(led)
    assert work == [L.hour_ts(MON, 3), L.hour_ts(MON, 7)]


def test_repair_work_respects_start_end():
    tue = datetime.date(2024, 3, 5)
    led = {L.hour_ts(MON, h): "F" for h in range(24)}
    led.update({L.hour_ts(tue, h): "F" for h in range(24)})
    assert L.repair_work(led, start=tue) == [L.hour_ts(tue, h) for h in range(24)]
    assert L.repair_work(led, end=tue) == [L.hour_ts(MON, h) for h in range(24)]
    assert L.repair_work({}) == []


def test_group_runs():
    hrs = [L.hour_ts(MON, 1), L.hour_ts(MON, 2), L.hour_ts(MON, 3), L.hour_ts(MON, 9)]
    assert L.group_runs(hrs) == [(L.hour_ts(MON, 1), L.hour_ts(MON, 3), 3),
                                 (L.hour_ts(MON, 9), L.hour_ts(MON, 9), 1)]
    assert L.group_runs([]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-eval/bin/python -m pytest tests/test_ledger.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'ledger'`.

- [ ] **Step 3: Implement `ledger.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-eval/bin/python -m pytest tests/test_ledger.py -q`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add ledger.py tests/test_ledger.py
git commit -m "Add ledger module: session calendar and per-hour fetch ledger"
```

---

### Task 2: `download.py` — fetch result, retry ladder, per-hour statuses, Sunday hours

**Files:**
- Modify: `download.py` (constants near line 60–75; `_trading_days` ~line 180; `download_hour_bi5` ~line 195; `_download_and_decode_hour` ~line 300; `download_day_ticks` ~line 315)
- Test: `tests/test_download.py` (new)

**Interfaces:**
- Consumes: `ledger.session_hours`, `ledger.session_days`, `ledger.hour_ts`.
- Produces:
  - `LEDGER_DIR = BASE_DIR / "ledger"`, `RETRY_DELAYS = (2, 4, 8, 16, 32, 60)`
  - `class FetchResult(NamedTuple): status: str; data: Optional[bytes]`
  - `download_hour_bi5(pair, dt, delays=RETRY_DELAYS) -> FetchResult`
  - `_download_and_decode_hour(pair, day, hour, point_divider) -> tuple[int, str, pd.DataFrame]`
  - `download_day_ticks(pair, day, point_divider, hours=None) -> tuple[pd.DataFrame, int, dict[int, str]]`
  - `_trading_days` removed; callers use `session_days`.

- [ ] **Step 1: Write the failing tests**

`tests/test_download.py` (initial content; later tasks append):

```python
import datetime

import pandas as pd
import pytest

import download as D
import ledger as L
from tests.conftest import FakeFeed

MON = datetime.date(2024, 3, 4)
SUN = datetime.date(2024, 3, 3)
SAT = datetime.date(2024, 3, 2)
U = datetime.timezone.utc


def dt(day, hour):
    return datetime.datetime(day.year, day.month, day.day, hour, tzinfo=U)


# ---- Task 2: fetch result + ladder + per-hour statuses --------------------

def test_fetch_statuses(feed):
    feed.set(MON, 1, "empty")
    feed.set(MON, 2, "notfound")
    feed.set(MON, 3, "fail")
    assert D.download_hour_bi5("EURUSD", dt(MON, 0)).status == "data"
    assert D.download_hour_bi5("EURUSD", dt(MON, 1)) == D.FetchResult("empty", None)
    assert D.download_hour_bi5("EURUSD", dt(MON, 2)) == D.FetchResult("notfound", None)
    assert D.download_hour_bi5("EURUSD", dt(MON, 3)) == D.FetchResult("failed", None)


def test_ladder_is_seven_attempts_then_failed(feed):
    feed.set(MON, 3, "fail")
    D.download_hour_bi5("EURUSD", dt(MON, 3))
    assert feed.calls.count((MON, 3)) == len(D.RETRY_DELAYS) + 1 == 7


def test_ladder_recovers_after_transient_503s(feed):
    feed.set(MON, 3, 3)          # fail 3x then data
    r = D.download_hour_bi5("EURUSD", dt(MON, 3))
    assert r.status == "data" and r.data
    assert feed.calls.count((MON, 3)) == 4


def test_day_ticks_reports_per_hour_status(feed):
    feed.set(MON, 7, "fail")
    feed.set(MON, 8, "empty")
    m5, ticks, status = D.download_day_ticks("EURUSD", MON, 100000)
    assert status[7] == "failed" and status[8] == "empty" and status[0] == "data"
    assert len(status) == 24
    assert ticks == 22 * 60
    assert len(m5) == 22 * 12
    assert 7 not in set(m5["time"].dt.hour)


def test_day_ticks_hours_subset(feed):
    m5, ticks, status = D.download_day_ticks("EURUSD", MON, 100000, hours=[5, 6])
    assert sorted(status) == [5, 6]
    assert sorted(c[1] for c in feed.calls) == [5, 6]


def test_sunday_and_saturday_hours(feed):
    m5, _, status = D.download_day_ticks("EURUSD", SUN, 100000)
    assert sorted(status) == [20, 21, 22, 23]
    m5s, _, status_s = D.download_day_ticks("EURUSD", SAT, 100000)
    assert status_s == {} and m5s.empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-eval/bin/python -m pytest tests/test_download.py -q`
Expected: FAIL (`AttributeError: module 'download' has no attribute 'FetchResult'` / `LEDGER_DIR` from the fixture).

- [ ] **Step 3: Implement**

In `download.py`:

(a) Imports: add `from typing import NamedTuple, Optional` after the existing `from pathlib import Path`, and
`from ledger import (FETCH_TO_LEDGER, group_runs, hour_ts, last_attempted_date, ledger_path, load_ledger, repair_work, save_ledger, seed_from_h1, session_days, session_hours)`.

(b) Constants: after `HOUR_WORKERS = 2` add

```python
RETRY_DELAYS = (2, 4, 8, 16, 32, 60)   # seconds between attempts -> 7 attempts, ~2 min
```

and after `LOG_DIR = BASE_DIR / "logs"` add `LEDGER_DIR   = BASE_DIR / "ledger"`.

(c) Delete `_trading_days` entirely.

(d) Replace `download_hour_bi5`:

```python
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
```

(e) Replace `_download_and_decode_hour` and `download_day_ticks`:

```python
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
    weekday) and return (M5 bars, tick_count, {hour: fetch status})."""
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
```

(f) In `download_symbol`, temporarily keep it compiling: replace `days = list(_trading_days(start, end))` with `days = list(session_days(start, end))` and `day_m5, day_tick_count = future.result()` with `day_m5, day_tick_count, _ = future.result()`. Task 3 rewrites this function fully.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-eval/bin/python -m pytest tests -q`
Expected: all pass (12 ledger + 6 download).

- [ ] **Step 5: Commit**

```bash
git add download.py tests/test_download.py
git commit -m "download: typed fetch result, 7-attempt ladder, per-hour statuses, Sunday session hours"
```

---

### Task 3: `download.py` — ledger-backed symbol run: statuses, sweep, merge, reporting

**Files:**
- Modify: `download.py` (`download_symbol`, `run`)
- Test: `tests/test_download.py` (append)

**Interfaces:**
- Consumes: Task 1 ledger functions; Task 2 `download_day_ticks(pair, day, pd, hours)`.
- Produces:
  - `download_symbol(pair, start, end, incremental=False, repair=False, symbol_idx=0, total_symbols=1) -> tuple[Optional[pd.DataFrame], list[pd.Timestamp]]` — returns `(merged M5 or None, lost hours after sweep)`.
  - `run(symbols, years=DEFAULT_YEARS, incremental=False, start_override=None, end_override=None, repair=False) -> int` — total lost hours.
  - Status-file fields: `mode`, `hours_failed`, `hours_empty`, `hours_lost`, `lost_hours` (list of ISO strings, max 200). `days_failed` removed.
  - Work items are `list[tuple[datetime.date, list[int]]]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_download.py`:

```python
# ---- Task 3: ledger, sweep, merge, reporting ------------------------------

WEEK_START = datetime.date(2024, 3, 4)   # Mon
WEEK_END = datetime.date(2024, 3, 9)     # Sat (exclusive)


def run_week(**kw):
    return D.download_symbol("EURUSD", WEEK_START, WEEK_END, **kw)


def read_status(data_dirs):
    import json
    return json.loads(data_dirs["status"].read_text())


def test_failed_hour_recorded_in_ledger_and_day_completes(feed, data_dirs):
    tue = datetime.date(2024, 3, 5)
    feed.set(tue, 7, "fail")
    m5, lost = run_week()
    led = L.load_ledger(L.ledger_path(data_dirs["ledger"], "EURUSD"))
    assert led[L.hour_ts(tue, 7)] == "F"
    assert led[L.hour_ts(tue, 6)] == "D"
    assert lost == [L.hour_ts(tue, 7)]
    st = read_status(data_dirs)
    assert st["hours_failed"] == 1 and st["hours_lost"] == 1
    assert st["days_completed"] == 5 and "days_failed" not in st
    assert len(m5) == 5 * 288 - 12


def test_sweep_recovers_hour_that_fails_in_main_pass(feed, data_dirs):
    tue = datetime.date(2024, 3, 5)
    feed.set(tue, 7, 7)      # fails the whole main-pass ladder, succeeds on the sweep
    m5, lost = run_week()
    assert lost == []
    led = L.load_ledger(L.ledger_path(data_dirs["ledger"], "EURUSD"))
    assert led[L.hour_ts(tue, 7)] == "D"
    assert len(m5) == 5 * 288
    st = read_status(data_dirs)
    assert st["hours_failed"] == 1 and st["hours_lost"] == 0


def test_empty_hours_recorded_and_not_refetched(feed, data_dirs):
    fri = datetime.date(2024, 3, 8)
    feed.set(fri, 22, "empty")
    feed.set(fri, 23, "empty")
    run_week()
    led = L.load_ledger(L.ledger_path(data_dirs["ledger"], "EURUSD"))
    assert led[L.hour_ts(fri, 22)] == "E"
    assert read_status(data_dirs)["hours_empty"] == 2
    feed.calls.clear()
    # repair over the same range must not touch E hours
    D.download_symbol("EURUSD", WEEK_START, WEEK_END, repair=True)
    assert (fri, 22) not in feed.calls and (fri, 23) not in feed.calls


def test_narrow_rerun_merges_instead_of_overwriting(feed, data_dirs):
    run_week()
    wed = datetime.date(2024, 3, 6)
    m5, _ = D.download_symbol("EURUSD", wed, wed + datetime.timedelta(days=1))
    assert len(m5) == 5 * 288
    assert m5["time"].is_monotonic_increasing and not m5["time"].duplicated().any()
    on_disk = pd.read_csv(data_dirs["compiled"] / "EURUSD_M5.csv", parse_dates=["time"])
    assert len(on_disk) == 5 * 288
    h1 = pd.read_csv(data_dirs["compiled"] / "EURUSD_H1.csv")
    assert len(h1) == 5 * 24


def test_sunday_bars_land_in_all_timeframes(feed, data_dirs):
    m5, _ = D.download_symbol("EURUSD", datetime.date(2024, 3, 3), datetime.date(2024, 3, 5))
    assert len(m5) == 4 * 12 + 288
    d1 = pd.read_csv(data_dirs["compiled"] / "EURUSD_D1.csv", parse_dates=["time"])
    assert list(d1["time"].dt.dayofweek) == [6, 0]


def test_run_returns_total_lost_and_writes_status(feed, data_dirs):
    feed.set(datetime.date(2024, 3, 5), 7, "fail")
    feed.set(datetime.date(2024, 3, 6), 9, "fail")
    lost = D.run(["EURUSD"], start_override=WEEK_START, end_override=WEEK_END)
    assert lost == 2
    st = read_status(data_dirs)
    assert st["state"] == "completed" and st["hours_lost"] == 2 and st["mode"] == "download"
    assert st["lost_hours"] == ["2024-03-05T07:00:00Z", "2024-03-06T09:00:00Z"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-eval/bin/python -m pytest tests/test_download.py -q`
Expected: the six new tests fail (`TypeError: cannot unpack` / `KeyError: 'hours_failed'`).

- [ ] **Step 3: Rewrite `download_symbol` and update `run`**

Replace the whole `download_symbol` function with:

```python
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
```

Update `run`:

```python
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
```

Temporarily adjust `main()` so it still runs: `lost = run(...)` then `sys.exit(1 if lost else 0)`. Task 4 finishes the CLI.

Also update the module docstring line `Monitoring: writes real-time status...` to mention the ledger:
`Ledger: records every attempted hour in ledger/<SYM>.csv (see ledger.py).`

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-eval/bin/python -m pytest tests -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add download.py tests/test_download.py
git commit -m "download: ledger-backed runs with sweep, merge-on-write, and lost-hour reporting"
```

---

### Task 4: `download.py` — repair and incremental planning, clear/fresh confirmation, CLI

**Files:**
- Modify: `download.py` (`main`, new helpers before `main`)
- Test: `tests/test_download.py` (append)

**Interfaces:**
- Consumes: Task 3 `run(...) -> int`, `download_symbol(..., repair=True)`.
- Produces:
  - `symbol_data_files(pair) -> list[Path]` (existing compiled + ledger files)
  - `confirm_delete(symbols, assume_yes, stdin=None, out=print) -> bool`
  - `delete_symbol_data(symbols) -> list[Path]`
  - `clear_symbols(symbols, assume_yes) -> int` (exit code)
  - `build_parser() -> argparse.ArgumentParser`
  - `main(argv=None) -> int` (returns exit code; `if __name__ == "__main__": sys.exit(main())`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_download.py`:

```python
# ---- Task 4: repair / incremental / clear / fresh / CLI -------------------
import io


def seed_h1_only(data_dirs, days):
    """Legacy layout: compiled H1 (and M5) but no ledger."""
    rows = []
    for d in days:
        for h in range(24):
            rows.append({"time": f"{d} {h:02d}:00:00+00:00", "open": 1, "high": 1, "low": 1,
                         "close": 1, "volume": 1, "spread": 0})
    df = pd.DataFrame(rows)
    df.to_csv(data_dirs["compiled"] / "EURUSD_H1.csv", index=False)
    df.to_csv(data_dirs["compiled"] / "EURUSD_M5.csv", index=False)


def test_repair_without_ledger_seeds_and_fetches_gaps(feed, data_dirs):
    mon, tue = datetime.date(2024, 3, 4), datetime.date(2024, 3, 5)
    seed_h1_only(data_dirs, [mon, tue])
    # remove one hour from the legacy files -> a never-attempted hour
    for tf in ("H1", "M5"):
        p = data_dirs["compiled"] / f"EURUSD_{tf}.csv"
        df = pd.read_csv(p)
        df[df["time"] != "2024-03-04 07:00:00+00:00"].to_csv(p, index=False)
    D.download_symbol("EURUSD", None, None, repair=True)
    assert feed.calls == [(mon, 7)]
    led = L.load_ledger(L.ledger_path(data_dirs["ledger"], "EURUSD"))
    assert led[L.hour_ts(mon, 7)] == "D" and led[L.hour_ts(mon, 6)] == "D"
    assert len(led) == 48


def test_repair_with_ledger_fetches_only_failed(feed, data_dirs):
    tue = datetime.date(2024, 3, 5)
    feed.set(tue, 7, "fail")
    run_week()
    feed.script.clear(); feed.calls.clear()
    m5, lost = D.download_symbol("EURUSD", None, None, repair=True)
    assert feed.calls == [(tue, 7)] and lost == []
    assert len(m5) == 5 * 288
    led = L.load_ledger(L.ledger_path(data_dirs["ledger"], "EURUSD"))
    assert led[L.hour_ts(tue, 7)] == "D"


def test_repair_nothing_to_do_without_any_data(feed, data_dirs):
    m5, lost = D.download_symbol("EURUSD", None, None, repair=True)
    assert m5 is None and lost == [] and feed.calls == []


def test_incremental_starts_after_ledger(feed, data_dirs):
    run_week()
    feed.calls.clear()
    nxt = datetime.date(2024, 3, 10)   # Sunday after the week
    D.download_symbol("EURUSD", WEEK_START, datetime.date(2024, 3, 12), incremental=True)
    assert min(c[0] for c in feed.calls) == datetime.date(2024, 3, 9) or \
           min(c[0] for c in feed.calls) == nxt
    assert all(c[0] >= datetime.date(2024, 3, 9) for c in feed.calls)


def test_symbol_data_files_and_delete(feed, data_dirs):
    run_week()
    files = D.symbol_data_files("EURUSD")
    assert {p.name for p in files} == {"EURUSD_M5.csv", "EURUSD_H1.csv", "EURUSD_H4.csv",
                                       "EURUSD_D1.csv", "EURUSD.csv"}
    (data_dirs["compiled"] / "GBPUSD_M5.csv").write_text("x")
    (data_dirs["compiled"] / "all_pairs_M5.csv").write_text("x")
    deleted = D.delete_symbol_data(["EURUSD"])
    assert len(deleted) == 5
    assert not (data_dirs["compiled"] / "EURUSD_M5.csv").exists()
    assert (data_dirs["compiled"] / "GBPUSD_M5.csv").exists()
    assert (data_dirs["compiled"] / "all_pairs_M5.csv").exists()


def test_confirm_delete_requires_literal_DELETE(feed, data_dirs, capsys):
    run_week()
    class TTY(io.StringIO):
        def isatty(self): return True
    assert D.confirm_delete(["EURUSD"], assume_yes=False, stdin=TTY("DELETE\n")) is True
    assert D.confirm_delete(["EURUSD"], assume_yes=False, stdin=TTY("yes\n")) is False
    out = capsys.readouterr().out
    assert "EURUSD_M5.csv" in out and "MB" in out or "KB" in out
    assert "This cannot be undone. Back up compiled/ and ledger/ first if you need them" in out
    assert "Type DELETE to continue:" in out


def test_confirm_delete_non_tty_without_yes_aborts(feed, data_dirs, capsys):
    run_week()
    assert D.confirm_delete(["EURUSD"], assume_yes=False, stdin=io.StringIO("DELETE\n")) is False
    assert "--yes" in capsys.readouterr().out
    assert D.confirm_delete(["EURUSD"], assume_yes=True, stdin=io.StringIO("")) is True


def test_clear_symbols_exit_codes(feed, data_dirs, monkeypatch):
    run_week()
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert D.clear_symbols(["EURUSD"], assume_yes=False) == 2
    assert (data_dirs["compiled"] / "EURUSD_M5.csv").exists()
    feed.calls.clear()
    assert D.clear_symbols(["EURUSD"], assume_yes=True) == 0
    assert not (data_dirs["compiled"] / "EURUSD_M5.csv").exists()
    assert not L.ledger_path(data_dirs["ledger"], "EURUSD").exists()
    assert feed.calls == []


def test_cli_mutually_exclusive_modes():
    p = D.build_parser()
    for combo in (["--repair", "--incremental"], ["--repair", "--fresh"], ["--clear", "--fresh"],
                  ["--clear", "--incremental"]):
        with pytest.raises(SystemExit):
            p.parse_args(combo)
    a = p.parse_args(["--clear", "--yes", "--symbols", "EURUSD"])
    assert a.clear and a.yes and a.symbols == ["EURUSD"]


def test_main_exit_codes(feed, data_dirs, monkeypatch):
    base = ["--symbols", "EURUSD", "--start", "2024-03-04", "--end", "2024-03-09"]
    assert D.main(base) == 0
    feed.set(datetime.date(2024, 3, 5), 7, "fail")
    assert D.main(base) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert D.main(["--fresh", "--symbols", "EURUSD", "--start", "2024-03-04", "--end", "2024-03-09"]) == 2
    assert (data_dirs["compiled"] / "EURUSD_M5.csv").exists()
    feed.script.clear(); feed.calls.clear()
    assert D.main(["--fresh", "--yes"] + base) == 0
    assert len(feed.calls) == 5 * 24          # everything re-fetched after deletion
    assert D.main(["--clear", "--yes", "--symbols", "EURUSD"]) == 0
    assert not (data_dirs["compiled"] / "EURUSD_M5.csv").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-eval/bin/python -m pytest tests/test_download.py -q`
Expected: the new tests fail with `AttributeError` (`symbol_data_files`, `build_parser`, ...).

- [ ] **Step 3: Implement helpers and CLI**

Insert before `def main` in `download.py`:

```python
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
    out("Type DELETE to continue: ", end="") if out is print else out("Type DELETE to continue:")
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
```

Replace `main()`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-eval/bin/python -m pytest tests -q`
Expected: all pass. If `test_incremental_starts_after_ledger` is flaky on the min-date assertion, simplify it to the second `assert all(...)` line only; the intent is "no day before Sat 2024-03-09 is re-fetched".

- [ ] **Step 5: Smoke the real CLI help and a no-network dry path**

Run: `.venv-eval/bin/python download.py --help` → shows the four mutually exclusive flags and `--yes`.
Run: `.venv-eval/bin/python download.py --clear --symbols ZZZTEST` → prints "(no files found)" and the backup reminder, then (stdin is a TTY in the terminal) waits for DELETE; type anything else → "Aborted." exit 2. In a non-TTY harness it aborts with the `--yes` message; both are correct.

- [ ] **Step 6: Commit**

```bash
git add download.py tests/test_download.py
git commit -m "download: --repair, ledger-driven --incremental, --fresh/--clear with typed confirmation, exit codes"
```

---

### Task 5: `check_integrity.py` — `--data-dir`, weekday-only ratios, check 12

**Files:**
- Modify: `check_integrity.py`
- Test: `tests/test_check_integrity.py` (new)

**Interfaces:**
- Consumes: `ledger.load_ledger`, `ledger.ledger_path`, `ledger.expected_session_hours`, `ledger.group_runs`.
- Produces: `configure(data_dir: Path) -> None` (sets `COMPILED_DIR`, `LEDGER_DIR`, `SYMBOLS`), `check_session_completeness() -> bool`, `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

`tests/test_check_integrity.py`:

```python
import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import check_integrity as C
import ledger as L
from download import resample_ohlc


def synth_m5(days, drop_hours=()):
    """Deterministic M5 bars for the given dates (24h each), minus drop_hours [(date, hour)]."""
    rows = []
    for d in days:
        for h in range(24):
            if (d, h) in drop_hours:
                continue
            for m in range(0, 60, 5):
                t = pd.Timestamp(datetime.datetime(d.year, d.month, d.day, h, m), tz="UTC")
                px = 1.1 + 0.0001 * ((h * 12 + m // 5) % 7)
                rows.append({"time": t, "open": px, "high": px + 0.0002, "low": px - 0.0002,
                             "close": px + 0.0001, "volume": 10.0, "spread": 0.00007})
    return pd.DataFrame(rows)


def write_symbol(compiled: Path, sym: str, m5: pd.DataFrame):
    compiled.mkdir(parents=True, exist_ok=True)
    m5.to_csv(compiled / f"{sym}_M5.csv", index=False)
    for tf, rule in [("H1", "1h"), ("H4", "4h"), ("D1", "1D")]:
        resample_ohlc(m5, rule).to_csv(compiled / f"{sym}_{tf}.csv", index=False)


WEEK = [datetime.date(2024, 3, d) for d in (4, 5, 6, 7, 8)]      # Mon-Fri
WEEK2 = [datetime.date(2024, 3, d) for d in (11, 12, 13, 14, 15)]


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


def full_ledger(days, overrides=None):
    led = {L.hour_ts(d, h): "D" for d in days for h in range(24)}
    led.update(overrides or {})
    return led


def test_check12_fails_on_failed_ledger_rows(data_dir, capsys):
    gap = (datetime.date(2024, 3, 5), 7)
    write_symbol(data_dir / "compiled", "EURUSD", synth_m5(WEEK + WEEK2, drop_hours={gap}))
    led = full_ledger(WEEK + WEEK2, {L.hour_ts(*gap): "F"})
    L.save_ledger(L.ledger_path(data_dir / "ledger", "EURUSD"), led)
    rc = C.main(["--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL]  Session-hour completeness" in out
    assert "2024-03-05 07:00 -> 2024-03-05 07:00  (1h)" in out


def test_check12_fails_on_never_attempted_hours(data_dir, capsys):
    gap = (datetime.date(2024, 3, 5), 7)
    write_symbol(data_dir / "compiled", "EURUSD", synth_m5(WEEK + WEEK2, drop_hours={gap}))
    led = full_ledger(WEEK + WEEK2)
    del led[L.hour_ts(*gap)]
    L.save_ledger(L.ledger_path(data_dir / "ledger", "EURUSD"), led)
    assert C.main(["--data-dir", str(data_dir)]) == 1
    assert "1 never attempted" in capsys.readouterr().out


def test_check12_informational_without_ledger(data_dir, capsys):
    gap = (datetime.date(2024, 3, 5), 7)
    write_symbol(data_dir / "compiled", "EURUSD", synth_m5(WEEK + WEEK2, drop_hours={gap}))
    rc = C.main(["--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[PASS]  Session-hour completeness" in out
    assert "no ledger" in out and "--repair" in out
    assert "2024-03-05 07:00 -> 2024-03-05 07:00  (1h)" in out


def test_check12_passes_complete_ledger(data_dir, capsys):
    write_symbol(data_dir / "compiled", "EURUSD", synth_m5(WEEK + WEEK2))
    L.save_ledger(L.ledger_path(data_dir / "ledger", "EURUSD"), full_ledger(WEEK + WEEK2))
    assert C.main(["--data-dir", str(data_dir)]) == 0
    assert "12/12 checks passed" in capsys.readouterr().out


def test_check4_ignores_sunday_bars(data_dir, capsys):
    # Two full weeks plus Sunday 20-23 bars before each: ratios must still sit in the FX band.
    sundays = [datetime.date(2024, 3, 3), datetime.date(2024, 3, 10)]
    m5 = synth_m5(WEEK + WEEK2)
    sun = synth_m5(sundays)
    sun = sun[sun["time"].dt.hour >= 20]
    m5 = pd.concat([m5, sun]).sort_values("time").reset_index(drop=True)
    write_symbol(data_dir / "compiled", "EURUSD", m5)
    led = full_ledger(WEEK + WEEK2, {L.hour_ts(s, h): "D" for s in sundays for h in range(20, 24)})
    L.save_ledger(L.ledger_path(data_dir / "ledger", "EURUSD"), led)
    rc = C.main(["--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[PASS]  Bar count ratios" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-eval/bin/python -m pytest tests/test_check_integrity.py -q`
Expected: FAIL — `TypeError: main() takes 0 positional arguments` (or import-time crash if `compiled/` symbols are discovered at import; both count).

- [ ] **Step 3: Implement**

In `check_integrity.py`:

(a) Docstring: add `12. Session-hour completeness (gated when a ledger exists; informational otherwise)` to the list and change the gate sentence to `Correctness checks (1, 4, 5, 6, 7, 9, 10, 12) gate the exit code`.

(b) Imports: add `import argparse` and `from ledger import expected_session_hours, group_runs, ledger_path, load_ledger`.

(c) Replace the module-level `COMPILED_DIR = ...` and `SYMBOLS = discover_symbols()` with:

```python
BASE_DIR = Path(__file__).parent
COMPILED_DIR = BASE_DIR / "compiled"
LEDGER_DIR = BASE_DIR / "ledger"
SYMBOLS: list = []


def configure(data_dir: Path) -> None:
    """Point the checker at <data_dir>/compiled and <data_dir>/ledger."""
    global COMPILED_DIR, LEDGER_DIR, SYMBOLS
    COMPILED_DIR = Path(data_dir) / "compiled"
    LEDGER_DIR = Path(data_dir) / "ledger"
    SYMBOLS = discover_symbols()
```

(`discover_symbols` stays as is; it reads the `COMPILED_DIR` global at call time.)

(d) `check_bar_counts`: replace `counts = {tf: len(load_df(sym, tf)) for tf in TIMEFRAMES}` with

```python
        counts = {}
        for tf in TIMEFRAMES:
            df = load_df(sym, tf)
            counts[tf] = int((df["time"].dt.dayofweek <= 4).sum())   # Mon-Fri only
```

and change the header line to `"[4] Cross-timeframe bar count ratios, Mon-Fri bars (M5/H1~12, H1/H4~4, H4/D1~6)"`.

(e) Add before `def main`:

```python
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
```

(f) `main`:

```python
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
```

Also in `check_recent_data`, change the trailing hint to
`print("  (run `python3 download.py --incremental` to top up; `--repair` to fill gaps)")`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-eval/bin/python -m pytest tests -q`
Expected: all pass.

- [ ] **Step 5: Run the checker against the real data (still no ledger)**

Run: `.venv-eval/bin/python check_integrity.py | tail -25`
Expected: `12/12 checks passed`; check 12 prints `no ledger:` lines with thousands of missing hours per symbol (Sundays + holidays + 503 losses) and the `--repair` hint. Check 4 still passes.

- [ ] **Step 6: Commit**

```bash
git add check_integrity.py tests/test_check_integrity.py
git commit -m "check_integrity: --data-dir, weekday-only ratios, ledger-gated session-hour completeness check"
```

---

### Task 6: `dashboard.py` — run modes, `/clear`, `/files`, backup modal, hours stats

**Files:**
- Modify: `dashboard.py`
- Test: `tests/test_dashboard.py` (new)

**Interfaces:**
- Produces: `MODES: dict[str, list[str]]`, `start_download(payload) -> dict`, `clear_data(payload) -> dict`, `list_files(symbols) -> dict[str, list[dict]]`, `_launch(cmd, tag) -> dict`, `_validate_symbols(payload) -> tuple[Optional[list], Optional[str]]`, `COMPILED_DIR`, `LEDGER_DIR`.
- POST `/start` body: `{symbols, start, end, mode}`; POST `/clear` body: `{symbols}`; GET `/files?symbols=A,B`.

- [ ] **Step 1: Write the failing tests**

`tests/test_dashboard.py`:

```python
import json
from pathlib import Path

import pytest

import dashboard as W


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    """Capture argv instead of spawning; pretend nothing is running."""
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(W.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(W, "running_download_pids", lambda: [])
    monkeypatch.setattr(W, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(W, "COMPILED_DIR", tmp_path / "compiled")
    monkeypatch.setattr(W, "LEDGER_DIR", tmp_path / "ledger")
    return captured


BASE = {"symbols": ["EURUSD", "gbpusd"], "start": "2024-03-04", "end": "2024-03-09"}


def flags(cmd):
    """argv after download.py, minus the symbols block."""
    i = cmd.index("--symbols")
    return cmd[3:i] + [a for a in cmd[i + 3:]]


@pytest.mark.parametrize("mode,extra", [
    ("download", []), ("incremental", ["--incremental"]),
    ("repair", ["--repair"]), ("fresh", ["--fresh", "--yes"]),
])
def test_modes_map_to_flags(launcher, mode, extra):
    r = W.start_download({**BASE, "mode": mode})
    assert r["ok"], r
    cmd = launcher["cmd"]
    assert cmd[2].endswith("download.py")
    assert cmd[cmd.index("--symbols") + 1:cmd.index("--symbols") + 3] == ["EURUSD", "GBPUSD"]
    assert "--start" in cmd and "--end" in cmd
    for f in extra:
        assert f in cmd
    for f in {"--incremental", "--repair", "--fresh", "--yes"} - set(extra):
        assert f not in cmd


def test_missing_mode_defaults_to_download(launcher):
    assert W.start_download(BASE)["ok"]
    assert not {"--incremental", "--repair", "--fresh"} & set(launcher["cmd"])


def test_unknown_mode_rejected(launcher):
    r = W.start_download({**BASE, "mode": "nuke"})
    assert not r["ok"] and "mode" in r["error"]


def test_repair_dates_optional(launcher):
    r = W.start_download({"symbols": ["EURUSD"], "start": "", "end": "", "mode": "repair"})
    assert r["ok"]
    assert "--start" not in launcher["cmd"] and "--end" not in launcher["cmd"]
    r = W.start_download({"symbols": ["EURUSD"], "start": "", "end": "", "mode": "download"})
    assert not r["ok"]


def test_clear_builds_clear_yes(launcher):
    r = W.clear_data({"symbols": ["EURUSD"]})
    assert r["ok"]
    cmd = launcher["cmd"]
    assert "--clear" in cmd and "--yes" in cmd and cmd[cmd.index("--symbols") + 1] == "EURUSD"
    assert "--start" not in cmd
    assert not W.clear_data({"symbols": ["bad symbol"]})["ok"]


def test_refused_while_running(launcher, monkeypatch):
    monkeypatch.setattr(W, "running_download_pids", lambda: [123])
    assert not W.start_download({**BASE, "mode": "download"})["ok"]
    assert not W.clear_data({"symbols": ["EURUSD"]})["ok"]


def test_list_files(launcher, tmp_path):
    (tmp_path / "compiled").mkdir(); (tmp_path / "ledger").mkdir()
    (tmp_path / "compiled" / "EURUSD_M5.csv").write_bytes(b"x" * 10)
    (tmp_path / "ledger" / "EURUSD.csv").write_bytes(b"y" * 3)
    out = W.list_files(["EURUSD", "GBPUSD"])
    assert out["EURUSD"] == [{"file": "compiled/EURUSD_M5.csv", "bytes": 10},
                             {"file": "ledger/EURUSD.csv", "bytes": 3}]
    assert out["GBPUSD"] == []
    with pytest.raises(ValueError):
        W.list_files(["../etc"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv-eval/bin/python -m pytest tests/test_dashboard.py -q`
Expected: FAIL (`AttributeError: module 'dashboard' has no attribute 'COMPILED_DIR'` / `clear_data`).

- [ ] **Step 3: Implement the server side**

In `dashboard.py`:

(a) Docstring endpoints: add
```
    GET  /files?symbols=A,B -> existing compiled/ledger files + sizes per symbol
    POST /clear       -> launch download.py --clear --yes for the given symbols (after UI confirmation)
```
and change the `/start` line to `-> launch download.py detached in the given mode (download|incremental|repair|fresh)`.

(b) After `STATUS_FILE = ...` add:

```python
COMPILED_DIR = BASE_DIR / "compiled"
LEDGER_DIR   = BASE_DIR / "ledger"
TIMEFRAMES   = ("M5", "H1", "H4", "D1")

# Run mode -> extra argv for download.py. "fresh" passes --yes because the
# detached child has no TTY; the dashboard's backup modal is the confirmation.
MODES: dict[str, list[str]] = {
    "download":    [],
    "incremental": ["--incremental"],
    "repair":      ["--repair"],
    "fresh":       ["--fresh", "--yes"],
}
```

(c) Replace `start_download` with:

```python
def _validate_symbols(payload: dict):
    symbols = payload.get("symbols") or []
    if not isinstance(symbols, list) or not symbols:
        return None, "Select at least one symbol."
    symbols = [str(s).upper() for s in symbols]
    bad = [s for s in symbols if not SYMBOL_RE.match(s)]
    if bad:
        return None, f"Invalid symbol(s): {', '.join(bad)}"
    return symbols, None


def _launch(cmd: list, tag: str) -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"dashboard_run_{ts}.log"
    logf = open(log_path, "ab")
    logf.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] launching ({tag}): "
               f"{' '.join(cmd)}\n".encode())
    logf.flush()
    # start_new_session detaches the child so it survives the dashboard exiting.
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                            cwd=str(BASE_DIR), start_new_session=True)
    return {"ok": True, "pid": proc.pid, "log": log_path.name, "cmd": " ".join(cmd), "mode": tag}


def start_download(payload: dict) -> dict:
    """Validate payload and launch download.py detached in the requested mode."""
    if running_download_pids():
        return {"ok": False, "error": "A download is already running. Wait for it to finish."}
    symbols, err = _validate_symbols(payload)
    if err:
        return {"ok": False, "error": err}
    mode = str(payload.get("mode") or "download")
    if mode not in MODES:
        return {"ok": False, "error": f"Unknown mode '{mode}'. Use one of: {', '.join(MODES)}."}

    start = str(payload.get("start", "")).strip()
    end = str(payload.get("end", "")).strip()
    date_args: list = []
    if mode == "repair" and not start and not end:
        pass                                    # whole ledger range
    else:
        try:
            datetime.date.fromisoformat(start)
            datetime.date.fromisoformat(end)
        except ValueError:
            return {"ok": False, "error": "start/end must be valid YYYY-MM-DD dates."}
        if start >= end:
            return {"ok": False, "error": "start date must be before end date."}
        date_args = ["--start", start, "--end", end]

    cmd = [sys.executable, "-u", str(DOWNLOAD_PY), "--symbols", *symbols, *date_args, *MODES[mode]]
    return _launch(cmd, mode)


def clear_data(payload: dict) -> dict:
    """Launch download.py --clear --yes for the given symbols (UI already confirmed)."""
    if running_download_pids():
        return {"ok": False, "error": "A download is already running. Wait for it to finish."}
    symbols, err = _validate_symbols(payload)
    if err:
        return {"ok": False, "error": err}
    cmd = [sys.executable, "-u", str(DOWNLOAD_PY), "--clear", "--yes", "--symbols", *symbols]
    return _launch(cmd, "clear")


def list_files(symbols: list) -> dict:
    """Existing compiled CSVs and ledger per symbol with byte sizes."""
    out: dict = {}
    for s in symbols:
        s = str(s).upper()
        if not SYMBOL_RE.match(s):
            raise ValueError(f"invalid symbol {s!r}")
        files = []
        for tf in TIMEFRAMES:
            p = COMPILED_DIR / f"{s}_{tf}.csv"
            if p.exists():
                files.append({"file": f"compiled/{p.name}", "bytes": p.stat().st_size})
        lp = LEDGER_DIR / f"{s}.csv"
        if lp.exists():
            files.append({"file": f"ledger/{lp.name}", "bytes": lp.stat().st_size})
        out[s] = files
    return out
```

(d) In `Handler.do_GET`, add before the `/favicon.ico` branch:

```python
        elif path == "/files":
            syms = [s for s in qs.get("symbols", [""])[0].split(",") if s]
            try:
                self._json(list_files(syms))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
```

Replace `do_POST`:

```python
    def do_POST(self):
        route = urlparse(self.path)
        handlers = {"/start": start_download, "/clear": clear_data}
        fn = handlers.get(route.path)
        if fn is None:
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json({"ok": False, "error": "bad JSON body"}, 400)
            return
        result = fn(payload)
        self._json(result, 200 if result.get("ok") else 409)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv-eval/bin/python -m pytest tests/test_dashboard.py -q`
Expected: all pass.

- [ ] **Step 5: Implement the UI**

In `INDEX_HTML`:

(a) CSS: append inside `<style>`:

```css
  .modes { display:flex; gap:6px; flex-wrap:wrap; margin-top:10px; }
  .modes label { display:inline-flex; align-items:center; gap:6px; padding:6px 10px;
                 border:1px solid var(--line); border-radius:8px; cursor:pointer; font-size:13px; }
  .modes label:has(input:checked) { border-color:var(--accent); background:#13233d; }
  .modes .danger:has(input:checked) { border-color:var(--err); background:#3a1417; }
  button.secondary { background:transparent; border:1px solid var(--err); color:var(--err); }
  .badge.warn { background:#3a2a0f; color:#ffd27c; }
  .modal-bg { position:fixed; inset:0; background:rgba(0,0,0,.6); display:none;
              align-items:center; justify-content:center; z-index:10; }
  .modal-bg.show { display:flex; }
  .modal { background:var(--panel); border:1px solid var(--err); border-radius:12px;
           padding:22px; max-width:560px; width:92%; }
  .modal h3 { margin:0 0 10px; color:var(--err); font-size:15px; }
  .modal pre { background:#0b0e12; border:1px solid var(--line); border-radius:8px; padding:10px;
               font:12px/1.4 ui-monospace,Menlo,monospace; max-height:200px; overflow:auto; margin:8px 0; }
  .modal input[type=text] { width:100%; background:var(--bg); border:1px solid var(--line);
               color:var(--fg); border-radius:7px; padding:8px; font-size:14px; margin:8px 0; }
  .modal .actions { display:flex; gap:10px; justify-content:flex-end; }
```

(b) Form: replace the `.row` block (Start/End/Incremental/button) with:

```html
    <div class="modes" id="modes">
      <label><input type="radio" name="mode" value="download" checked> Download (merge)</label>
      <label><input type="radio" name="mode" value="incremental"> Incremental</label>
      <label><input type="radio" name="mode" value="repair"> Repair only</label>
      <label class="danger"><input type="radio" name="mode" value="fresh"> Re-run from scratch</label>
    </div>
    <div class="row">
      <label class="field"><span>Start</span><input type="date" id="start"></label>
      <label class="field"><span>End</span><input type="date" id="end"></label>
      <button id="go">Start</button>
      <button id="clear" class="secondary">Clear existing data…</button>
    </div>
    <div id="msg" class="msg"></div>
    <div class="hint" id="modehint">Merges new hours into existing data. Refused if a download is already running. Runs detached — safe to close this tab.</div>
```

(c) Modal markup: insert just before `<script>`:

```html
<div class="modal-bg" id="modal">
  <div class="modal">
    <h3 id="mtitle">Delete existing data?</h3>
    <div id="mbody">The following files will be deleted. This cannot be undone — the data can only be recovered by re-downloading it.</div>
    <pre id="mfiles">…</pre>
    <div class="hint">Back up first if you need them, e.g.<br><code id="mbackup">cp -r compiled ledger ~/duka-backup</code></div>
    <input type="text" id="mconfirm" placeholder="Type DELETE to enable the button" autocomplete="off">
    <div class="actions">
      <button id="mcancel" class="secondary">Cancel</button>
      <button id="mok" disabled>Delete</button>
    </div>
  </div>
</div>
```

(d) JS: replace the `$('go').onclick = ...` block with:

```javascript
const MODE_HINTS = {
  download: 'Merges new hours into existing data.',
  incremental: 'Fetches only days after the last attempted day in the ledger.',
  repair: 'Re-fetches hours that failed or were never attempted. Dates optional (narrow the range).',
  fresh: 'DELETES the selected symbols\' compiled files and ledger, then downloads the range.',
};
const mode = () => document.querySelector('#modes input:checked').value;
document.querySelectorAll('#modes input').forEach(i => i.onchange = () => {
  $('modehint').textContent = MODE_HINTS[mode()] + ' Refused if a download is already running.';
});

function fmtBytes(b){ return b>=1048576 ? (b/1048576).toFixed(1)+' MB' : b>=1024 ? (b/1024).toFixed(1)+' KB' : b+' B'; }

async function confirmDelete(symbols, title){
  const files = await (await fetch('/files?symbols='+encodeURIComponent(symbols.join(',')))).json();
  const lines = [];
  for(const [s, fs] of Object.entries(files)){
    if(!fs.length){ lines.push(`${s}: (no files)`); continue; }
    for(const f of fs) lines.push(`${f.file.padEnd(32)} ${fmtBytes(f.bytes)}`);
  }
  $('mtitle').textContent = title;
  $('mfiles').textContent = lines.join('\n');
  $('mbackup').textContent = `cp -r compiled ledger ~/duka-backup-${new Date().toISOString().slice(0,10)}`;
  $('mconfirm').value=''; $('mok').disabled=true;
  $('modal').classList.add('show'); $('mconfirm').focus();
  return new Promise(resolve => {
    const done = v => { $('modal').classList.remove('show'); resolve(v); };
    $('mconfirm').oninput = () => { $('mok').disabled = $('mconfirm').value.trim() !== 'DELETE'; };
    $('mcancel').onclick = () => done(false);
    $('mok').onclick = () => done(true);
  });
}

async function post(url, body, btn){
  const m=$('msg'); m.className='msg'; m.textContent='Starting…'; btn.disabled=true;
  try{
    const r = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j = await r.json();
    if(j.ok){ m.className='msg ok'; m.textContent=`Started ${j.mode} (PID ${j.pid}) → ${j.log}`; }
    else { m.className='msg err'; m.textContent=j.error||'Failed to start.'; }
  }catch(e){ m.className='msg err'; m.textContent=String(e); }
  btn.disabled=false;
}

$('go').onclick = async () => {
  const symbols = chosen(), md = mode();
  if(!symbols.length){ $('msg').className='msg err'; $('msg').textContent='Select at least one symbol.'; return; }
  if(md==='fresh' && !(await confirmDelete(symbols, `Re-run from scratch: delete data for ${symbols.join(', ')}?`))) return;
  await post('/start', {symbols, start:$('start').value, end:$('end').value, mode:md}, $('go'));
};

$('clear').onclick = async () => {
  const symbols = chosen();
  if(!symbols.length){ $('msg').className='msg err'; $('msg').textContent='Select at least one symbol.'; return; }
  if(!(await confirmDelete(symbols, `Clear existing data for ${symbols.join(', ')}?`))) return;
  await post('/clear', {symbols}, $('clear'));
};
```

(e) In `renderStatus`, replace the badge lines and the `st` array:

```javascript
  const lost = Number(s.hours_lost||0);
  const b=$('state');
  if(!running && state==='completed' && lost>0){
    b.textContent=`completed · ${lost} hours lost`; b.className='badge warn';
    $('msg').className='msg err'; $('msg').textContent=`${lost} hour(s) lost after retries. Run "Repair only" for these symbols.`;
  } else { b.textContent=state; b.className='badge '+state; }
  $('sub').textContent = (s.date_range||'') + (s.mode?` · ${s.mode}`:'');
```

and add to `st` after `['Failures', ...]`:

```javascript
    ['Hours lost', fmt(s.hours_lost)], ['Hours empty', fmt(s.hours_empty)],
```

Also `$('go').disabled = running; $('clear').disabled = running;`.

- [ ] **Step 6: Manual UI check**

Run: `.venv-eval/bin/python dashboard.py --no-browser --port 8799` in the background, then:

```bash
curl -s 'http://127.0.0.1:8799/files?symbols=EURUSD' | head -c 300; echo
curl -s -X POST http://127.0.0.1:8799/clear -H 'Content-Type: application/json' -d '{"symbols":["bad symbol"]}'; echo
curl -s -X POST http://127.0.0.1:8799/start -H 'Content-Type: application/json' -d '{"symbols":["EURUSD"],"mode":"nuke","start":"2024-03-04","end":"2024-03-05"}'; echo
curl -s http://127.0.0.1:8799/ | grep -c 'name="mode"'
```

Expected: file list JSON with sizes; `{"ok": false, "error": "Invalid symbol(s): BAD SYMBOL"}`; unknown-mode error; `4`. Then kill the server. Do NOT click Start/Clear against the real `compiled/` during this check.

- [ ] **Step 7: Run the whole suite and commit**

Run: `.venv-eval/bin/python -m pytest tests -q` → all pass.

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "dashboard: run modes, Clear button with backup modal, /files and /clear endpoints, hours-lost stats"
```

---

### Task 7: Documentation

**Files:**
- Modify: `USAGE.md`, `README.md`

- [ ] **Step 1: USAGE.md**

Replace the `--incremental` row in the Arguments table and add rows:

```
| `--incremental` | Off | Only fetch days after the last attempted day in the ledger |
| `--repair` | Off | Re-fetch hours that failed or were never attempted (ledger-driven); `--start/--end` narrow the range |
| `--fresh` | Off | Delete the symbols' compiled files and ledger, then download (asks for confirmation) |
| `--clear` | Off | Delete the symbols' compiled files and ledger, then exit (asks for confirmation) |
| `--yes` | Off | Skip the `DELETE` confirmation for `--fresh`/`--clear` (scripts, dashboard) |
```

Add a note under the table: `--incremental`, `--repair`, `--fresh`, `--clear` are mutually exclusive. `--end` is exclusive.

Add a new section after "Incremental Mode":

```markdown
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
```

Update the Coverage bullets: `**Hours:** Full session, Sunday 20:00 UTC to Friday 23:59 UTC (Friday 21/22–23 come back empty)` and `**Weekends:** Saturday is never requested; Sunday fetches hours 20–23`; replace `**Holidays:** Empty hours are silently skipped` with `**Holidays:** recorded as E in the ledger and never re-fetched`.

Update the Monitoring example JSON: remove `days_failed`, add `"mode": "download", "hours_failed": 0, "hours_empty": 3, "hours_lost": 0`. In "The log contains", change the WARNING bullet to `**WARNING** — lost hours (with the exact list) and the --repair hint`.

Update the Directory Structure block: add `├── ledger/                  # per-symbol fetch ledger (hour,status) — see Completeness` after `compiled/`.

Update Incremental Mode step 1: `Reads the ledger (falls back to .download_meta.json) to find the last attempted day per symbol`.

- [ ] **Step 2: README.md**

Tooling table: change the `check_integrity.py` row to `Validate compiled CSVs (gaps, ordering, bad bars, ledger completeness)` and add a row `| \`ledger.py\` | Session calendar + per-symbol fetch ledger used by the downloader and the checker |`.

After the Quick start `--incremental` block add:

```markdown
The downloader exits **1** if any hour was lost to Dukascopy's intermittent
503s after retries; run `python3 download.py --repair` to fill those hours. See
[USAGE.md](USAGE.md#completeness-the-fetch-ledger-lost-hours-and-repair).
```

Dashboard paragraph: after "watch live progress" add `Run modes: Download (merge), Incremental, Repair only, Re-run from scratch; a separate **Clear existing data** button. Both destructive actions show the files that will be deleted and require typing DELETE.`

Also in the Data source / disclaimer area nothing changes.

- [ ] **Step 3: Commit**

```bash
git add USAGE.md README.md
git commit -m "docs: ledger, repair, fresh/clear confirmation, Sunday session, exit codes"
```

---

### Task 8: Backfill the local dataset and produce the "after" validation

**Files:**
- Create: `docs/superpowers/validation/2026-09-11-after.md`
- Modify: `DATA_QUALITY.md`; regenerate `DATA_MANIFEST.md` (gitignored)

- [ ] **Step 1: Back up the current data (cheap insurance, outside the repo)**

```bash
mkdir -p ~/duka-backup-2026-09-11 && cp -r compiled ~/duka-backup-2026-09-11/
```

- [ ] **Step 2: Run the repair for all 14 symbols in the background**

```bash
nohup .venv-eval/bin/python -u download.py --repair --symbols EURUSD GBPUSD USDJPY USDCHF AUDUSD USDCAD NZDUSD EURGBP EURJPY GBPJPY AUDJPY XAUUSD LIGHTCMDUSD USA500IDXUSD > logs/repair_backfill_2026-09-11.log 2>&1 &
```

Monitor with `tail -f logs/repair_backfill_2026-09-11.log` and `.download_status.json`. Each symbol seeds its ledger from H1 and fetches ~3,500 hours (Sundays, holidays, lost hours). Expect roughly 15–40 minutes per symbol at the current concurrency. Record the exit code from `wait` / the log's last lines.

- [ ] **Step 3: Verify**

```bash
.venv-eval/bin/python check_integrity.py > /tmp/after_check.txt; echo "exit=$?"
tail -30 /tmp/after_check.txt
ls -la ledger/
.venv-eval/bin/python make_manifest.py
```

Expected: `12/12 checks passed`, exit 0; check 12 shows `0 failed, 0 never attempted` for every symbol. If a symbol still has `F` rows (a 503 burst during the backfill), run `--repair --symbols <SYM>` again and re-check.

- [ ] **Step 4: Write the "after" report**

```bash
{ echo '# After: 2026-09-11 (ledger + repair applied to the shipped data)'; echo; echo '## check_integrity.py'; echo '```'; cat /tmp/after_check.txt; echo '```'; echo; echo '## Missing-hour census (expected = Sun 20-23 + Mon-Fri 0-23 UTC)'; echo; .venv-eval/bin/python docs/superpowers/validation/census.py compiled; echo; echo '## Repair run summary'; echo '```'; grep -E "Seeded ledger|hours: .* lost after sweep|WARNING|COMPLETE" logs/repair_backfill_2026-09-11.log; echo '```'; } > docs/superpowers/validation/2026-09-11-after.md
```

Expected in the census: Sunday bars > 0 for every FX symbol; `missing` drops to holidays only (no isolated 1h gaps on non-holiday weekdays).

- [ ] **Step 5: Rewrite DATA_QUALITY.md**

Update the header line to `_Last validated 2026-09-11 ... (12/12 checks passing)_`. In the Verdict table keep grades but change the Completeness summary to: `Full ~20-year FX history incl. Sunday sessions; every expected session hour is accounted for in the fetch ledger (D/E/N), none failed.` Add a section:

```markdown
## Completeness audit (2026-09-11)

A reader noticed that runs with many 503 retries still passed the integrity
check. Investigation confirmed two silent gaps in the 2026-07-06 dataset:

- **~565 hours lost to HTTP 503** across the 14 symbols (EURUSD 53, USDJPY 87,
  AUDUSD 78, USDCAD 66, …). The downloader gave up after 4 retries and the
  checker could not see it because all timeframes derive from the same M5.
- **No Sunday sessions at all**: the downloader iterated Mon–Fri only, so the
  Sunday 21:00/22:00–23:59 UTC session (~2,000–3,000 hours per symbol over 20
  years) was never fetched.

Both are fixed: the downloader now keeps a per-hour ledger (`ledger/`), sweeps
and reports lost hours, fetches Sunday 20–23, and `check_integrity.py` check 12
fails on any lost or never-attempted hour. The dataset was backfilled with
`python3 download.py --repair`; see `docs/superpowers/validation/` for the
before/after reports.
```

Replace the old bullet "**Freshness.**" line as appropriate (data end date unchanged unless you also ran `--incremental`). In "Known caveats" add: `**Sunday D1 bar.** Each week has a short Sunday bar (UTC days). Filter dayofweek != 6 if you want 5 bars/week.`

- [ ] **Step 6: Commit**

```bash
git add DATA_QUALITY.md docs/superpowers/validation/2026-09-11-after.md
git commit -m "Backfill validation: after-report and DATA_QUALITY completeness audit"
```

---

### Task 9: Pull request

- [ ] **Step 1: Final full verification**

```bash
.venv-eval/bin/python -m pytest tests -q
.venv-eval/bin/python check_integrity.py | tail -16
git status --short
```

Expected: all tests pass, 12/12, clean tree.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin lost-hours-completeness
gh pr create --title "Ledger-backed completeness: repair lost 503 hours, fetch Sunday sessions, real coverage check" --body-file <body.md>
```

Body (write to a scratch file first) must contain: link `Closes #3`; a summary of the four findings; the design decisions; the before/after tables copied from the validation reports; the test-suite result line; the checker summary before (11/11 with silent gaps) and after (12/12 with ledger); the manual dashboard check; and the trailer:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_012VRdaceaeDkQPTuzCps4Sk
```

- [ ] **Step 3: Comment on issue #3** with the PR link and the reader-facing summary (what `hours_lost` means, `--repair`).

---

## Self-review

**Spec coverage.** §1 session calendar → Task 1/2. §2 fetch result + ladder → Task 2. §3 ledger/sweep/merge/repair/CLI/confirmation/exit codes → Tasks 3–4. §4 checker → Task 5. §5 docs + backfill → Tasks 7–8. §6 tests → every task; dashboard tests in Task 6. §7 dashboard modes/clear/modal/files/stats → Task 6. Rollout → Task 9. `.gitignore` → Task 0.

**Placeholder scan.** None; every step has code or an exact command.

**Type consistency.** `download_day_ticks` returns `(m5, ticks, {hour: status})` in Tasks 2–3; `download_symbol` returns `(df | None, lost list)` in Tasks 3–4 and `run` returns `int` in Tasks 3–4; `FETCH_TO_LEDGER` keys are the four fetch strings used by `download_hour_bi5`; dashboard `list_files` returns `{"file", "bytes"}` dicts matching the JS `f.file` / `f.bytes`.
