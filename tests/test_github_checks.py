"""CI / review rollup parsing and the work-attention it produces."""

from __future__ import annotations

from ephor.constants import CiState, ReviewState, WorkAttention
from ephor.github import PENDING_TTL_SEC, PrResolver, PullRequest, _parse_pr


def _pr(**fields: object) -> PullRequest:
    payload = {"number": 1, "url": "https://x/1", "state": "OPEN", **fields}
    parsed = _parse_pr(payload)
    assert parsed is not None
    return parsed


def test_one_failure_decides_the_rollup() -> None:
    pr = _pr(
        statusCheckRollup=[
            {"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "e2e", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
    )
    assert pr.ci is CiState.FAILING
    assert pr.failing_checks == ("e2e",)


def test_skipped_and_neutral_checks_are_not_failures() -> None:
    """A matrix full of conditionally-skipped jobs must not light up red."""
    pr = _pr(
        statusCheckRollup=[
            {"name": "a", "status": "COMPLETED", "conclusion": "SKIPPED"},
            {"name": "b", "status": "COMPLETED", "conclusion": "NEUTRAL"},
            {"name": "c", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
    )
    assert pr.ci is CiState.PASSING
    assert pr.failing_checks == ()


def test_running_checks_read_as_pending_not_passing() -> None:
    pr = _pr(
        statusCheckRollup=[
            {"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "b", "status": "IN_PROGRESS"},
        ]
    )
    assert pr.ci is CiState.PENDING


def test_no_checks_is_distinct_from_pending() -> None:
    assert _pr(statusCheckRollup=[]).ci is CiState.NONE
    assert _pr().ci is CiState.NONE


def test_legacy_status_contexts_are_understood() -> None:
    """The older StatusContext shape carries `state` and `context`."""
    pr = _pr(statusCheckRollup=[{"context": "ci/jenkins", "state": "FAILURE"}])
    assert pr.ci is CiState.FAILING
    assert pr.failing_checks == ("ci/jenkins",)


def test_the_nested_gh_pr_list_shape_is_normalized() -> None:
    """`gh pr list` nests the same contexts one level deeper than `gh pr view`."""
    pr = _pr(
        statusCheckRollup=[
            {
                "contexts": {
                    "nodes": [{"name": "unit", "status": "COMPLETED", "conclusion": "FAILURE"}]
                }
            }
        ]
    )
    assert pr.ci is CiState.FAILING
    assert pr.failing_checks == ("unit",)


def test_review_decision_and_outstanding_requests() -> None:
    pr = _pr(reviewDecision="CHANGES_REQUESTED", reviewRequests=[{"login": "a"}])
    assert pr.review is ReviewState.CHANGES_REQUESTED
    assert pr.reviewers_requested == 1

    # No decision but someone has been asked still means review is required.
    assert _pr(reviewRequests=[{"login": "a"}]).review is ReviewState.REVIEW_REQUIRED
    # Nobody asked, no decision: nothing is pending on a human.
    assert _pr().review is ReviewState.NONE


def test_conflict_is_asserted_by_either_field() -> None:
    assert _pr(mergeable="CONFLICTING").has_conflict is True
    assert _pr(mergeStateStatus="DIRTY").has_conflict is True
    # BLOCKED means "not approved yet", which is not a conflict.
    assert _pr(mergeStateStatus="BLOCKED", mergeable="MERGEABLE").has_conflict is False


def test_attention_is_ordered_most_urgent_first() -> None:
    pr = _pr(
        statusCheckRollup=[{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}],
        reviewDecision="CHANGES_REQUESTED",
        mergeable="CONFLICTING",
    )
    assert pr.attention() is WorkAttention.CI_FAILED


def test_a_finished_pr_generates_no_attention() -> None:
    """Red CI on a merged PR is history, not a task."""
    payload = {
        "number": 1,
        "url": "u",
        "state": "MERGED",
        "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}],
    }
    pr = _parse_pr(payload)
    assert pr is not None
    assert pr.ci is CiState.FAILING
    assert pr.attention() is None


def test_a_draft_does_not_demand_review() -> None:
    draft = _pr(isDraft=True, reviewDecision="REVIEW_REQUIRED", reviewRequests=[{"login": "a"}])
    assert draft.attention() is None
    ready = _pr(reviewDecision="REVIEW_REQUIRED", reviewRequests=[{"login": "a"}])
    assert ready.attention() is WorkAttention.REVIEW_REQUESTED


def test_open_prs_expire_faster_than_finished_ones() -> None:
    """The board is only as honest as this TTL."""
    resolver = PrResolver()
    merged = _parse_pr({"number": 1, "url": "u", "state": "MERGED"})
    open_pending = _pr(statusCheckRollup=[{"name": "a", "status": "IN_PROGRESS"}])
    open_settled = _pr(
        statusCheckRollup=[{"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    )

    assert resolver._ttl_for(merged) > resolver._ttl_for(open_settled)
    assert resolver._ttl_for(open_pending) == PENDING_TTL_SEC
    assert resolver._ttl_for(None) < resolver._ttl_for(merged)
