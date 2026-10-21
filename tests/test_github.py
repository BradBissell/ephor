"""Tests for GitHub pull-request resolution behind the Jira cell."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ephor import github as gh
from ephor import gitinfo
from ephor.github import PrResolver, PullRequest, pr_for_branch, pr_for_ticket


@pytest.fixture(autouse=True)
def _clear_git_cache() -> None:
    """PrResolver's cache key is derived from memoized git facts — reset them."""
    gitinfo.clear_cache()


_PR_JSON = json.dumps(
    {
        "number": 42,
        "url": "https://github.com/acme/repo/pull/42",
        "state": "OPEN",
        "title": "DR-8222 add the thing",
        "isDraft": False,
    }
)


def _fake_gh(stdout: str, returncode: int = 0) -> object:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=list(args),  # type: ignore[arg-type]
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )

    return run


def test_pr_for_branch_parses_gh_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh(_PR_JSON))
    pr = pr_for_branch(tmp_path)
    assert pr == PullRequest(
        number=42,
        url="https://github.com/acme/repo/pull/42",
        state="OPEN",
        title="DR-8222 add the thing",
        draft=False,
    )
    assert pr is not None and pr.is_open


def test_pr_for_branch_returns_none_when_gh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh("", returncode=1))
    assert pr_for_branch(tmp_path) is None


def test_pr_for_branch_survives_missing_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(gh.subprocess, "run", boom)
    assert pr_for_branch(tmp_path) is None


def test_pr_for_branch_survives_garbage_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh("not json at all"))
    assert pr_for_branch(tmp_path) is None


def test_pr_for_ticket_takes_the_first_search_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh(f"[{_PR_JSON}]"))
    pr = pr_for_ticket(tmp_path, "DR-8222")
    assert pr is not None and pr.number == 42


def test_pr_for_ticket_returns_none_on_empty_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh("[]"))
    assert pr_for_ticket(tmp_path, "DR-8222") is None


def _fake_cli(
    gh_stdout: str,
    *,
    gh_returncode: int = 0,
    branch: str = "topic",
    toplevel: str = "/repo",
    gh_calls: list[list[str]] | None = None,
) -> object:
    """Stub subprocess.run for both CLIs, dispatching on argv.

    `gh.subprocess` and `gitinfo.subprocess` are the same module object, so
    patching one patches both. PrResolver now asks git for the branch to
    build its cache key, which means a resolver test has to answer git as
    well as gh — and count only the gh calls it means to assert on.
    """

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "git" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=f"{branch}\n{toplevel}\n", stderr=""
            )
        if gh_calls is not None:
            gh_calls.append(list(argv))
        return subprocess.CompletedProcess(
            args=argv, returncode=gh_returncode, stdout=gh_stdout, stderr=""
        )

    return run


# ---- PrResolver -----------------------------------------------------------


def test_resolver_caches_hits_and_does_not_re_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        gh.subprocess, "run", _fake_cli(_PR_JSON, toplevel=str(tmp_path), gh_calls=calls)
    )
    resolver = PrResolver()
    assert resolver.needs_refresh(tmp_path)
    first = resolver.resolve(tmp_path)
    assert first is not None
    assert resolver.resolve(tmp_path) is first
    assert len(calls) == 1
    # cached() is the render-path read: no subprocess, same answer.
    assert resolver.cached(tmp_path) is first
    assert not resolver.needs_refresh(tmp_path)


def test_resolver_cached_is_none_before_any_probe(tmp_path: Path) -> None:
    assert PrResolver().cached(tmp_path) is None


def test_resolver_miss_expires_sooner_than_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gh.subprocess,
        "run",
        _fake_cli("", gh_returncode=1, toplevel=str(tmp_path)),
    )
    resolver = PrResolver(hit_ttl=1000.0, miss_ttl=0.0)
    assert resolver.resolve(tmp_path) is None
    # A zero-length negative TTL means the next sweep probes again — that's
    # how a PR opened mid-session shows up without restarting ephor.
    assert resolver.needs_refresh(tmp_path)


def test_resolver_falls_back_to_ticket_search_when_branch_has_no_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        if "view" in argv:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=f"[{_PR_JSON}]", stderr=""
        )

    monkeypatch.setattr(gh.subprocess, "run", run)
    pr = PrResolver().resolve(tmp_path, ticket="DR-8222")
    assert pr is not None and pr.number == 42
    assert any("DR-8222" in argv for argv in seen)


def test_resolver_skips_missing_directories(tmp_path: Path) -> None:
    resolver = PrResolver()
    assert resolver.resolve(tmp_path / "gone") is None
    assert resolver.resolve(None) is None
    assert resolver.cached(None) is None


def test_resolver_invalidate_clears_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh(_PR_JSON))
    resolver = PrResolver()
    resolver.resolve(tmp_path)
    resolver.invalidate(tmp_path)
    assert resolver.cached(tmp_path) is None


def test_pr_fields_request_body_and_head_ref() -> None:
    """The Jira harvester reads a key back out of these — see ephor.jira."""
    for field in ("body", "headRefName"):
        assert field in gh._PR_FIELDS


def test_parse_pr_keeps_body_and_head_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "number": 7,
            "url": "https://github.com/acme/repo/pull/7",
            "state": "OPEN",
            "title": "add the thing",
            "isDraft": False,
            "body": "Closes DR-8222",
            "headRefName": "feature/whatever",
        }
    )
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh(payload))
    pr = pr_for_branch(tmp_path)
    assert pr is not None
    assert pr.body == "Closes DR-8222"
    assert pr.head_ref == "feature/whatever"


def test_resolver_key_follows_the_branch_not_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR belongs to a branch, so a branch switch must miss the cache.

    Keyed on cwd, a shared checkout served the previous branch's review for
    the rest of the hour-long hit TTL.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(
        gh.subprocess,
        "run",
        _fake_cli(_PR_JSON, branch="DR-1-first", toplevel=str(tmp_path), gh_calls=calls),
    )
    resolver = PrResolver()
    assert resolver.resolve(tmp_path) is not None
    assert len(calls) == 1
    assert not resolver.needs_refresh(tmp_path)

    # Same directory, different branch.
    gitinfo.clear_cache()
    monkeypatch.setattr(
        gh.subprocess,
        "run",
        _fake_cli(_PR_JSON, branch="DR-2-second", toplevel=str(tmp_path), gh_calls=calls),
    )
    assert resolver.needs_refresh(tmp_path), "branch switch re-served a stale entry"
    assert resolver.resolve(tmp_path) is not None
    assert len(calls) == 2


def test_resolver_falls_back_to_the_path_outside_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-repo directories (and detached heads) still cache per directory."""

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "git" in argv:
            return subprocess.CompletedProcess(args=argv, returncode=128, stdout="", stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=_PR_JSON, stderr="")

    monkeypatch.setattr(gh.subprocess, "run", run)
    resolver = PrResolver()
    assert resolver.resolve(tmp_path) is not None
    assert resolver.cached(tmp_path) is not None


# ---- head sha + ticket-wide lookup (flows) --------------------------------


def test_pr_fields_carry_the_head_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({**json.loads(_PR_JSON), "headRefOid": "abc123"})
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh(payload))
    pr = pr_for_branch(tmp_path)
    assert pr is not None and pr.head_sha == "abc123"
    assert "headRefOid" in gh._PR_FIELDS.split(",")


def _ticket_pr(number: int, title: str, state: str = "OPEN", **extra: object) -> dict[str, object]:
    return {
        "number": number,
        "url": f"https://github.com/acme/repo/pull/{number}",
        "state": state,
        "title": title,
        "headRefOid": f"sha{number}",
        "headRefName": "",
        "body": "",
        **extra,
    }


def test_find_prs_for_ticket_returns_every_pr_naming_the_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    stdout = json.dumps(
        [
            _ticket_pr(3, "DR-12 second attempt"),
            _ticket_pr(2, "first attempt", "CLOSED", headRefName="DR-12-first"),
            _ticket_pr(1, "unrelated", "MERGED", body="Fixes dr-12."),
        ]
    )
    monkeypatch.setattr(gh.subprocess, "run", _fake_cli(stdout, gh_calls=calls))
    prs = gh.find_prs_for_ticket(tmp_path, "DR-12")
    assert prs is not None
    assert [(p.number, p.state, p.head_sha) for p in prs] == [
        (3, "OPEN", "sha3"),
        (2, "CLOSED", "sha2"),
        (1, "MERGED", "sha1"),
    ]
    argv = calls[0]
    assert argv[argv.index("--search") + 1] == "DR-12"
    assert argv[argv.index("--state") + 1] == "all"
    fields = argv[argv.index("--json") + 1].split(",")
    assert {"number", "state", "headRefOid", "url", "title"} <= set(fields)


def test_find_prs_for_ticket_drops_hits_for_a_longer_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub search can surface DR-123 for DR-12; reviewing it would be wrong."""
    stdout = json.dumps([_ticket_pr(9, "DR-123 other work"), _ticket_pr(8, "XDR-12 lookalike")])
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh(stdout))
    assert gh.find_prs_for_ticket(tmp_path, "DR-12") == []


def test_find_prs_for_ticket_distinguishes_no_pr_from_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh("[]"))
    assert gh.find_prs_for_ticket(tmp_path, "DR-1") == []

    monkeypatch.setattr(gh.subprocess, "run", _fake_gh("", returncode=1))
    assert gh.find_prs_for_ticket(tmp_path, "DR-1") is None

    monkeypatch.setattr(gh.subprocess, "run", _fake_gh("garbage"))
    assert gh.find_prs_for_ticket(tmp_path, "DR-1") is None

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("gh must not run for a missing repo")

    monkeypatch.setattr(gh.subprocess, "run", boom)
    assert gh.find_prs_for_ticket(tmp_path / "missing", "DR-1") is None


def test_find_prs_for_ticket_ignores_a_passing_mention_in_another_pr_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DR-99's PR saying "Follow-up to DR-12" is not DR-12's PR."""
    stdout = json.dumps(
        [
            _ticket_pr(
                7,
                "DR-99 thing",
                headRefName="dr-99-thing",
                body="Follow-up to DR-12.\nCloses DR-12",
            ),
            _ticket_pr(6, "DR-99: follow-up to DR-12"),
            _ticket_pr(5, "no key here", body="Blocked by DR-12 for now"),
        ]
    )
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh(stdout))
    assert gh.find_prs_for_ticket(tmp_path, "DR-12") == []


def test_find_prs_for_ticket_trusts_the_branch_over_the_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = json.dumps(
        [
            _ticket_pr(4, "DR-12 tidy-up", headRefName="bb/DR-99-tidy"),
            _ticket_pr(3, "tidy-up", headRefName="bb/dr-12-tidy"),
            _ticket_pr(2, "plain title", body="Some context.\n\nJira: DR-12\n"),
        ]
    )
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh(stdout))
    prs = gh.find_prs_for_ticket(tmp_path, "dr-12")
    assert prs is not None
    assert [p.number for p in prs] == [3, 2]


def test_find_prs_for_ticket_treats_a_full_page_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full page may have pushed the ticket's own PR off the end."""
    calls: list[list[str]] = []
    stdout = json.dumps(
        [_ticket_pr(1000 + i, f"DR-{100 + i} other") for i in range(gh._TICKET_PR_LIMIT)]
    )
    monkeypatch.setattr(gh.subprocess, "run", _fake_cli(stdout, gh_calls=calls))
    assert gh.find_prs_for_ticket(tmp_path, "DR-1") is None
    argv = calls[0]
    assert "--limit" in argv
    assert argv[argv.index("--limit") + 1] == str(gh._TICKET_PR_LIMIT)
