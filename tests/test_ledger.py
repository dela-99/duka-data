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
