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
    """Weekdays D for all 24h; any Sunday inside the range gets its 20-23 session as E."""
    led = {L.hour_ts(d, h): "D" for d in days for h in range(24)}
    d, hi = min(days), max(days)
    while d <= hi:
        if d.weekday() == 6:
            for h in range(20, 24):
                led[L.hour_ts(d, h)] = "E"
        d += datetime.timedelta(days=1)
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
