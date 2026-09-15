"""Tests for btctrader.common.jsonl."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from btctrader.common.jsonl import append_jsonl, atomic_write_json, read_json, read_jsonl_tail


def test_append_and_tail(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "events.jsonl"
    for i in range(5):
        append_jsonl(p, {"i": i, "text": "über €"})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert "über €" in lines[0]  # ensure_ascii=False
    assert read_jsonl_tail(p, 2) == [{"i": 3, "text": "über €"}, {"i": 4, "text": "über €"}]
    assert read_jsonl_tail(p, 50) == [{"i": i, "text": "über €"} for i in range(5)]
    assert read_jsonl_tail(p, 0) == []


def test_tail_tolerates_truncated_last_line(tmp_path: Path) -> None:
    p = tmp_path / "d.jsonl"
    append_jsonl(p, {"a": 1})
    append_jsonl(p, {"a": 2})
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"a": 3, "partial": "tru')  # crash mid-write, no newline
    assert read_jsonl_tail(p, 10) == [{"a": 1}, {"a": 2}]
    # subsequent append after truncated line: the broken line is skipped, the new one read
    append_jsonl(p, {"a": 4})
    tail = read_jsonl_tail(p, 10)
    assert tail[-1] == {"a": 4}
    assert {"a": 2} in tail


def test_tail_skips_blank_and_non_object_lines(tmp_path: Path) -> None:
    p = tmp_path / "x.jsonl"
    p.write_text('\n{"ok": 1}\n[1,2]\n\n"str"\n{"ok": 2}\n', encoding="utf-8")
    assert read_jsonl_tail(p) == [{"ok": 1}, {"ok": 2}]


def test_tail_missing_file(tmp_path: Path) -> None:
    assert read_jsonl_tail(tmp_path / "missing.jsonl") == []


def test_atomic_write_replaces_file_and_leaves_no_tmp(tmp_path: Path) -> None:
    p = tmp_path / "adv" / "decision.json"
    atomic_write_json(p, {"v": 1, "name": "ä"})
    assert read_json(p) == {"v": 1, "name": "ä"}
    atomic_write_json(p, {"v": 2})
    assert read_json(p) == {"v": 2}
    assert json.loads(p.read_text(encoding="utf-8")) == {"v": 2}
    assert [f.name for f in p.parent.iterdir()] == ["decision.json"]


def test_atomic_write_failure_keeps_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import btctrader.common.jsonl as mod

    p = tmp_path / "decision.json"
    atomic_write_json(p, {"v": 1})

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(p, {"v": 2})
    assert read_json(p) == {"v": 1}  # old content untouched
    assert [f.name for f in tmp_path.iterdir()] == ["decision.json"]  # temp file cleaned up


def test_read_json_invalid_or_missing(tmp_path: Path) -> None:
    assert read_json(tmp_path / "nope.json") is None
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert read_json(p) is None
    p.write_text("[1, 2]", encoding="utf-8")
    assert read_json(p) is None
