"""Conversation summary for the dashboard summary column.

Shells out to the `claude` CLI in non-interactive mode (`claude -p`) — the
documented programmatic surface for Claude Code. The CLI handles
authentication against your subscription (Pro/Max/Team/Enterprise) without
any token plumbing, undocumented headers, or anthropic-SDK dependency on
our side.

  https://code.claude.com/docs/en/headless

We pipe a compact form of the recent transcript turns to stdin and ask
Claude for a single ≤70-char sentence. Output is requested as JSON
(`--output-format json`) and we read the `result` field.

Defensive: returns "" silently when the `claude` binary is absent, when
the subprocess fails for any reason, or when the JSON output is malformed.
The dashboard treats "" as "no summary yet" and shows "—".

CCO_INTERNAL=1 is set in the subprocess env so cco's own hook handler
short-circuits — otherwise every summary call would create a ghost
session in the dashboard.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from claude_orchestrator.jira import ticket_for_cwd

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

    When ``cwd`` is supplied and a Jira ticket key can be resolved (from
    the branch name or any directory in the path), the summary is
    prefixed with ``<KEY>: ``. The ticket-prefix consumes part of the
    MAX_LENGTH budget so the truncation rule still produces a single
    bounded line.

    All exceptions are caught and logged at DEBUG so the UI never sees a
    stack trace.
    """
    binary = _claude_binary()
    if binary is None:
        return ""

    messages = _extract_messages(path)
    if not messages:
        return ""

    prompt_text = _format_for_prompt(messages)

    # CCO_INTERNAL flags this invocation as a cco-internal call so the
    # event_handler.sh hook short-circuits and doesn't write a state file
    # for the summarizer subprocess.
    env = {**os.environ, "CCO_INTERNAL": "1"}

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
    if not isinstance(result, str):
        return ""

    text = result.strip(" \t\n\"'`")
    if text.endswith("."):
        text = text[:-1].rstrip()

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
