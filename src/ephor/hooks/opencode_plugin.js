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

export const ephor = async ({ $, directory }) => {
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

  return {
    // Session lifecycle + permission prompts arrive on the event bus.
    event: async ({ event }) => {
      const p = event?.properties ?? {};
      // sessionID lives directly on most events; session.* lifecycle events
      // nest the Session under `info`.
      const sid = p.sessionID ?? p.info?.id ?? p.session?.id;
      switch (event?.type) {
        case "session.created":
          return emit(sid, "SessionStart");
        case "session.deleted":
          return emit(sid, "SessionEnd");
        case "session.idle":
          return emit(sid, "Stop"); // turn finished → IDLE (+ speak-back)
        case "session.status": {
          const t = p.status?.type;
          // "busy"/"retry" → WORKING without bumping tool_count (PostToolUse is
          // the handler's WORKING-no-increment event); "idle" → Stop.
          if (t === "busy" || t === "retry") return emit(sid, "PostToolUse");
          if (t === "idle") return emit(sid, "Stop");
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
      emit(input?.sessionID, "PreToolUse", { tool_name: input?.tool }),
    "tool.execute.after": async (input) =>
      emit(input?.sessionID, "PostToolUse", { tool_name: input?.tool }),
  };
};
