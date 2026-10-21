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


# --- ids, search, myself, fetch_fresh (flows) ------------------------------


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPHOR_JIRA_URL", "https://mine.example")
    monkeypatch.setenv("EPHOR_JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("EPHOR_JIRA_TOKEN", "t")


def _issue(key: str, status: str = "In Progress", status_id: str = "3") -> dict[str, object]:
    return {
        "key": key,
        "fields": {
            "summary": f"title {key}",
            "status": {"name": status, "id": status_id, "statusCategory": {"key": "indeterminate"}},
            "assignee": {"displayName": "Me", "accountId": "acc-1"},
        },
    }


def test_parses_status_and_assignee_ids() -> None:
    issue = jira_api._parse_issue("DR-1", _issue("DR-1", "Peer Review", "10001"))
    assert issue is not None
    assert issue.status_id == "10001"
    assert issue.assignee_id == "acc-1"
    # Unassigned tickets and old payloads degrade to empty ids.
    bare = jira_api._parse_issue("DR-1", _PAYLOAD)
    assert bare is not None and bare.status_id == "" and bare.assignee_id == ""


def test_search_follows_page_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    pages = [
        {"issues": [_issue("DR-1"), _issue("DR-2")], "nextPageToken": "p2", "isLast": False},
        {"issues": [_issue("DR-3")], "isLast": True},
    ]
    bodies: list[dict[str, object]] = []

    def fake_request(creds, path, body=None):
        assert path == "/rest/api/3/search/jql"
        assert body is not None
        bodies.append(body)
        return pages[len(bodies) - 1]

    monkeypatch.setattr(jira_api, "_request", fake_request)
    issues = jira_api.search("project in (DR)")
    assert issues is not None
    assert [i.key for i in issues] == ["DR-1", "DR-2", "DR-3"]
    assert "nextPageToken" not in bodies[0]
    assert bodies[1]["nextPageToken"] == "p2"
    assert bodies[0]["fields"] == ["summary", "status", "assignee"]
    assert bodies[0]["jql"] == "project in (DR)"


def test_search_fails_whole_when_any_page_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A board missing its second page would look like tickets leaving it."""
    _configure(monkeypatch)
    pages: list[object] = [{"issues": [_issue("DR-1")], "nextPageToken": "p2"}, None]
    monkeypatch.setattr(jira_api, "_request", lambda creds, path, body=None: pages.pop(0))
    assert jira_api.search("x") is None


def test_search_rejects_a_malformed_page(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(jira_api, "_request", lambda creds, path, body=None: {"nope": 1})
    assert jira_api.search("x") is None


def test_search_stops_on_a_repeated_token_and_at_the_page_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    calls: list[object] = []

    def looping(creds, path, body=None):
        calls.append(body)
        return {"issues": [_issue(f"DR-{len(calls)}")], "nextPageToken": "same"}

    monkeypatch.setattr(jira_api, "_request", looping)
    issues = jira_api.search("x")
    assert issues is not None and len(issues) == 2  # second page repeated the token
    assert len(calls) == 2

    calls.clear()
    counter = iter(range(100))

    def endless(creds, path, body=None):
        calls.append(body)
        return {"issues": [_issue("DR-1")], "nextPageToken": f"t{next(counter)}"}

    monkeypatch.setattr(jira_api, "_request", endless)
    issues = jira_api.search("x", max_pages=3)
    assert issues is not None and len(issues) == 3
    assert len(calls) == 3


def test_search_and_myself_stay_offline_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("a request was made without credentials")

    monkeypatch.setattr(jira_api, "_request", explode)
    assert jira_api.search("project in (DR)") is None
    assert jira_api.myself() is None
    assert jira_api.fetch_fresh("DR-1") is None


def test_myself_returns_the_account_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    paths: list[str] = []

    def fake_request(creds, path, body=None):
        paths.append(path)
        return {"accountId": "acc-1", "displayName": "Me"}

    monkeypatch.setattr(jira_api, "_request", fake_request)
    assert jira_api.myself() == "acc-1"
    assert paths == ["/rest/api/3/myself"]

    monkeypatch.setattr(jira_api, "_request", lambda creds, path, body=None: {"accountId": ""})
    assert jira_api.myself() is None


def test_fetch_fresh_never_uses_a_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    statuses = iter(["In Progress", "Peer Review"])
    monkeypatch.setattr(
        jira_api, "_fetch", lambda key, creds: Issue(key=key, status=next(statuses))
    )
    first = jira_api.fetch_fresh("dr-1")
    second = jira_api.fetch_fresh("DR-1")
    assert first is not None and first.key == "DR-1" and first.status == "In Progress"
    assert second is not None and second.status == "Peer Review"


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_request_posts_json_with_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        seen.update(
            url=request.full_url,
            method=request.get_method(),
            data=request.data,
            content_type=request.get_header("Content-type"),
            auth=request.get_header("Authorization"),
        )
        return _FakeResponse(b'{"issues": [], "isLast": true}')

    monkeypatch.setattr(jira_api.urllib.request, "urlopen", fake_urlopen)
    assert jira_api.search("project in (DR)") == []
    assert seen["url"] == "https://mine.example/rest/api/3/search/jql"
    assert seen["method"] == "POST"
    assert seen["content_type"] == "application/json"
    assert str(seen["auth"]).startswith("Basic ")
    assert b'"jql": "project in (DR)"' in bytes(seen["data"])  # type: ignore[arg-type]


def test_request_returns_none_on_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    def failing(request, timeout):
        raise jira_api.urllib.error.HTTPError(request.full_url, 401, "no", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(jira_api.urllib.request, "urlopen", failing)
    assert jira_api.search("x") is None
    assert jira_api.myself() is None
    assert jira_api.fetch_fresh("DR-1") is None


class _TruncatedResponse(_FakeResponse):
    """A body cut off mid-read, as a flaky VPN produces."""

    def read(self) -> bytes:
        import http.client

        raise http.client.IncompleteRead(b"partial")


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(lambda: _TruncatedResponse(b""), id="incomplete-read"),
        pytest.param(lambda: _FakeResponse(b"[" * 100000), id="deeply-nested-json"),
        pytest.param(lambda: _FakeResponse(b"\xff\xfe not utf-8"), id="undecodable"),
    ],
)
def test_request_returns_none_on_any_failure_reading_the_body(
    monkeypatch: pytest.MonkeyPatch, response: object
) -> None:
    """Neither IncompleteRead nor RecursionError is an OSError; both must stay inside."""
    _configure(monkeypatch)
    monkeypatch.setattr(
        jira_api.urllib.request,
        "urlopen",
        lambda request, timeout: response(),  # type: ignore[operator]
    )
    assert jira_api.search("project = DR") is None
    assert jira_api.myself() is None
    assert jira_api.fetch_fresh("DR-1") is None
