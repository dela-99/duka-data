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
