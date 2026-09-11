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
