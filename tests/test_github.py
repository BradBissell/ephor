"""Tests for GitHub pull-request resolution behind the Jira cell."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ephor import github as gh
from ephor.github import PrResolver, PullRequest, pr_for_branch, pr_for_ticket

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


# ---- PrResolver -----------------------------------------------------------


def test_resolver_caches_hits_and_does_not_re_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess(
            args=list(args),  # type: ignore[arg-type]
            returncode=0,
            stdout=_PR_JSON,
            stderr="",
        )

    monkeypatch.setattr(gh.subprocess, "run", run)
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
    monkeypatch.setattr(gh.subprocess, "run", _fake_gh("", returncode=1))
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
