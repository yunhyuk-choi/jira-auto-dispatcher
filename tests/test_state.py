"""state 영속 계층 단위테스트(원자적 쓰기·roundtrip)."""

from __future__ import annotations

import os

from app import state


def test_atomic_roundtrip_and_utf8(isolated_state):
    d = {"한글": "값", "list": [1, 2, 3]}
    path = os.path.join(state.get_state_dir(), "x.json")
    state.atomic_write_json(path, d)
    assert state.load_json(path) == d
    # UTF-8(BOM 없음) 확인
    raw = open(path, "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert "한글".encode("utf-8") in raw
    # 임시파일이 남지 않음
    assert not any(f.endswith(".tmp") for f in os.listdir(state.get_state_dir()))


def test_missing_returns_default(isolated_state):
    assert state.load_json(os.path.join(state.get_state_dir(), "none.json"), []) == []


def test_corrupt_returns_default(isolated_state):
    p = os.path.join(state.get_state_dir(), "bad.json")
    open(p, "w", encoding="utf-8").write("{ not json")
    assert state.load_json(p, {"fallback": True}) == {"fallback": True}


def test_targeted_helpers(isolated_state):
    state.save_jobs([{"ticket": "PROJ-1"}])
    state.save_dedup(["PROJ-1"])
    state.save_watermark("2026-08-10T00:00:00")
    state.save_registry({"users": [{"username": "u"}]})
    assert state.load_jobs() == [{"ticket": "PROJ-1"}]
    assert state.load_dedup() == ["PROJ-1"]
    assert state.load_watermark() == "2026-08-10T00:00:00"
    assert state.load_registry() == {"users": [{"username": "u"}]}
