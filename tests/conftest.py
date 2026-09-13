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
