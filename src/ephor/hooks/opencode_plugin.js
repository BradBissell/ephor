// ephor — OpenCode session-monitor plugin.
//
// Auto-generated and installed by `ephor init --provider opencode` into
// ~/.config/opencode/plugins/ephor.js. OpenCode has no shell hooks, so this
// plugin bridges the gap: it subscribes to OpenCode's bus events + tool hooks,
// translates them into ephor's canonical event JSON, and pipes that to the
// SAME event_handler.sh every other agent uses (tagged EPHOR_PROVIDER=opencode).
// So OpenCode sessions land in ~/.local/state/ephor/sessions/ with the same
// schema, tmux mapping, and locking as Claude/Gemini/Codex/Grok.
//
// The HANDLER path is injected by the installer (it's ephor's package path).
// Fail-open everywhere: a monitoring hook must never disrupt OpenCode.

const HANDLER = "__EPHOR_HANDLER_PATH__";

export const ephor = async ({ $, directory, client }) => {
  // Pipe one canonical event to event_handler.sh via Bun's shell. stdin carries
  // the JSON; EPHOR_PROVIDER selects the opencode dialect in the handler.
  const fire = async (payload) => {
    if (!payload.session_id) return;
    const json = JSON.stringify({ cwd: directory, ...payload });
    try {
      await $`printf '%s' ${json} | ${HANDLER}`
        .env({ ...process.env, EPHOR_PROVIDER: "opencode" })
        .quiet()
        .nothrow();
    } catch {
      /* fail-open — never let monitoring break the session */
    }
  };

  const emit = (session_id, hook_event_name, extra = {}) =>
    fire({ session_id, hook_event_name, ...extra });

  // Reply text for speak-back (full/summary TTS). Fetch the session's messages
  // via the in-process SDK client (NOT `opencode export` — spawning that during
  // an active session deadlocks) and pull the last assistant message's text
  // parts. Best-effort — returns "" on any failure.
  const lastAssistantText = async (sid) => {
    if (!sid) return "";
    try {
      const res = await client.session.messages({ path: { id: sid } });
      const msgs = res?.data ?? res ?? [];
      for (let i = msgs.length - 1; i >= 0; i--) {
        const m = msgs[i];
        if ((m?.info?.role ?? m?.role) === "assistant") {
          const txt = (m?.parts ?? [])
            .filter((p) => p?.type === "text" && typeof p.text === "string")
            .map((p) => p.text)
            .join("")
            .trim();
          if (txt) return txt.slice(0, 4000);
        }
      }
    } catch {
      /* fail-open */
    }
    return "";
  };

  // Turn end can surface as both `session.idle` and `session.status:idle`.
  // Fetch the reply (an `opencode export`) only once per idle transition; a
  // repeat idle emits a plain Stop (no reply → no duplicate speak-back). The
  // set is cleared whenever the session becomes active again.
  const idled = new Set();
  const onIdle = async (sid) => {
    if (!sid) return;
    if (idled.has(sid)) return emit(sid, "Stop");
    idled.add(sid);
    const reply = await lastAssistantText(sid);
    return emit(sid, "Stop", reply ? { reply_text: reply } : {});
  };
  const onActive = (sid, hook_event_name, extra) => {
    if (sid) idled.delete(sid);
    return emit(sid, hook_event_name, extra);
  };

  return {
    // Session lifecycle + permission prompts arrive on the event bus.
    event: async ({ event }) => {
      const p = event?.properties ?? {};
      // sessionID lives directly on most events; session.* lifecycle events
      // nest the Session under `info`.
      const sid = p.sessionID ?? p.info?.id ?? p.session?.id;
      switch (event?.type) {
        case "session.created":
          return onActive(sid, "SessionStart");
        case "session.deleted":
          return emit(sid, "SessionEnd");
        case "session.idle":
          return onIdle(sid);
        case "session.status": {
          const t = p.status?.type;
          // "busy"/"retry" → WORKING without bumping tool_count (PostToolUse is
          // the handler's WORKING-no-increment event); "idle" → Stop (+ reply).
          if (t === "busy" || t === "retry") return onActive(sid, "PostToolUse");
          if (t === "idle") return onIdle(sid);
          return;
        }
        case "session.error":
          return emit(sid, "PostToolUseFailure", { error: "session.error" });
        // Permission prompt raised → WAITING_PERMISSION. Names vary by version;
        // accept both. `permission.replied` means it's resolved → back to WORKING.
        case "permission.updated":
        case "permission.asked":
          return emit(sid, "PermissionRequest", {
            tool_name: p.type ?? p.title ?? "permission",
          });
        case "permission.replied":
          return emit(sid, "PostToolUse");
        default:
          return;
      }
    },

    // Tool execution uses the first-class hooks (documented input shape) so
    // tool_count increments exactly once per tool call.
    "tool.execute.before": async (input) =>
      onActive(input?.sessionID, "PreToolUse", { tool_name: input?.tool }),
    "tool.execute.after": async (input) =>
      emit(input?.sessionID, "PostToolUse", { tool_name: input?.tool }),
  };
};
