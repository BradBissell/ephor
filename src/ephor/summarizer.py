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

# The model emits this sentinel when the excerpt has no real task to describe
# (a greeting, small talk, an empty reply). We map it — and any all-punctuation
# reply — back to "" so the UI shows "—" instead of a hollow line.
_NO_TASK = "—"  # em dash

# Dash/bullet glyphs a model might emit as a stand-in for "no task". Written as
# escapes so they don't trip ruff's ambiguous-unicode check in a strip set.
_DASHES = "\u2014\u2013\u00b7\u2022"  # em, en dash, middot, bullet

# Defense-in-depth: even with the instruction below, a small/local model handed
# a thin excerpt sometimes narrates the *act of summarizing* ("I'm summarizing
# the transcript…") or announces there's nothing to do. Drop those — they are
# never a real task summary. Kept deliberately tight to avoid eating genuine
# summaries that merely mention a "conversation" or "session" feature.
_META_RE = re.compile(
    r"\b(summariz\w*\s+(the\s+)?(transcript|conversation|reply|response|session|chat)"
    r"|no\s+(substantive\s+|real\s+)?(task|activity|content|work)"
    r"|(nothing|unable)\s+to\s+summariz)",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "You write a one-line status for a terminal coding-agent session, from an "
    "excerpt of its conversation. Output ONE sentence "
    f"(max {MAX_LENGTH} characters) naming the concrete coding task the agent "
    "is working on — e.g. 'Add retry logic to the upload client'. "
    "Output the sentence only: no preamble, no quotes, no trailing period, and "
    "never narrate the act of summarizing. "
    "If the excerpt is empty or has no real coding task (a greeting, small "
    f"talk, or a bare acknowledgment), output exactly: {_NO_TASK}"
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
    raw = _run_backend(_format_for_prompt(messages))
    return _postprocess(raw, cwd) if raw else ""


def summarize_text(text: str, cwd: str | Path | None = None, *, prompt: str = "") -> str:
    """Condense a captured reply (and, if known, the user prompt) into one line.

    Provider-agnostic counterpart to summarize_transcript: used for agents whose
    reply text is captured at turn-end (from an event payload field or a plugin)
    rather than a Claude-format transcript file. When ``prompt`` is supplied (the
    latest user turn, from the session's ``last_summary``) it is sent alongside
    the reply as a ``USER:``/``ASSISTANT:`` pair — the task context is what lets
    the model produce a real summary instead of narrating a lone stray sentence.
    Same backend + ticket-prefix + truncation rules. "" on empty/failure.
    """
    reply = (text or "").strip()
    prompt = (prompt or "").strip()
    if not reply and not prompt:
        return ""
    parts: list[str] = []
    if prompt:
        parts.append(f"USER: {prompt}")
    if reply:
        parts.append(f"ASSISTANT: {reply}")
    raw = _run_backend("\n\n".join(parts))
    return _postprocess(raw, cwd) if raw else ""


# Google Antigravity (agy) writes each conversation's transcript as JSONL under
# its "brain" dir, keyed by the same conversationId ephor uses as the session
# id. Unlike every other agent, agy's Stop hook hands us no transcript path and
# fires no user-prompt event, so its captured reply is empty — we resolve and
# read the transcript here instead. Override the root via env for tests.
ENV_AGY_BRAIN = "EPHOR_AGY_BRAIN_DIR"
_SAFE_SID_RE = re.compile(r"[A-Za-z0-9_-]+")
# agy wraps the real user prompt in <USER_REQUEST>…</USER_REQUEST>, surrounded
# by <ADDITIONAL_METADATA>/<USER_SETTINGS_CHANGE> blocks that are noise here.
_AGY_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


def _agy_brain_dir() -> Path:
    override = os.environ.get(ENV_AGY_BRAIN)
    if override:
        return Path(override)
    return Path.home() / ".gemini" / "antigravity-cli" / "brain"


def _agy_transcript_path(sid: str) -> Path:
    return _agy_brain_dir() / sid / ".system_generated" / "logs" / "transcript.jsonl"


def _agy_user_text(content: str) -> str:
    m = _AGY_REQUEST_RE.search(content)
    return (m.group(1) if m else content).strip()


def _extract_agy_messages(path: Path) -> list[dict[str, str]]:
    """Parse an agy transcript.jsonl into user/assistant text pairs.

    Lines look like ``{step_index, source, type, status, content}``. User turns
    are ``type == "USER_INPUT"`` (content wrapped in <USER_REQUEST>); the model's
    *prose* is ``type == "PLANNER_RESPONSE"``. agy's other MODEL step types
    (RUN_COMMAND, VIEW_FILE, GREP_SEARCH, LIST_DIRECTORY, GENERIC) are tool
    calls/results whose content is timestamped command output — pure noise for a
    one-line summary, so they're dropped, mirroring the tool-call filtering the
    Claude reader does. SYSTEM lines (checkpoints, history markers) are skipped
    too.
    """
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []

    messages: list[dict[str, str]] = []
    for raw in lines[-RECENT_TURNS * 6 :]:  # 6x: absorb the interleaved tool steps
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if entry.get("type") == "USER_INPUT":
            text = _agy_user_text(content)
            if text:
                messages.append({"role": "user", "content": text})
        elif entry.get("source") == "MODEL" and entry.get("type") == "PLANNER_RESPONSE":
            messages.append({"role": "assistant", "content": content.strip()})

    if len(messages) > RECENT_TURNS:
        messages = messages[-RECENT_TURNS:]
    return messages


def summarize_agy(sid: str, cwd: str | Path | None = None) -> str:
    """One-line summary for a Google Antigravity (agy) session.

    agy hands the hook no transcript path, so we locate its brain transcript by
    ``sid`` (== conversationId) and summarize it with the same rich, multi-turn
    context the Claude path enjoys. "" if the id is unsafe, the transcript is
    missing/unreadable, or the backend fails.
    """
    if not sid or not _SAFE_SID_RE.fullmatch(sid):
        return ""
    messages = _extract_agy_messages(_agy_transcript_path(sid))
    if not messages:
        return ""
    raw = _run_backend(_format_for_prompt(messages))
    return _postprocess(raw, cwd) if raw else ""


def _run_backend(prompt_text: str) -> str:
    """Send `prompt_text` to the configured backend; raw reply or "" on failure."""
    if _resolve_backend() == BACKEND_OPENAI:
        return _summarize_via_openai(prompt_text)
    return _summarize_via_claude(prompt_text)


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


def unavailable_reason() -> str:
    """A short, backend-aware explanation for why a summary came back empty,
    suitable for a UI toast. Reflects the *currently configured* backend so the
    message is actionable (and always points at the local-model option)."""
    backend = _resolve_backend()
    if backend == BACKEND_OPENAI:
        base = (os.environ.get(ENV_API_BASE) or "").strip()
        model = (os.environ.get(ENV_MODEL) or "").strip()
        if not model:
            return f"summary unavailable — set {ENV_MODEL} (endpoint {ENV_API_BASE} is set)"
        return (
            f"summary unavailable — check the model endpoint ({model} @ {base}); "
            "run `ephor doctor`. Reasoning models need "
            f'{ENV_EXTRA_BODY}=\'{{"chat_template_kwargs":{{"enable_thinking":false}}}}\''
        )
    if _claude_binary() is None:
        return (
            f"summary unavailable — `claude` not on PATH (or set {ENV_API_BASE} for a local model)"
        )
    return (
        "summary unavailable — log in to Claude Code / set ANTHROPIC_API_KEY, "
        f"or set {ENV_API_BASE} to use a local model"
    )


def probe_openai() -> tuple[bool, str]:
    """End-to-end health check for the openai backend, for `ephor doctor`.

    Returns (ok, detail). Three stages, each with an actionable message:
      1. ``GET <base>/models`` — reachability + the configured model is offered
         (lists what *is* offered on a mismatch).
      2. a real minimal ``/chat/completions`` — because a reachable endpoint
         with the right model can *still* yield empty summaries: a reasoning
         model burns the token budget "thinking" and returns null content
         unless EPHOR_SUMMARY_EXTRA_BODY disables it. Catching that here is the
         whole point — the /models check alone gives false confidence.
    """
    base = (os.environ.get(ENV_API_BASE) or "").strip()
    model = (os.environ.get(ENV_MODEL) or "").strip()
    if not base:
        return False, f"{ENV_API_BASE} not set"
    if not model:
        return False, f"{ENV_MODEL} not set"
    url = base.rstrip("/") + "/models"
    if not url.startswith(("http://", "https://")):
        return False, f"{ENV_API_BASE} must be an http(s) URL"

    headers = {}
    key = (os.environ.get(ENV_API_KEY) or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"cannot reach {url}: {exc}"

    try:
        ids = [
            str(m["id"])
            for m in json.loads(body).get("data", [])
            if isinstance(m, dict) and m.get("id")
        ]
    except (ValueError, TypeError):
        ids = []
    if ids and model not in ids:
        offered = ", ".join(ids)[:120]
        return False, f"reachable, but model {model!r} not offered (has: {offered})"

    # Stage 2: a real (tiny) completion — the definitive "can this config
    # actually produce a summary?" test.
    sample = _summarize_via_openai("USER: Reply with the single word: ready.")
    if sample:
        return True, f"reachable; {model} produced a test summary"
    return False, (
        f"reachable and {model} is offered, but a test completion came back empty — "
        f"if it's a reasoning model set {ENV_EXTRA_BODY}="
        '\'{"chat_template_kwargs":{"enable_thinking":false}}\''
        f" (or raise {ENV_MAX_TOKENS})"
    )


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
    # No-task sentinel, an all-punctuation reply, or a meta "I'm summarizing…"
    # narration → treat as "no summary" so the UI shows "—" rather than noise.
    if not text.strip("-.,: " + _DASHES) or _META_RE.search(text):
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
