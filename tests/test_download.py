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
