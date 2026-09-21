"""Tests for user-declared ticket pins."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ephor import ticket_pins


def test_get_is_none_before_anything_is_pinned() -> None:
    assert ticket_pins.get("sid-1") is None
    assert ticket_pins.get(None) is None
    assert ticket_pins.get("") is None


def test_pin_then_get_round_trips() -> None:
    ticket_pins.pin("sid-1", "DR-8222")
    assert ticket_pins.get("sid-1") == "DR-8222"


def test_pin_survives_a_fresh_read_of_the_file() -> None:
    """Pins outlive the process — that is the point of writing them down."""
    ticket_pins.pin("sid-1", "DR-1")
    ticket_pins.pin("sid-2", "DR-2")
    assert json.loads(ticket_pins.pins_path().read_text()) == {
        "sid-1": "DR-1",
        "sid-2": "DR-2",
    }


def test_repinning_replaces_the_previous_answer() -> None:
    ticket_pins.pin("sid-1", "DR-1")
    ticket_pins.pin("sid-1", "DR-2")
    assert ticket_pins.get("sid-1") == "DR-2"


def test_unpin_removes_only_that_session() -> None:
    ticket_pins.pin("sid-1", "DR-1")
    ticket_pins.pin("sid-2", "DR-2")
    ticket_pins.unpin("sid-1")
    assert ticket_pins.get("sid-1") is None
    assert ticket_pins.get("sid-2") == "DR-2"


def test_unpin_is_a_no_op_for_an_unknown_session() -> None:
    ticket_pins.unpin("never-pinned")
    assert ticket_pins.load() == {}


def test_empty_values_are_ignored() -> None:
    ticket_pins.pin("sid-1", "")
    ticket_pins.pin("", "DR-1")
    assert ticket_pins.load() == {}


def test_a_corrupt_file_reads_back_as_empty(tmp_path: Path) -> None:
    """A broken pin file must not take the dashboard down with it."""
    path = ticket_pins.pins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert ticket_pins.load() == {}
    # And it is repaired by the next write rather than staying broken.
    ticket_pins.pin("sid-1", "DR-1")
    assert ticket_pins.get("sid-1") == "DR-1"


def test_non_string_values_are_dropped() -> None:
    path = ticket_pins.pins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sid-1": 42, "sid-2": "DR-2"}))
    assert ticket_pins.load() == {"sid-2": "DR-2"}


def test_a_json_array_reads_back_as_empty() -> None:
    path = ticket_pins.pins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]")
    assert ticket_pins.load() == {}


def test_saving_never_raises_when_the_directory_is_unwritable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(ticket_pins.tempfile, "mkstemp", boom)
    ticket_pins.pin("sid-1", "DR-1")  # must not raise
    assert ticket_pins.get("sid-1") is None
