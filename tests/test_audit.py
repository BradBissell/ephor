"""Session auditing: pricing, transcript parsing, outcome classification, store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ephor import audit, eventlog, work_items


def _write_transcript(root: Path, session_id: str, lines: list[dict]) -> Path:
    folder = root / "-home-brad-projects-ephor"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _turn(
    *,
    at: str,
    model: str = "claude-opus-5",
    inp: int = 0,
    out: int = 0,
    cache_write: int = 0,
    cache_read: int = 0,
    sidechain: bool = False,
    tools: list[str] | None = None,
    stop: str = "end_turn",
) -> dict:
    content: list[dict] = [{"type": "text", "text": "hi"}]
    for name in tools or []:
        content.append({"type": "tool_use", "name": name, "input": {}})
    return {
        "type": "assistant",
        "timestamp": at,
        "isSidechain": sidechain,
        "stopReason": stop,
        "cwd": "/home/brad/projects/ephor",
        "gitBranch": "DR-1-x",
        "effort": "high",
        "message": {
            "model": model,
            "content": content,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
            },
        },
    }


@pytest.fixture(autouse=True)
def _transcripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "transcripts"
    root.mkdir()
    monkeypatch.setenv(audit.ENV_TRANSCRIPT_ROOT, str(root))
    return root


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


def test_price_lookup_exact_and_by_prefix() -> None:
    """A dated snapshot of a known model prices like the model it snapshots."""
    assert audit.price_for("claude-opus-5") == audit.PRICES["claude-opus-5"]
    assert audit.price_for("claude-opus-5-20260401") == audit.PRICES["claude-opus-5"]


def test_unknown_model_is_unpriced_rather_than_guessed() -> None:
    """A silently-assumed price is worse than a visible gap."""
    assert audit.price_for("some-other-llm") is None
    assert audit.price_for("") is None


def test_cache_rates_derive_from_input_unless_overridden() -> None:
    opus = audit.PRICES["claude-opus-5"]
    assert opus.cache_read == pytest.approx(0.5)  # 5.0 * 0.1
    assert opus.cache_write == pytest.approx(6.25)  # 5.0 * 1.25
    # Fable prices reads explicitly; it must not be derived from the input rate.
    assert audit.PRICES["claude-fable-5-1"].cache_read == pytest.approx(0.25)


# --------------------------------------------------------------------------
# Transcript parsing
# --------------------------------------------------------------------------


def test_cost_is_computed_per_token_class(_transcripts: Path) -> None:
    """Cache reads are 0.1x and writes 1.25x — not counted at the input rate."""
    _write_transcript(
        _transcripts,
        "sess1",
        [_turn(at="2026-09-21T10:00:00Z", inp=1_000_000, out=1_000_000)],
    )
    stats = audit.analyze_transcript(audit.find_transcript("sess1"))
    # 1M input @ $5 + 1M output @ $25
    assert stats.equiv_cost_usd == pytest.approx(30.0)

    _write_transcript(
        _transcripts,
        "sess2",
        [_turn(at="2026-09-21T10:00:00Z", cache_read=1_000_000, cache_write=1_000_000)],
    )
    stats2 = audit.analyze_transcript(audit.find_transcript("sess2"))
    # 1M cache-read @ $0.50 + 1M cache-write @ $6.25
    assert stats2.equiv_cost_usd == pytest.approx(6.75)


def test_unpriced_model_counts_tokens_but_not_dollars(_transcripts: Path) -> None:
    _write_transcript(
        _transcripts,
        "sess",
        [_turn(at="2026-09-21T10:00:00Z", model="mystery-model", inp=500, out=500)],
    )
    stats = audit.analyze_transcript(audit.find_transcript("sess"))
    assert stats.input_tokens == 500
    assert stats.output_tokens == 500
    assert stats.equiv_cost_usd == 0.0
    assert stats.unpriced_models == ("mystery-model",)


def test_active_time_excludes_long_gaps(_transcripts: Path) -> None:
    """A 22-hour session is not 22 hours of work — it includes you asleep."""
    _write_transcript(
        _transcripts,
        "sess",
        [
            _turn(at="2026-09-21T10:00:00Z"),
            _turn(at="2026-09-21T10:01:00Z"),  # +60s, counted
            _turn(at="2026-09-21T18:00:00Z"),  # +8h overnight gap, not counted
            _turn(at="2026-09-21T18:02:00Z"),  # +120s, counted
        ],
    )
    stats = audit.analyze_transcript(audit.find_transcript("sess"))
    assert stats.active_sec == pytest.approx(180.0)
    assert stats.wall_clock_sec == pytest.approx(8 * 3600 + 120)


def test_sidechain_turns_are_counted_separately(_transcripts: Path) -> None:
    """Subagent work is inline in the parent transcript; attribute it as such."""
    _write_transcript(
        _transcripts,
        "sess",
        [
            _turn(at="2026-09-21T10:00:00Z"),
            _turn(at="2026-09-21T10:00:30Z", sidechain=True),
        ],
    )
    stats = audit.analyze_transcript(audit.find_transcript("sess"))
    assert stats.turns == 2
    assert stats.sidechain_turns == 1


def test_tools_and_truncation_are_recorded(_transcripts: Path) -> None:
    _write_transcript(
        _transcripts,
        "sess",
        [
            _turn(at="2026-09-21T10:00:00Z", tools=["Bash", "Bash", "Edit"]),
            _turn(at="2026-09-21T10:00:30Z", stop="max_tokens"),
        ],
    )
    stats = audit.analyze_transcript(audit.find_transcript("sess"))
    assert stats.tool_counts == {"Bash": 2, "Edit": 1}
    assert stats.truncated_turns == 1


def test_cache_hit_ratio_reports_share_of_read_context(_transcripts: Path) -> None:
    _write_transcript(
        _transcripts,
        "sess",
        [_turn(at="2026-09-21T10:00:00Z", inp=100, cache_read=900)],
    )
    stats = audit.analyze_transcript(audit.find_transcript("sess"))
    assert stats.cache_hit_ratio == pytest.approx(0.9)


def test_malformed_lines_are_skipped_not_fatal(_transcripts: Path) -> None:
    """Transcripts are appended to by another process; a torn last line is normal."""
    folder = _transcripts / "-proj"
    folder.mkdir()
    path = folder / "sess.jsonl"
    path.write_text(
        json.dumps(_turn(at="2026-09-21T10:00:00Z", out=10)) + "\n"
        "{not json at all\n"
        "\n"
        '{"type":"assistant"}\n'  # no message
         + json.dumps(_turn(at="2026-09-21T10:00:10Z", out=5))[:40]  # torn
    )
    stats = audit.analyze_transcript(path)
    assert stats.output_tokens == 10
    # Only the first line is a countable turn: the torn one never parsed, and
    # an assistant entry carrying no `message` has no usage to attribute.
    assert stats.turns == 1


def test_missing_transcript_returns_none() -> None:
    assert audit.find_transcript("nope") is None


def test_unsafe_session_id_never_reaches_the_glob() -> None:
    """An id off disk is interpolated into a pattern — it must be anchored."""
    assert audit.find_transcript("../../etc/passwd") is None
    assert audit.find_transcript("*") is None
    assert audit.find_transcript("") is None


# --------------------------------------------------------------------------
# Phases and outcome
# --------------------------------------------------------------------------


def test_phase_metrics_read_transitions_off_the_event_log() -> None:
    eventlog.append(eventlog.EventKind.SESSION_STARTED, ticket="DR-1", session_id="s")
    eventlog.append(eventlog.EventKind.PR_LINKED, ticket="DR-1", detail="#7 open")
    eventlog.append(eventlog.EventKind.CI_CHANGED, ticket="DR-1", detail="#7: PENDING → FAILING")
    eventlog.append(
        eventlog.EventKind.REVIEW_CHANGED,
        ticket="DR-1",
        detail="#7: REVIEW_REQUIRED → CHANGES_REQUESTED",
    )
    eventlog.append(eventlog.EventKind.PR_STATE_CHANGED, ticket="DR-1", detail="#7: OPEN → MERGED")

    phases = audit.phase_metrics(eventlog.read(ticket="DR-1"))
    assert phases.ci_failures == 1
    assert phases.review_rounds == 1
    assert phases.time_to_pr_sec is not None
    assert phases.time_to_merge_sec is not None


def test_merged_without_rework_is_clean() -> None:
    work_items.record("DR-2", pr_number=1, pr_state="MERGED")
    item = work_items.load("DR-2")
    assert audit.classify(item, audit.PhaseMetrics()) is audit.Outcome.SHIPPED_CLEAN


def test_merged_after_ci_failure_is_rework() -> None:
    work_items.record("DR-3", pr_number=1, pr_state="MERGED")
    item = work_items.load("DR-3")
    phases = audit.PhaseMetrics(ci_failures=2)
    assert audit.classify(item, phases) is audit.Outcome.SHIPPED_REWORK


def test_open_pr_goes_stalled_only_once_it_is_quiet() -> None:
    work_items.record("DR-4", pr_number=1, pr_state="OPEN")
    item = work_items.load("DR-4")
    assert item is not None
    assert audit.classify(item, audit.PhaseMetrics()) is audit.Outcome.OPEN
    # Same record, read a fortnight later.
    import time as _time

    later = _time.time() + 14 * 86400
    assert audit.classify(item, audit.PhaseMetrics(), now=later) is audit.Outcome.STALLED


def test_no_pr_and_quiet_is_abandoned() -> None:
    import time as _time

    work_items.record("DR-5")
    item = work_items.load("DR-5")
    later = _time.time() + 14 * 86400
    assert audit.classify(item, audit.PhaseMetrics(), now=later) is audit.Outcome.ABANDONED


def test_no_work_record_is_unknown_not_abandoned() -> None:
    """Absence of a record is missing data, never evidence of failure."""
    assert audit.classify(None, audit.PhaseMetrics()) is audit.Outcome.UNKNOWN


# --------------------------------------------------------------------------
# Join, store, aggregate
# --------------------------------------------------------------------------


def test_audit_session_joins_transcript_to_outcome(_transcripts: Path) -> None:
    _write_transcript(
        _transcripts, "sX", [_turn(at="2026-09-21T10:00:00Z", out=1000, cache_read=50_000)]
    )
    work_items.record("DR-9", session_id="sX", pr_number=42, pr_state="MERGED", repo="ephor")

    built = audit.audit_session("sX", ticket="DR-9", item=work_items.load("DR-9"))
    assert built is not None
    assert built.ticket == "DR-9"
    assert built.pr_number == 42
    assert built.outcome == audit.Outcome.SHIPPED_CLEAN
    assert built.output_tokens == 1000
    assert built.shipped is True


def test_audit_session_without_transcript_is_none_not_a_row_of_zeroes() -> None:
    work_items.record("DR-10", session_id="ghost", pr_state="MERGED")
    assert audit.audit_session("ghost", ticket="DR-10") is None


def test_collect_joins_via_work_item_session_ids(_transcripts: Path) -> None:
    _write_transcript(_transcripts, "s1", [_turn(at="2026-09-21T10:00:00Z", out=10)])
    _write_transcript(_transcripts, "s2", [_turn(at="2026-09-21T11:00:00Z", out=20)])
    work_items.record("DR-11", session_id="s1", pr_number=1, pr_state="MERGED")

    everything = audit.collect()
    assert {a.session_id for a in everything} == {"s1", "s2"}
    by_id = {a.session_id: a for a in everything}
    assert by_id["s1"].ticket == "DR-11"
    assert by_id["s2"].ticket == ""  # a session with no work record still audits

    scoped = audit.collect(ticket="DR-11")
    assert [a.session_id for a in scoped] == ["s1"]


def test_records_round_trip_and_newest_wins() -> None:
    first = audit.SessionAudit(session_id="s", ticket="DR-12", equiv_cost_usd=1.0, pr_state="OPEN")
    assert audit.record(first)
    second = audit.SessionAudit(
        session_id="s", ticket="DR-12", equiv_cost_usd=2.0, pr_state="MERGED"
    )
    assert audit.record(second)

    loaded = audit.load_records()
    assert len(loaded) == 1
    assert loaded[0].pr_state == "MERGED"
    assert loaded[0].equiv_cost_usd == 2.0


def test_compact_collapses_the_append_only_store() -> None:
    for cost in (1.0, 2.0, 3.0):
        audit.record(audit.SessionAudit(session_id="s", equiv_cost_usd=cost))
    assert len(audit.audit_path().read_text().splitlines()) == 3
    assert audit.compact() == 1
    assert len(audit.audit_path().read_text().splitlines()) == 1
    assert audit.load_records()[0].equiv_cost_usd == 3.0


def test_corrupt_record_lines_are_skipped() -> None:
    audit.record(audit.SessionAudit(session_id="good"))
    with audit.audit_path().open("a") as handle:
        handle.write("{broken\n")
        handle.write('{"no_session_id": true}\n')
    assert [a.session_id for a in audit.load_records()] == ["good"]


def test_summary_headline_is_cost_per_merged_pr() -> None:
    audits = [
        audit.SessionAudit(
            session_id="a", ticket="T1", equiv_cost_usd=10.0, outcome=audit.Outcome.SHIPPED_CLEAN
        ),
        audit.SessionAudit(
            session_id="b", ticket="T2", equiv_cost_usd=30.0, outcome=audit.Outcome.SHIPPED_REWORK
        ),
        audit.SessionAudit(
            session_id="c", ticket="T3", equiv_cost_usd=20.0, outcome=audit.Outcome.STALLED
        ),
    ]
    summary = audit.summarize(audits)
    assert summary.merged == 2
    assert summary.equiv_cost_usd == pytest.approx(60.0)
    # Unit cost charges the two merged PRs for their own sessions ($10 + $30),
    # not for the $20 still sitting in a stalled one.
    assert summary.cost_per_merged_pr == pytest.approx(20.0)
    assert summary.fleet_cost_per_merged_pr == pytest.approx(30.0)
    assert summary.rework_rate == pytest.approx(0.5)
    assert summary.stalled == 1
    assert summary.tickets == 3


def test_cost_per_merged_pr_is_none_not_zero_when_nothing_merged() -> None:
    """Dividing by nothing is not free work — it is an unanswerable question."""
    summary = audit.summarize(
        [audit.SessionAudit(session_id="a", equiv_cost_usd=5.0, outcome=audit.Outcome.STALLED)]
    )
    assert summary.cost_per_merged_pr is None
    assert summary.rework_rate is None


def test_summary_surfaces_unpriced_models() -> None:
    summary = audit.summarize([audit.SessionAudit(session_id="a", unpriced_models=("mystery",))])
    assert summary.unpriced_models == ("mystery",)


# --------------------------------------------------------------------------
# Judge (phase 3)
# --------------------------------------------------------------------------


def test_parse_verdict_reads_json_wrapped_in_prose() -> None:
    verdict = audit.parse_verdict('Sure!\n{"met": true, "note": "adds the retry"}\nHope that helps')
    assert verdict.ok is True
    assert verdict.met is True
    assert verdict.note == "adds the retry"


def test_parse_verdict_preserves_a_refusal_to_decide() -> None:
    """`null` is a real answer; it must not collapse into False."""
    verdict = audit.parse_verdict('{"met": null, "note": "diffstat too thin"}')
    assert verdict.ok is True
    assert verdict.met is None


def test_parse_verdict_rejects_non_json() -> None:
    assert audit.parse_verdict("I think it probably worked").ok is False
    assert audit.parse_verdict("").ok is False


def test_judge_declines_without_pr_evidence() -> None:
    """No PR means nothing to grade — and no model call is made."""
    verdict = audit.judge(audit.SessionAudit(session_id="s", ticket="DR-1"))
    assert verdict.ok is False
    assert verdict.met is None


def test_gather_evidence_needs_a_pr_number() -> None:
    assert audit.gather_evidence(audit.SessionAudit(session_id="s")) == ""


def test_summary_separates_unit_cost_from_fleet_burn() -> None:
    """Most sessions are in flight; charging them to what shipped misleads."""
    audits = [
        audit.SessionAudit(
            session_id="merged",
            ticket="T1",
            equiv_cost_usd=10.0,
            outcome=audit.Outcome.SHIPPED_CLEAN,
        ),
        audit.SessionAudit(
            session_id="inflight", ticket="T2", equiv_cost_usd=90.0, outcome=audit.Outcome.OPEN
        ),
    ]
    summary = audit.summarize(audits)
    # Unit economics charge the merged PR only for the session that made it.
    assert summary.cost_per_merged_pr == pytest.approx(10.0)
    # The burn ratio charges it for everything spent in the window.
    assert summary.fleet_cost_per_merged_pr == pytest.approx(100.0)
    assert summary.merged_cost_usd == pytest.approx(10.0)


def test_unattributed_sessions_are_counted_not_hidden() -> None:
    """A session with no work record still spent money; it must appear."""
    summary = audit.summarize(
        [
            audit.SessionAudit(session_id="a", equiv_cost_usd=5.0, outcome=audit.Outcome.UNKNOWN),
            audit.SessionAudit(
                session_id="b",
                ticket="T",
                equiv_cost_usd=1.0,
                outcome=audit.Outcome.SHIPPED_CLEAN,
            ),
        ]
    )
    assert summary.unknown == 1
    assert summary.sessions == 2
    assert summary.equiv_cost_usd == pytest.approx(6.0)
    # Unattributed spend must not inflate the unit cost of what shipped.
    assert summary.cost_per_merged_pr == pytest.approx(1.0)
