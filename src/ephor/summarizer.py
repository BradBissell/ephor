"""Conversation summary for the dashboard summary column (and summary-mode TTS).

Two interchangeable backends produce the one-sentence summary; both receive the
same compact transcript and are asked for a single ≤70-char sentence.

1. **claude** (default) — shells out to the `claude` CLI in non-interactive
   mode (`claude -p`), the documented programmatic surface for Claude Code.
   The CLI handles subscription auth (Pro/Max/Team/Enterprise) with no token
   plumbing or SDK dependency on our side. https://code.claude.com/docs/en/headless

2. **openai** — POSTs to any OpenAI-compatible `/chat/completions` endpoint.
   This lets you point summarization at a local inference server (llama-swap /
   vLLM / Ollama / LM Studio, …) or any hosted OpenAI-compatible API, keeping
   transcripts on your own hardware and off the Claude auth path.

Backend selection (env, read fresh on every call so it's live-tunable):

  EPHOR_SUMMARY_BACKEND   "claude" | "openai" (optional explicit override)
  EPHOR_SUMMARY_API_BASE  e.g. http://localhost:8000/v1  — setting this alone
                          switches the backend to openai
  EPHOR_SUMMARY_MODEL     model name sent in the request (required for openai;
                          for llama-swap this is what triggers the model swap)
  EPHOR_SUMMARY_API_KEY   optional bearer token (omitted if unset — most local
                          servers don't require one)
  EPHOR_SUMMARY_TIMEOUT   request timeout in seconds (default 30; raise it if a
                          cold llama-swap model load is slow)
  EPHOR_SUMMARY_MAX_TOKENS  completion token budget (default 128)
  EPHOR_SUMMARY_EXTRA_BODY  JSON object merged into the request body — the
                          portable escape hatch for server-specific options.
                          For a *reasoning* model (Qwen3, etc.) disable the
                          think phase so a one-line summary isn't truncated
                          mid-thought:
                            EPHOR_SUMMARY_EXTRA_BODY='{"chat_template_kwargs":{"enable_thinking":false}}'
                          Any `<think>…</think>` still present in the reply is
                          stripped defensively.

Defensive throughout: returns "" silently when a backend is unavailable, the
call fails or times out, or the response is malformed. The dashboard treats ""
as "no summary yet" and shows "—".

EPHOR_INTERNAL=1 is set in the `claude -p` subprocess env so ephor's own hook
handler short-circuits — otherwise every summary call would create a ghost
session in the dashboard. (The openai backend spawns no agent, so it needs no
such guard.)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ephor.jira import ticket_for_cwd

log = logging.getLogger(__name__)

# Number of trailing transcript entries to feed the summarizer. Enough for
# context, small enough to keep latency tight.
RECENT_TURNS = 30

# Hard cap on summary length. The model is asked for ≤70 but we defend
# against runaway outputs.
MAX_LENGTH = 70

# Subprocess timeout. Claude Code startup adds ~1s, model reply ~1-2s for Haiku;
# 30s is generous and prevents wedged terminals from hanging the TUI worker.
SUBPROCESS_TIMEOUT_SEC = 30.0

# Default HTTP request timeout for the openai backend, overridable via
# EPHOR_SUMMARY_TIMEOUT (a cold llama-swap model load may need longer).
DEFAULT_REQUEST_TIMEOUT_SEC = 30.0

# Env vars selecting/parameterising the summary backend. Read on every call.
ENV_BACKEND = "EPHOR_SUMMARY_BACKEND"
ENV_API_BASE = "EPHOR_SUMMARY_API_BASE"
ENV_MODEL = "EPHOR_SUMMARY_MODEL"
ENV_API_KEY = "EPHOR_SUMMARY_API_KEY"
ENV_TIMEOUT = "EPHOR_SUMMARY_TIMEOUT"
ENV_MAX_TOKENS = "EPHOR_SUMMARY_MAX_TOKENS"
ENV_EXTRA_BODY = "EPHOR_SUMMARY_EXTRA_BODY"

BACKEND_CLAUDE = "claude"
BACKEND_OPENAI = "openai"

# Completion token budget for the openai backend. A one-line summary needs ~30
# tokens; 128 leaves headroom without inviting a runaway reply. Reasoning
# models should disable thinking via EPHOR_SUMMARY_EXTRA_BODY rather than rely
# on a huge budget.
DEFAULT_MAX_TOKENS = 128

# Strips a `<think>…</think>` block some reasoning models inline into content.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

_SYSTEM_PROMPT = (
    "You summarize what an autonomous coding agent is currently working on. "
    "Read the most recent turns of the transcript and return ONE sentence "
    f"(max {MAX_LENGTH} characters) describing the current task. "
    "Output the sentence only — no preamble, no quotes, no trailing period."
)


def _extract_messages(path: Path) -> list[dict[str, str]]:
    """Read jsonl, return user/assistant text pairs (no tool noise).

    Tool calls are noise for a one-line summary — collapse them by skipping
    assistant messages whose content is purely tool_use, and skip user
    messages that are tool_result echoes.
    """
    messages: list[dict[str, str]] = []
    try:
        with path.open() as fh:
            lines = fh.readlines()
    except OSError:
        return []

    for raw in lines[-RECENT_TURNS * 4 :]:  # 4x to absorb tool/intermediate lines
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            continue

        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue

        text = _extract_text(msg.get("content"))
        if not text:
            continue
        messages.append({"role": role, "content": text})

    if len(messages) > RECENT_TURNS:
        messages = messages[-RECENT_TURNS:]
    return messages


def _extract_text(content: Any) -> str:
    """Pull plain text out of a Claude Code transcript content field."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            t = block.get("text", "")
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
    return "\n".join(parts)


def _format_for_prompt(messages: list[dict[str, str]]) -> str:
    """Render the messages as a plain-text transcript suitable for stdin.

    Format kept deliberately simple — we want Claude to spend tokens on the
    summary, not on parsing structure.
    """
    parts: list[str] = []
    for m in messages:
        role = "USER" if m["role"] == "user" else "ASSISTANT"
        parts.append(f"{role}: {m['content']}")
    return "\n\n".join(parts)


def _claude_binary() -> str | None:
    """Resolve the `claude` CLI path, or None if not installed."""
    return shutil.which("claude")


def summarize_transcript(path: Path, cwd: str | Path | None = None) -> str:
    """Return a one-sentence summary of the transcript, or "" on any failure.

    Dispatches to the configured backend (claude CLI or an OpenAI-compatible
    HTTP endpoint — see the module docstring). When ``cwd`` is supplied and a
    Jira ticket key can be resolved (from the branch name or any directory in
    the path), the summary is prefixed with ``<KEY>: ``. The ticket-prefix
    consumes part of the MAX_LENGTH budget so the truncation rule still
    produces a single bounded line.

    All exceptions are caught and logged at DEBUG so the UI never sees a
    stack trace.
    """
    messages = _extract_messages(path)
    if not messages:
        return ""

    prompt_text = _format_for_prompt(messages)

    if _resolve_backend() == BACKEND_OPENAI:
        raw = _summarize_via_openai(prompt_text)
    else:
        raw = _summarize_via_claude(prompt_text)

    if not raw:
        return ""
    return _postprocess(raw, cwd)


def _resolve_backend() -> str:
    """Decide which summary backend to use, read fresh from the environment.

    Explicit ``EPHOR_SUMMARY_BACKEND`` wins; otherwise setting an API base
    implies the openai backend; the default is the claude CLI.
    """
    explicit = (os.environ.get(ENV_BACKEND) or "").strip().lower()
    if explicit in (BACKEND_CLAUDE, BACKEND_OPENAI):
        return explicit
    if (os.environ.get(ENV_API_BASE) or "").strip():
        return BACKEND_OPENAI
    return BACKEND_CLAUDE


def _summarize_via_claude(prompt_text: str) -> str:
    """Raw summary text from `claude -p`, or "" on any failure."""
    binary = _claude_binary()
    if binary is None:
        return ""

    # EPHOR_INTERNAL flags this invocation as a ephor-internal call so the
    # event_handler.sh hook short-circuits and doesn't write a state file
    # for the summarizer subprocess.
    env = {**os.environ, "EPHOR_INTERNAL": "1"}

    try:
        proc = subprocess.run(
            [
                binary,
                "-p",
                "--append-system-prompt",
                _SYSTEM_PROMPT,
                "--output-format",
                "json",
            ],
            input=prompt_text,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
            env=env,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("claude -p subprocess failed: %s", exc)
        return ""

    if proc.returncode != 0:
        log.debug("claude -p exited %d: %s", proc.returncode, proc.stderr.strip()[:200])
        return ""

    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        log.debug("claude -p produced non-JSON output: %s", proc.stdout[:200])
        return ""

    result = data.get("result") if isinstance(data, dict) else None
    return result if isinstance(result, str) else ""


def _request_timeout() -> float:
    raw = os.environ.get(ENV_TIMEOUT)
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            log.debug("ignoring non-numeric %s=%r", ENV_TIMEOUT, raw)
    return DEFAULT_REQUEST_TIMEOUT_SEC


def _summarize_via_openai(prompt_text: str) -> str:
    """Raw summary text from an OpenAI-compatible /chat/completions endpoint,
    or "" on any failure. Uses only the stdlib (no SDK / extra dependency)."""
    base = (os.environ.get(ENV_API_BASE) or "").strip()
    model = (os.environ.get(ENV_MODEL) or "").strip()
    if not base:
        return ""
    if not model:
        log.debug("%s is set but %s is missing; cannot summarize via HTTP", ENV_API_BASE, ENV_MODEL)
        return ""

    endpoint = base.rstrip("/") + "/chat/completions"
    if not endpoint.startswith(("http://", "https://")):
        log.debug("%s must be an http(s) URL: %r", ENV_API_BASE, base)
        return ""

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt_text},
        ],
        "max_tokens": _max_tokens(),
        "temperature": 0.0,
        "stream": False,
    }
    # Merge the operator's extra body last so it can add/override anything
    # (e.g. chat_template_kwargs to disable a reasoning model's think phase).
    payload.update(_extra_body())

    headers = {"Content-Type": "application/json"}
    key = (os.environ.get(ENV_API_KEY) or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    req = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        # Scheme is validated http(s) above; the URL is operator-configured
        # (trusted env var). S310 is waived for this file in pyproject.toml.
        with urllib.request.urlopen(req, timeout=_request_timeout()) as resp:
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.debug("summary API request to %s failed: %s", endpoint, exc)
        return ""

    try:
        obj = json.loads(body)
        content = obj["choices"][0]["message"]["content"]
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        log.debug("summary API returned unexpected payload: %s", exc)
        return ""

    if not isinstance(content, str):
        # Reasoning models return content=null when the think phase consumes
        # the whole token budget — a hint the caller should disable thinking
        # (EPHOR_SUMMARY_EXTRA_BODY) or raise EPHOR_SUMMARY_MAX_TOKENS.
        log.debug(
            "summary API returned non-string content (%r); see EPHOR_SUMMARY_EXTRA_BODY", content
        )
        return ""
    return _THINK_RE.sub("", content).strip()


def _max_tokens() -> int:
    raw = os.environ.get(ENV_MAX_TOKENS)
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            log.debug("ignoring non-integer %s=%r", ENV_MAX_TOKENS, raw)
    return DEFAULT_MAX_TOKENS


def _extra_body() -> dict[str, Any]:
    raw = os.environ.get(ENV_EXTRA_BODY)
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        log.debug("ignoring malformed %s (%s)", ENV_EXTRA_BODY, exc)
        return {}
    if not isinstance(parsed, dict):
        log.debug("%s must be a JSON object, got %s", ENV_EXTRA_BODY, type(parsed).__name__)
        return {}
    return parsed


def _postprocess(raw: str, cwd: str | Path | None) -> str:
    """Shared cleanup: strip decoration, bound length, glue on the Jira ticket."""
    text = raw.strip(" \t\n\"'`")
    if text.endswith("."):
        text = text[:-1].rstrip()
    if not text:
        return ""

    # Jira-ticket prefix is a single source of truth — the model is told
    # nothing about tickets, and we glue the key on here so the column
    # rendering and TTS read-out share the same convention. Strip any
    # accidental duplicate prefix the model might have invented.
    ticket = ticket_for_cwd(cwd) if cwd is not None else None
    if ticket:
        dup = re.match(rf"^{re.escape(ticket)}\s*[:\-]\s*", text)
        if dup:
            text = text[dup.end() :]
        prefix = f"{ticket}: "
        budget = max(8, MAX_LENGTH - len(prefix))
        if len(text) > budget:
            text = text[: budget - 1].rstrip() + "…"
        return prefix + text

    if len(text) > MAX_LENGTH:
        text = text[: MAX_LENGTH - 1].rstrip() + "…"
    return text
