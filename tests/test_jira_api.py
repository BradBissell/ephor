"""Jira reads: opt-in gating, parsing, drift detection."""

from __future__ import annotations

import pytest

from ephor import jira_api
from ephor.jira_api import Issue, IssueResolver

_PAYLOAD = {
    "key": "DR-8222",
    "fields": {
        "summary": "Add retry to the upload client",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "assignee": {"displayName": "A Developer"},
    },
}


def test_stays_offline_until_credentials_are_configured() -> None:
    assert jira_api.is_configured() is False
    assert jira_api.credentials() is None
    # And a resolve is a no-op rather than a failed request.
    assert IssueResolver().resolve("DR-1") is None


def test_credentials_prefer_ephor_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_URL", "https://other.example")
    monkeypatch.setenv("JIRA_EMAIL", "other@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "other")
    creds = jira_api.credentials()
    assert creds is not None and creds.base_url == "https://other.example"

    monkeypatch.setenv("EPHOR_JIRA_URL", "https://mine.example/")
    monkeypatch.setenv("EPHOR_JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("EPHOR_JIRA_TOKEN", "mine")
    creds = jira_api.credentials()
    assert creds is not None
    assert creds.base_url == "https://mine.example"  # trailing slash stripped
    assert creds.email == "me@example.com"


def test_parses_the_fields_the_dashboard_uses() -> None:
    issue = jira_api._parse_issue("DR-8222", _PAYLOAD)
    assert issue is not None
    assert issue.key == "DR-8222"
    assert issue.title == "Add retry to the upload client"
    assert issue.status == "In Progress"
    assert issue.assignee == "A Developer"
    assert issue.is_done is False


def test_malformed_payloads_return_none() -> None:
    assert jira_api._parse_issue("DR-1", None) is None
    assert jira_api._parse_issue("DR-1", {"no": "fields"}) is None
    # Missing sub-objects degrade to empty strings, not exceptions.
    issue = jira_api._parse_issue("DR-1", {"fields": {}})
    assert issue is not None and issue.status == ""


def test_done_prefers_the_status_category_over_the_name() -> None:
    """A team that renamed Done to "Shipped" must still read as finished."""
    assert Issue(key="D", status="Shipped", status_category="done").is_done is True
    assert Issue(key="D", status="Done", status_category="indeterminate").is_done is False
    # No category at all falls back to matching the name.
    assert Issue(key="D", status="Closed").is_done is True
    assert Issue(key="D", status="In Review").is_done is False


def test_done_statuses_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_JIRA_DONE_STATUSES", "shipped, live")
    assert Issue(key="D", status="Shipped").is_done is True
    assert Issue(key="D", status="Done").is_done is False


def test_drift_reports_only_the_two_expensive_directions() -> None:
    in_progress = Issue(key="D", status="In Progress", status_category="indeterminate")
    done = Issue(key="D", status="Done", status_category="done")

    assert jira_api.drift(in_progress, "MERGED") == "PR merged, ticket still In Progress"
    assert jira_api.drift(done, "OPEN") == "ticket Done, PR still open"
    # A ticket in flight with an open PR is not drift, it is Tuesday.
    assert jira_api.drift(in_progress, "OPEN") is None
    assert jira_api.drift(done, "MERGED") is None
    assert jira_api.drift(None, "MERGED") is None


def test_resolver_caches_and_can_be_invalidated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_JIRA_URL", "https://mine.example")
    monkeypatch.setenv("EPHOR_JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("EPHOR_JIRA_TOKEN", "t")
    calls: list[str] = []

    def fake_fetch(key: str, creds: jira_api.Credentials) -> Issue:
        calls.append(key)
        return Issue(key=key, status="In Progress")

    monkeypatch.setattr(jira_api, "_fetch", fake_fetch)
    resolver = IssueResolver()
    assert resolver.needs_refresh("DR-1") is True
    resolver.resolve("DR-1")
    resolver.resolve("dr-1")  # same ticket, different casing
    assert calls == ["DR-1"]
    assert resolver.cached("DR-1") is not None
    assert resolver.needs_refresh("DR-1") is False

    resolver.invalidate("DR-1")
    assert resolver.cached("DR-1") is None


def test_plain_http_sites_never_receive_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential must not go out in the clear, even to a named host."""
    monkeypatch.setenv("EPHOR_JIRA_URL", "http://insecure.example")
    monkeypatch.setenv("EPHOR_JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("EPHOR_JIRA_TOKEN", "t")

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("a request was made over plain http")

    monkeypatch.setattr(jira_api.urllib.request, "urlopen", explode)
    creds = jira_api.credentials()
    assert creds is not None
    assert jira_api._fetch("DR-1", creds) is None
