#!/usr/bin/env bash
# ephor hook handler.
#
# Receives event JSON on stdin from a coding-agent CLI's hooks and writes
# per-session state files to $EPHOR_STATE_DIR (default:
# $XDG_STATE_HOME/ephor/sessions/). One handler serves every supported agent —
# Claude Code, Gemini CLI, Codex CLI and Grok CLI — because they all deliver
# {session_id, cwd, hook_event_name, ...} on stdin. The installer tells us which
# one fired via EPHOR_PROVIDER; we also accept each agent's event-name and
# field-name dialect (see the event branches below).
#
# Design contract:
#   * Fail OPEN. ANY failure → exit 0, no output, no error to the agent.
#     A buggy hook must NEVER block the user's coding session.
#   * Atomic writes. mktemp + fsync + mv-rename. No partial states.
#   * Per-session flock. 30 sessions firing in parallel must not lose updates.
#   * No git, no PPID-based heuristics, no shell injection of user data.
#     Every agent-controlled value flows through `jq --arg` / `--argjson`.
#   * Sub-15 ms p95 latency budget.
#
# Canonical events handled (+ per-provider aliases in the case statement):
#   SessionStart, SessionEnd, UserPromptSubmit (BeforeAgent)
#   PreToolUse (BeforeTool), PostToolUse (AfterTool), PostToolUseFailure
#   Notification, PermissionRequest, PermissionDenied
#   Stop / StopFailure / AfterAgent, SubagentStart, SubagentStop
#
# For PermissionRequest, the handler emits a JSON decision object on stdout
# IF a pending decision file exists at $EPHOR_PENDING_DIR/<sid>.json. Otherwise
# emits empty output (the agent shows its normal dialog).

# --- safety + fail-OPEN trap ----------------------------------------------
# Even with set -e, we want to GUARANTEE exit 0. A trap on ERR + EXIT enforces it.
set -u

ephor_exit_open() {
  # Fail OPEN: regardless of internal failures, never block claude.
  exit 0
}
trap ephor_exit_open ERR EXIT

# Skip ephor's own internal `claude` invocations (e.g. the TUI summarizer
# shells out to `claude -p` to compute one-line summaries — those calls
# would otherwise pollute the dashboard with ghost sessions).
[ -n "${EPHOR_INTERNAL:-}" ] && ephor_exit_open

# Defeat hook-environment hijack vectors.
unset BASH_ENV ENV PROMPT_COMMAND
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

# --- paths ----------------------------------------------------------------
STATE_DIR="${EPHOR_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/ephor/sessions}"
PENDING_DIR="${EPHOR_PENDING_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/ephor/pending}"
LOCK_DIR="${EPHOR_LOCK_DIR:-${XDG_RUNTIME_DIR:-/tmp}/ephor/locks}"
SCHEMA_VERSION=3

# Which coding-agent CLI fired this hook. Set by the installer in the
# registered command (EPHOR_PROVIDER=<name>). Empty when the handler is invoked
# directly (tests, manual runs) — recorded verbatim, never trusted for paths.
PROVIDER="${EPHOR_PROVIDER:-}"
case "$PROVIDER" in
  *[!a-z]*) PROVIDER="" ;;  # canonical names are lowercase ascii; reject anything else
esac
# Grok Build (xAI's official CLI) reads Claude-compatible hooks from
# ~/.claude/settings.json too, so ephor's claude hook fires inside grok
# sessions carrying EPHOR_PROVIDER=claude. Grok Build sets GROK_SESSION_ID /
# GROK_HOOK_EVENT on every hook process (Claude Code never does), so detect it
# and correct the provider label.
if [ -n "${GROK_HOOK_EVENT:-}${GROK_SESSION_ID:-}" ]; then
  PROVIDER="grok"
fi

mkdir -p "$STATE_DIR" "$PENDING_DIR" "$LOCK_DIR" 2>/dev/null || ephor_exit_open
chmod 0700 "$STATE_DIR" "$PENDING_DIR" "$LOCK_DIR" 2>/dev/null || true

# --- read input -----------------------------------------------------------
INPUT_JSON="$(cat)"
[ -z "$INPUT_JSON" ] && ephor_exit_open

# Probe for jq early — if missing, fail open silently.
command -v jq >/dev/null 2>&1 || ephor_exit_open

# session id: `.session_id` (claude/gemini/codex) or `.sessionId` (grok build / cursor)
SESSION_ID="$(printf '%s' "$INPUT_JSON" | jq -r '.session_id // .sessionId // empty')"
[ -z "$SESSION_ID" ] && ephor_exit_open

# Defensive: anchor session_id to safe characters before path use.
case "$SESSION_ID" in
  *[!a-zA-Z0-9_-]*) ephor_exit_open ;;
esac

# event name: `.hook_event_name` or grok build / cursor `.hookEventName`. Grok
# Build's values are snake_case (`session_start`, `pre_tool_use`, `stop`), so
# normalize any snake_case/lowercase name to PascalCase — idempotent for the
# PascalCase names the other agents already send.
EVENT_NAME="$(printf '%s' "$INPUT_JSON" | jq -r '.hook_event_name // .hookEventName // empty')"
[ -z "$EVENT_NAME" ] && ephor_exit_open
EVENT_NAME="$(printf '%s' "$EVENT_NAME" | awk -F_ \
  '{o=""; for (i=1;i<=NF;i++) o=o toupper(substr($i,1,1)) substr($i,2); print o}')"
[ -z "$EVENT_NAME" ] && ephor_exit_open

CWD="$(printf '%s' "$INPUT_JSON" | jq -r '.cwd // empty')"
[ -z "$CWD" ] && CWD="$PWD"
PROJECT_NAME="$(basename -- "$CWD")"

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

STATE_FILE="$STATE_DIR/$SESSION_ID.json"
LOCK_FILE="$LOCK_DIR/$SESSION_ID.lock"

# --- per-session lock -----------------------------------------------------
exec 9>"$LOCK_FILE" || ephor_exit_open
flock -w 2 9 || ephor_exit_open

# --- claude PID discovery -------------------------------------------------
# Walk parents from our own PID, skipping shells, until we hit the claude
# process that fired this hook. Recording its PID lets the dashboard match
# state files to running claudes unambiguously, even when several share a cwd.
find_claude_pid() {
  local pid="$$"
  local depth=0
  while [ "$depth" -lt 8 ]; do
    pid="$(awk '/^PPid:/{print $2; exit}' "/proc/$pid/status" 2>/dev/null)" || return 0
    [ -z "$pid" ] && return 0
    [ "$pid" -le 1 ] && return 0
    local comm
    comm="$(cat "/proc/$pid/comm" 2>/dev/null)" || comm=""
    case "$comm" in
      sh|bash|dash|zsh|fish|ksh|"") ;;        # keep walking past shells
      *) printf '%s' "$pid"; return 0 ;;       # first non-shell ancestor
    esac
    depth=$((depth + 1))
  done
}

CLAUDE_PID="$(find_claude_pid)"

# --- helpers --------------------------------------------------------------
read_field() {
  # $1 = field name, $2 = default. Reads from current state file.
  if [ -f "$STATE_FILE" ]; then
    jq -r --arg f "$1" --arg d "$2" '.[$f] // $d' "$STATE_FILE" 2>/dev/null || printf '%s' "$2"
  else
    printf '%s' "$2"
  fi
}

write_state() {
  # Receives the desired JSON document on stdin and writes it atomically.
  # Pipes through populate_tmux_mapping first so EVERY event refreshes the
  # tmux session/window/pane fields — without this, a stale pane_id can
  # outlive the pane it pointed to (claude --resume reused the session_id
  # in a different pane), and the dashboard's Enter-to-jump lands the user
  # in the wrong window.
  local tmp
  tmp="$(mktemp "$STATE_DIR/.tmp.XXXXXX")" || return
  trap 'rm -f "$tmp"' RETURN
  populate_tmux_mapping >"$tmp" || return
  # fsync the temp file's contents before rename — survives crashes.
  if command -v sync >/dev/null 2>&1; then
    sync -f "$tmp" 2>/dev/null || sync "$tmp" 2>/dev/null || true
  fi
  chmod 0600 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$STATE_FILE" 2>/dev/null || return
}

base_state() {
  # Emit the always-present base fields. Status + event-specific fields are
  # added by the event branch via jq merge.
  if [ -f "$STATE_FILE" ]; then
    jq -c \
      --arg ev "$EVENT_NAME" \
      --arg ts "$NOW" \
      --argjson sv "$SCHEMA_VERSION" \
      --arg cwd "$CWD" \
      --arg proj "$PROJECT_NAME" \
      --arg cpid "${CLAUDE_PID:-}" \
      --arg prov "$PROVIDER" \
      '. + {
         schema_version: $sv,
         cwd: $cwd,
         project_name: $proj,
         last_event: $ev,
         last_event_time: $ts,
         last_event_seq: ((.last_event_seq // 0) + 1),
         agent_pid: (if $cpid == "" then .agent_pid else ($cpid | tonumber) end),
         provider: (if $prov == "" then (.provider // "") else $prov end)
       }' "$STATE_FILE"
  else
    jq -nc \
      --arg sid "$SESSION_ID" \
      --arg cwd "$CWD" \
      --arg proj "$PROJECT_NAME" \
      --arg started "$NOW" \
      --arg ev "$EVENT_NAME" \
      --arg ts "$NOW" \
      --argjson sv "$SCHEMA_VERSION" \
      --arg cpid "${CLAUDE_PID:-}" \
      --arg prov "$PROVIDER" \
      '{
         schema_version: $sv,
         session_id: $sid,
         cwd: $cwd,
         project_name: $proj,
         started_at: $started,
         status: "IDLE",
         last_event: $ev,
         last_event_time: $ts,
         last_event_seq: 1,
         tool_count: 0,
         error_count: 0,
         tmux_session: null,
         tmux_window: null,
         tmux_pane: null,
         agent_pid: (if $cpid == "" then null else ($cpid | tonumber) end),
         notification: null,
         last_summary: "",
         provider: $prov
       }'
  fi
}

apply_status() {
  # Transition status. $1 = new status string, optional $2 = jq filter for extra fields.
  local new_status="$1"
  local extra_filter="${2:-}"
  local merged
  merged="$(base_state)" || return
  if [ -n "$extra_filter" ]; then
    merged="$(printf '%s' "$merged" | jq -c --arg s "$new_status" ". + {status: \$s} | $extra_filter")" || return
  else
    merged="$(printf '%s' "$merged" | jq -c --arg s "$new_status" '. + {status: $s}')" || return
  fi
  printf '%s\n' "$merged" | write_state
}

# --- tmux mapping (best-effort, non-blocking) -----------------------------
populate_tmux_mapping() {
  # Pass stdin through, optionally enriched with tmux session/window/pane
  # IDs when we're inside a tmux pane. ALWAYS produces stdout — never
  # silently drops the input, even when tmux is unreachable. Re-runs every
  # event so a stale pane_id can't outlive the pane it pointed to.
  if [ -z "${TMUX:-}" ] || ! command -v tmux >/dev/null 2>&1; then
    cat
    return 0
  fi
  # CRITICAL: target $TMUX_PANE explicitly. `tmux display-message -p`
  # without `-t` returns info for the *active* pane in the session, not
  # the pane the calling subprocess is running in — so hooks firing from
  # an inactive pane would record the WRONG pane_id, and the dashboard's
  # Enter-to-jump would land the user in whichever pane happened to be
  # active when the hook last fired.
  local target="${TMUX_PANE:-}"
  local info
  if [ -n "$target" ]; then
    info="$(tmux display-message -t "$target" -p $'#S\t#W\t#{pane_id}' 2>/dev/null)"
  else
    info="$(tmux display-message -p $'#S\t#W\t#{pane_id}' 2>/dev/null)"
  fi
  if [ -z "$info" ]; then
    cat
    return 0
  fi
  local s w p
  IFS=$'\t' read -r s w p <<<"$info"
  # Tag the pane with our session_id as a tmux per-pane user option. Lets
  # discover_panes match a pane → sid directly without walking process
  # trees or parsing /proc/cmdline. Idempotent; runs every event so a
  # pane that loses its tag (e.g. tmux server restart) self-heals.
  # Skip when SESSION_ID isn't bound (function called from a unit test
  # harness without the script's top-level vars).
  if [ -n "${SESSION_ID:-}" ]; then
    tmux set-option -p -t "$p" "@ephor_sid" "$SESSION_ID" >/dev/null 2>&1 || true
  fi
  jq -c \
    --arg s "$s" --arg w "$w" --arg p "$p" \
    '. + {tmux_session: $s, tmux_window: $w, tmux_pane: $p}'
}

# --- speech-event log (TTS karaoke bar) -----------------------------------
# Writes start/stop records to $EPHOR_SPEECH_LOG so the TUI's SpeechBar widget
# can mirror what the user's TTS engine is reading aloud and let them jump
# to the speaking session with one keystroke.
#
# Emission strategy:
#   * Stop:              spawn emit_speech_start in a backgrounded subshell
#                        (it polls the transcript for up to 3s — must NOT
#                        block the main hook's <15ms latency budget).
#   * UserPromptSubmit:  append a stop record inline (cheap; no transcript).
#
# Failure mode is silent — the main event_handler still exits 0 via the
# fail-OPEN trap regardless of whether speech logging succeeds.

speech_log_path() {
  if [ -n "${EPHOR_SPEECH_LOG:-}" ]; then
    printf '%s' "$EPHOR_SPEECH_LOG"
  else
    printf '%s/ephor/speech.jsonl' "${XDG_STATE_HOME:-$HOME/.local/state}"
  fi
}

speech_append_locked() {
  # Append $1 (a single JSON line, NO trailing newline expected) to the
  # speech log under flock, so concurrent Stop hooks from multiple sessions
  # never interleave half-lines.
  local payload="$1"
  local log lock dir
  log="$(speech_log_path)"
  lock="${log}.lock"
  dir="$(dirname "$log")"
  mkdir -p "$dir" 2>/dev/null || return 0
  ( flock -x 9; printf '%s\n' "$payload" >> "$log" ) 9>"$lock" 2>/dev/null || true
}

emit_speech_stop_event() {
  local sid="$1"
  [ -z "$sid" ] && return 0
  local ts record
  ts="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
  record="$(jq -nc --arg ts "$ts" --arg sid "$sid" \
    '{event: "stop", ts: $ts, session_id: $sid}' 2>/dev/null)" || return 0
  speech_append_locked "$record"
}

emit_speech_start_event() {
  # Runs in a backgrounded subshell. Re-parses the hook JSON ($1) rather
  # than relying on the parent's top-level vars, since the parent may have
  # exited by the time we get here.
  local hook_json="$1"
  local sid transcript cwd text
  sid="$(printf '%s' "$hook_json" | jq -r '.session_id // empty' 2>/dev/null)"
  transcript="$(printf '%s' "$hook_json" | jq -r '.transcript_path // empty' 2>/dev/null)"
  cwd="$(printf '%s' "$hook_json" | jq -r '.cwd // empty' 2>/dev/null)"
  [ -z "$sid" ] && return 0

  # Provider-agnostic reply capture. Prefer the reply text delivered directly
  # in the event payload — no transcript parsing needed:
  #   .reply_text            generic (the opencode plugin sends this)
  #   .prompt_response       gemini (AfterAgent)
  #   .last_assistant_message / .["last-assistant-message"]  codex
  # Only claude (and any agent that just gives a transcript path) falls through
  # to the Claude-format transcript poll below.
  text="$(printf '%s' "$hook_json" | jq -r '
    .reply_text // .prompt_response // .last_assistant_message
      // .["last-assistant-message"] // empty' 2>/dev/null)"

  if [ -z "$text" ]; then
    [ -z "$transcript" ] && return 0
    [ ! -f "$transcript" ] && return 0
    # Wait up to ~3s for the assistant message to flush to the transcript.
    local prev_size=-1 size
    for _ in 1 2 3 4 5 6; do
      size="$(stat -c %s "$transcript" 2>/dev/null || echo 0)"
      if [ "$size" = "$prev_size" ] && [ "$size" -gt 0 ]; then
        break
      fi
      prev_size="$size"
      sleep 0.5
    done
    text="$(jq -rs '
      [.[] | select(.type == "assistant")
           | .message.content[]?
           | select(.type == "text")
           | .text] | last // empty
    ' "$transcript" 2>/dev/null)"
  fi
  [ -z "$text" ] && return 0

  # Markdown cleanup + sentence split. Done in Python because the regex
  # surface is annoying in pure jq/awk and tts-speak-response already
  # uses the same cleanup recipe — keep them in sync.
  #
  # NOTE: text is passed via argv (sys.argv[3]) rather than stdin. We can't
  # `printf | python3 - args <<HEREDOC` because the heredoc binds to
  # python3 but stdin is already taken by the pipe — the script body
  # would be silently empty. Argv works because Linux ARG_MAX is ≥128 KB
  # and our text is capped at 1500 chars upstream.
  command -v python3 >/dev/null 2>&1 || return 0
  local payload
  payload="$(python3 -c '
import json, os, re, sys
from datetime import datetime, timezone

sid = sys.argv[1]
try:
    speed = float(sys.argv[2])
except ValueError:
    speed = 1.3
raw = sys.argv[3] if len(sys.argv) > 3 else ""
# Optional extras — passed positionally; absent values arrive as "".
# Carried in the start record so consumers (e.g. the ephor speech_player
# in summary-mode) can re-derive a brief without re-walking the
# transcript on disk. Old listeners ignore unknown fields.
transcript_path = sys.argv[4] if len(sys.argv) > 4 else ""
cwd = sys.argv[5] if len(sys.argv) > 5 else ""

# Cap on the *bar* text. To actually HEAR more, the user must also raise
# the cap inside ~/.claude/hooks/tts-speak-response (the kokoro pipeline
# truncates independently). Default 4000 — about 4 minutes at speed 1.3.
try:
    max_chars = max(1, int(os.environ.get("EPHOR_SPEECH_MAX_CHARS", "4000")))
except ValueError:
    max_chars = 4000

t = re.sub(r"```.*?```", " (code block) ", raw, flags=re.DOTALL)
t = re.sub(r"`[^`]+`", "", t)
t = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", t)
t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
t = re.sub(r"^#+\s*", "", t, flags=re.MULTILINE)
t = re.sub(r"^[\*\-]\s+", "", t, flags=re.MULTILINE)
t = re.sub(r"[*_~]", "", t)
t = re.sub(r"\s+", " ", t).strip()[:max_chars]
if not t:
    sys.exit(0)

parts = re.split(r"(?<=[.!?])\s+", t)
out, buf = [], ""
for p in parts:
    if not p:
        continue
    if buf and len(buf) + len(p) + 1 < 240:
        buf = (buf + " " + p).strip()
    else:
        if buf:
            out.append(buf)
        buf = p
if buf:
    out.append(buf)

ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
record = {
    "event": "start",
    "ts": ts,
    "session_id": sid,
    "text": t,
    "sentences": out,
    "speed": speed,
}
if transcript_path:
    record["transcript_path"] = transcript_path
if cwd:
    record["cwd"] = cwd
sys.stdout.write(json.dumps(record, separators=(",", ":")))
' "$sid" "${KOKORO_SPEED:-1.3}" "$text" "$transcript" "$cwd" 2>/dev/null)"
  [ -z "$payload" ] && return 0
  speech_append_locked "$payload"
}

# --- event branches -------------------------------------------------------
case "$EVENT_NAME" in
  SessionStart)
    base_state | jq -c '. + {status: "IDLE"}' | write_state
    ;;

  # Turn/session finished → IDLE. Names by provider:
  #   claude/codex/grok: Stop  · claude/grok: StopFailure, SessionEnd  · gemini: AfterAgent
  SessionEnd | Stop | StopFailure | AfterAgent)
    apply_status "IDLE"
    if [ "$EVENT_NAME" = "Stop" ] || [ "$EVENT_NAME" = "AfterAgent" ]; then
      # Background-spawn so the up-to-3s transcript poll doesn't blow the
      # main hook's <15ms latency budget. setsid + nohup + redirect detach
      # the subshell from this hook process group.
      ( emit_speech_start_event "$INPUT_JSON" ) </dev/null >/dev/null 2>&1 &
      disown 2>/dev/null || true
    fi
    ;;

  # Tool starting → WORKING. claude/codex/grok: PreToolUse · gemini: BeforeTool
  PreToolUse | BeforeTool)
    base_state \
      | jq -c '. + {status: "WORKING", tool_count: (.tool_count + 1), notification: null}' \
      | write_state
    ;;

  # Tool finished → still WORKING. claude/codex/grok: PostToolUse · gemini: AfterTool
  PostToolUse | AfterTool)
    base_state | jq -c '. + {status: "WORKING", notification: null}' | write_state
    ;;

  PostToolUseFailure)
    base_state \
      | jq -c '. + {status: "ERROR", error_count: (.error_count + 1)}' \
      | write_state
    ;;

  Notification)
    NOTIF_MSG="$(printf '%s' "$INPUT_JSON" | jq -r '.message // ""')"
    # Gemini tags permission prompts with notification_type:"ToolPermission";
    # other providers only carry a free-text message, so we sniff both.
    NOTIF_KIND="$(printf '%s' "$INPUT_JSON" | jq -r '.notification_type // ""')"
    case "$NOTIF_KIND" in
      *[Pp]ermission*)
        NOTIF_TYPE="permission"; STATUS="WAITING_PERMISSION" ;;
      *)
        case "$NOTIF_MSG" in
          *"waiting for your input"*|*"need your"*|*"clarif"*)
            NOTIF_TYPE="question"; STATUS="WAITING_ANSWER" ;;
          *"permission"*|*"approve"*|*"allow"*)
            NOTIF_TYPE="permission"; STATUS="WAITING_PERMISSION" ;;
          *)
            NOTIF_TYPE="question"; STATUS="WAITING_ANSWER" ;;
        esac ;;
    esac
    base_state \
      | jq -c \
          --arg s "$STATUS" \
          --arg nt "$NOTIF_TYPE" \
          '. + {
             status: $s,
             notification: {
               type: $nt,
               tool: null,
               redacted_summary: null
             }
           }' \
      | write_state
    ;;

  PermissionRequest)
    TOOL_NAME="$(printf '%s' "$INPUT_JSON" | jq -r '.tool_name // .toolName // "unknown"')"
    base_state \
      | jq -c \
          --arg tool "$TOOL_NAME" \
          '. + {
             status: "WAITING_PERMISSION",
             notification: {
               type: "permission",
               tool: $tool,
               redacted_summary: null
             }
           }' \
      | write_state

    # If a pending decision file exists, emit it as the hook return value.
    PENDING_FILE="$PENDING_DIR/$SESSION_ID.json"
    if [ -f "$PENDING_FILE" ]; then
      cat "$PENDING_FILE"
      rm -f "$PENDING_FILE" 2>/dev/null || true
    fi
    ;;

  PermissionDenied)
    base_state \
      | jq -c '. + {status: "ERROR", error_count: (.error_count + 1), notification: null}' \
      | write_state
    ;;

  # New user turn. claude/codex: UserPromptSubmit(.prompt) · grok:
  # UserPromptSubmit(.user_prompt) · gemini: BeforeAgent(.prompt).
  UserPromptSubmit | BeforeAgent)
    # Capture the latest user prompt as a 70-char "what is this session doing"
    # subline for the dashboard. tr/sed scrub newlines so the JSON stays one-line.
    PROMPT_RAW="$(printf '%s' "$INPUT_JSON" | jq -r '.prompt // .user_prompt // ""')"
    PROMPT_TRIM="$(printf '%s' "$PROMPT_RAW" | tr '\n\r\t' '   ' | head -c 70)"
    base_state \
      | jq -c --arg s "$PROMPT_TRIM" '. + {last_summary: $s}' \
      | write_state
    # New prompt cancels in-flight TTS via tts-stop in user's hooks; mirror
    # that intent in the speech log so the bar clears immediately instead
    # of waiting for the estimated-duration timeout to expire.
    emit_speech_stop_event "$SESSION_ID"
    ;;

  SubagentStart | SubagentStop)
    base_state | write_state
    ;;

  *)
    # Unknown event — record it but don't fabricate a status.
    base_state | write_state
    ;;
esac

# Trap will run ephor_exit_open → exit 0.
