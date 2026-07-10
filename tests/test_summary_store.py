"""Tests for the per-session summary sidecar store."""

from __future__ import annotations

import json
from pathlib import Path

from ephor.summary_store import SummaryStore


def test_get_returns_none_for_unknown_session(tmp_path: Path) -> None:
    store = SummaryStore(tmp_path)
    assert store.get("never-existed") is None
    assert not store.has("never-existed")


def test_set_then_get_round_trips(tmp_path: Path) -> None:
    store = SummaryStore(tmp_path)
    store.set("sid-1", "Refactoring auth middleware")
    assert store.has("sid-1")
    assert store.get("sid-1") == "Refactoring auth middleware"


def test_set_with_empty_summary_is_noop(tmp_path: Path) -> None:
    store = SummaryStore(tmp_path)
    store.set("sid-1", "")
    assert not store.has("sid-1")
    assert not (tmp_path / "sid-1.json").exists()


def test_set_creates_directory_lazily(tmp_path: Path) -> None:
    """We never call mkdir until first write — TUI startup shouldn't require
    write access just to read a non-existent summary."""
    target = tmp_path / "fresh" / "summaries"
    store = SummaryStore(target)
    assert not target.exists()
    assert store.get("sid") is None  # read-only path doesn't create
    assert not target.exists()
    store.set("sid", "first summary")
    assert target.is_dir()


def test_set_atomic_no_partial_files(tmp_path: Path) -> None:
    store = SummaryStore(tmp_path)
    store.set("sid-1", "first")
    store.set("sid-1", "second")
    # Only the final file should exist; tmp files cleaned up.
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["sid-1.json"]
    assert store.get("sid-1") == "second"


def test_delete_removes_file(tmp_path: Path) -> None:
    store = SummaryStore(tmp_path)
    store.set("sid-1", "x")
    store.delete("sid-1")
    assert not store.has("sid-1")


def test_delete_missing_is_silent(tmp_path: Path) -> None:
    store = SummaryStore(tmp_path)
    store.delete("never-existed")  # must not raise


def test_unsafe_session_id_is_sanitized(tmp_path: Path) -> None:
    """Defense-in-depth: even if an unsafe sid leaks in, it can't escape the dir."""
    store = SummaryStore(tmp_path)
    store.set("../escape", "nope")
    # Filename should NOT contain '..' or '/'. It got reduced to 'escape.json'.
    files = list(tmp_path.iterdir())
    assert all("/" not in p.name and ".." not in p.name for p in files)


def test_payload_includes_generated_at(tmp_path: Path) -> None:
    store = SummaryStore(tmp_path)
    store.set("sid-1", "hello")
    payload = json.loads((tmp_path / "sid-1.json").read_text())
    assert payload["summary"] == "hello"
    assert "generated_at" in payload
