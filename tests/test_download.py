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
    D.download_symbol("EURUSD", WEEK_START, datetime.date(2024, 3, 12), incremental=True)
    assert feed.calls, "incremental run should fetch the new days"
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
    assert "EURUSD_M5.csv" in out and ("MB" in out or "KB" in out)
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
