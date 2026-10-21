# The board and ticket flows

ephor can show your Jira board as **status lanes** in the dashboard and run
**flows**: small rules such as "when one of my tickets enters Peer Review,
offer to start `/review-pr` on it" or "when a PR for one of my tickets is
merged, run this script".

Both are opt-in. Nothing on this page does anything until
`~/.config/ephor/board.toml` exists and Jira is configured.

- [How it works](#how-it-works)
- [Setup](#setup)
- [board.toml reference](#boardtoml-reference)
- [Triggers](#triggers)
- [What a flow is given](#what-a-flow-is-given)
- [Runs, approval and guards](#runs-approval-and-guards)
- [Commands and keys](#commands-and-keys)
- [Examples](#examples)
- [Running flows when the dashboard is closed](#running-flows-when-the-dashboard-is-closed)
- [Troubleshooting](#troubleshooting)
- [Where state lives](#where-state-lives)

## How it works

There is no daemon. A **tick** looks at the board once, compares what it
sees with what the previous tick saw, acts on the differences, and exits:

```
take tick.lock → search Jira → diff against snapshot.json → create runs
              → tend existing runs → dispatch → write snapshot.json (last)
```

The dashboard runs a tick every 60 seconds while it is open. `ephor flows
watch` does the same in a terminal, and a systemd timer can run `ephor
flows tick` on a schedule. All three can run at once: a tick that finds
another tick holding the lock returns immediately.

A few rules make repeated ticks safe:

- **A run's file name is its identity.** When a flow fires, ephor creates
  `runs/<KEY>.<flow>.<generation>.json`, where the generation names the
  single event (this visit to a status, this PR, this session). If two ticks
  see the same event, the second create fails, so a flow never fires twice
  for one event.
- **Unknown never means unmet.** If Jira, `gh`, tmux or the session state
  can't be read, that part of the tick is left exactly as it was and
  nothing fires from it. A failed search writes nothing at all.
- **Adopt before acting.** The first tick, and the first tick after a rule
  is added or retargeted, records the board as it stands and fires nothing.
  A new rule therefore never fires on every ticket already sitting in its
  status.
- **After a gap, ask.** If the previous snapshot is older than
  `max_event_age` (the laptop was asleep, for example), changes are still
  noticed, but every run they create waits for you (`stale`), whatever the
  rule's `mode`.
- **No Jira writes.** ephor only reads Jira. Your skills and agents move
  tickets, as they do today.

## Setup

1. **Configure Jira.** Export `EPHOR_JIRA_URL`, `EPHOR_JIRA_EMAIL` and
   `EPHOR_JIRA_TOKEN` (an API token) in the shell that runs `ephor`.
2. **Write `~/.config/ephor/board.toml`.** Start from
   [`docs/examples/board.toml`](examples/board.toml). The smallest useful
   file is:

   ```toml
   [board]
   projects = ["DR"]
   columns = ["To Do", "In Progress", "Peer Review", "Done"]
   ```

3. **Check it:** `ephor flows check` validates the file, prints the JQL the
   board will run, and asks Jira who you are.
4. **Look at it:** `ephor flows board --refresh` runs one tick and prints
   the lanes. In the dashboard, press `g` until the grouping is `status`.
5. **Add flows** (below), and run `ephor flows tick --dry-run` to see what
   they would do without launching anything.

## board.toml reference

Errors are collected rather than raised. `ephor flows check` lists all of
them. While `[board]` itself is valid the lanes still render, but **any**
error anywhere stops every flow from dispatching: a rule that was only half
understood is the one most likely to launch something you didn't ask for.

### `[board]`

| Key | Default | Meaning |
|---|---|---|
| `projects` | `$EPHOR_TICKET_PROJECTS` | Jira project keys. The search is always scoped to them (`project in (...) AND (<jql>)`), so the board can never become a search of the whole site. |
| `jql` | `assignee = currentUser() AND (statusCategory != Done OR updated >= -7d)` | Which tickets are on the board. A trailing `ORDER BY` is allowed. |
| `columns` | `["To Do", "In Progress", "Done"]` | The lanes, in order. Each entry is a status name, or `{ name = "Review", statuses = ["Peer Review", "QA"] }` to gather several statuses. Status names match case-insensitively. Statuses that no column claims go to an `Other` lane. |

`Other` and `Unticketed` are reserved lane names.

### `[repos]`

```toml
[repos]
DR = "~/projects/work/aim-myt"
```

This maps a project key to a checkout. A flow runs in the ticket's own
worktree when ephor knows one (from its work record). Otherwise it runs in
the `[repos]` path for the ticket's project. With neither, an agent flow is
`blocked: no repo` rather than started in some other checkout, and a
`command =` flow runs in `$HOME`. `on_pr` uses the same lookup to know
which repo to ask `gh` about.

### `[flows]`

| Key | Default | Meaning |
|---|---|---|
| `tmux_session` | `""` | Session to open flow windows in. `""` means tmux's current session. Set it when ticks run outside tmux (the systemd timer). |
| `max_active_agents` | `4` | No agent flow launches while this many sessions (any session, not just flows) are alive. |
| `max_runs_per_hour` | `6` | Launches across all flows in the last hour. |
| `ticket_breaker` | `3` | Launches for one ticket in the last hour before that ticket's flows are blocked. |
| `max_event_age` | `"30m"` | A tick after a longer gap creates every run as `stale` and waiting for confirmation. |
| `confirm_ttl` | `"24h"` | A run that nobody approves within this time expires. |
| `pr_interval` | `"5m"` | Minimum spacing between rounds of `gh` lookups for `on_pr` rules. |
| `settle` | `"10m"` | How long a session must be continuously idle for `on_session = "idle"`. |

Durations are `"90s"`, `"30m"`, `"1h30m"`, `"2d"`, or a number of seconds.

### `[[flow]]`

Each flow is its own `[[flow]]` table.

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Unique, lowercase letters, digits and `-`. Part of the run id. |
| `on_enter` / `on_exit` / `on_pr` / `on_session` | exactly one | The trigger; see [Triggers](#triggers). |
| `start` | one of `start` / `command` | Open a coding-agent session with this prompt, e.g. `"/review-pr {ticket}"`. |
| `command` | one of `start` / `command` | Run any executable in a tmux window, e.g. a shell script or a LangGraph program. |
| `assignee` | no (`"me"`) | `"me"` fires only for tickets assigned to you (compared by Jira accountId), `"any"` for every ticket on the board. |
| `mode` | no (`"confirm"`) | `"confirm"` waits for you to approve each run. `"auto"` dispatches on the tick that created it, and is **only allowed on `command =` flows**. |
| `provider` | no (`"claude"`) | `start =` only: which agent CLI to launch. |
| `worktree` | no (`false`) | `start =` only. `true` creates or reuses the ticket's worktree through `ephor start`. `false` opens the agent in the ticket's existing worktree, or else in the repo root. |

**Templates.** `start` and `command` may use `{ticket}`, `{title}` and
`{status}` and nothing else. Write `{{` and `}}` for literal braces.

- A `start =` prompt must begin with literal text, such as `/review-pr
  {ticket}`, not with a placeholder or `-`. A ticket title can therefore
  never reach the agent CLI as a command-line option.
- In a `command =`, every placeholder must be a separate, unquoted word.
  ephor shell-quotes the value for you. `notify.sh {ticket} {title}` is
  fine; `"{title}"` or `--title={title}` are rejected. To use ticket text
  inside a larger string, use the environment instead:
  `"$EPHOR_FLOW_TITLE"`, `"$EPHOR_FLOW_STATUS"`, `"$EPHOR_TICKET"`.

**Why agent flows are confirm-only.** Your skills move tickets through Jira
with your own token, so ephor can't tell your move from an agent's. An
automatic agent launch triggered by an agent's own move is how one ticket
becomes six sessions.

## Triggers

| Trigger | Fires when | Generation (one run per…) | The run stays current while… |
|---|---|---|---|
| `on_enter = "Peer Review"` | a ticket's status becomes the named one, including a ticket that appears on the board already in it | `<status_id>-<seq>`: one visit to the status | the ticket is still on that visit |
| `on_exit = "In Progress"` | a ticket's status stops being the named one | `<status_id>-<seq>` of the visit that ended | the ticket hasn't moved again |
| `on_pr = "opened"` | a PR naming the ticket is open and the previous lookup did not have it open (new, or reopened) | `pr<number>`: one PR | the PR is still open |
| `on_pr = "merged"` | a PR naming the ticket is merged and was not merged at the previous lookup | `pr<number>` | always (a merge is final) |
| `on_session = "died"` | a session bound to the ticket was alive at the previous tick and its agent process has now exited | the session id's first 12 characters | always |
| `on_session = "idle"` | a session bound to the ticket has been IDLE, or waiting for your input, with no further hook event for `settle`, and wasn't at the previous tick. `command=` only (see the launch guard below) | the session id's first 12 characters | the session stays idle |

A run that stops being current before it launches is `expired` if it was
waiting for you, or `superseded` if it was approved or blocked.

Notes on the newer triggers:

- **on_pr** asks `gh pr list --search <KEY>` in the ticket's repo. A hit
  counts only if the PR's branch or title names the ticket (or its body
  says `Closes DR-1` or similar). The lookups happen at most once every
  `pr_interval`. They cover tickets on the board that a rule's `assignee`
  filter accepts, plus Done tickets only while one of their PRs was last
  seen open. The first time ephor looks at a ticket, whatever PRs it already
  has are a **baseline** and fire nothing. Each lookup costs a `gh` call,
  so a board of thirty tickets holds the tick for some seconds every
  `pr_interval`.
- **on_session** reads the state files the dashboard already keeps. A
  session you kill from the dashboard counts as died. An older session id
  left behind by `--resume`, whose process is still running, does not.
  `idle` fires on the crossing: a session that was already idle past
  `settle` when ephor first saw it is a baseline. Because the generation is
  the session, each rule fires at most once per session.
- Both triggers fire only for tickets the last search returned, and only
  while their rule's observation is running. If you remove every `on_pr`
  rule, the PR observation is cleared, so adding one back starts from a
  fresh baseline. The same applies to `on_session`, and to a `board.toml`
  with errors.

`{status}` in an `on_pr` or `on_session` flow is the ticket's status when
the event fired.

## What a flow is given

Every flow runs in its own **detached** tmux window named
`flow:<KEY>:<flow>`, so it never takes focus from the pane you are typing
in. Its environment includes:

| Variable | Value |
|---|---|
| `EPHOR_FLOW_RUN` | The run id, e.g. `DR-8222.review.10-3` |
| `EPHOR_FLOW_PAYLOAD` | Path to a 0600 JSON file describing the event (below) |
| `EPHOR_TICKET` | The ticket key. Agent sessions pick this up, so the dashboard binds them to the ticket. |
| `EPHOR_FLOW_TITLE`, `EPHOR_FLOW_STATUS` | `command =` only: the ticket title and status, safe to use inside quotes |

The payload:

```json
{
  "run_id": "DR-8222.ship.pr41",
  "flow": "ship",
  "ticket": "DR-8222",
  "title": "Add retry to the importer",
  "trigger": "on_pr",
  "event": "merged",
  "from_status": "",
  "to_status": "Peer Review",
  "status_id": "10",
  "assignee": "Ada Lovelace",
  "assignee_id": "5f…",
  "pr": {"number": 41, "url": "https://github.com/o/r/pull/41", "head_sha": "9c1e…", "state": "MERGED"},
  "session_id": "",
  "repo_path": "/home/you/projects/work/aim-myt"
}
```

`pr` is `null` except for `on_pr`; `session_id` is empty except for
`on_session`; `from_status` is set for status triggers.

A `command =` flow runs as `sh -c "$EPHOR_FLOW_COMMAND"` inside a wrapper
that records its exit status. A later tick turns that into `done` (exit 0)
or `failed`. A `start =` flow is `done` when its agent window closes.

## Runs, approval and guards

```
awaiting_confirm ──approve──► claimed ──► launched ──► done | failed
      │                         │  ▲
      ├─deny──► denied          ▼  │ (retried while the event is current)
      └─ttl / event over──► expired      blocked
                                │
      event over ──► superseded      too many attempts ──► stuck
```

Before a claimed run launches, the guards are checked in this order:

1. the pause flag (`ephor flows pause`);
2. `max_runs_per_hour`;
3. `ticket_breaker`;
4. agent flows only: `max_active_agents`, and the **launch guard**. The
   guard refuses if a live session is already bound to the ticket, a
   session was started on it in the last 10 minutes, or its work record was
   touched in the last 10 minutes;
5. an uncached re-read of the ticket from Jira. An `on_enter` run whose
   ticket has already left the status is superseded, and so is any
   `assignee = "me"` run whose ticket is no longer yours.

If a guard says "not now", the run is `blocked` with the reason, and every
tick retries it for as long as its event is current. A launch that never
reports a window is retried after 10 minutes, up to 3 attempts, and then
marked `stuck`.

The launch guard is what makes `on_pr = "opened"` → `/review-pr` work as a
hand-off. The implementing session opens the PR and is usually still alive,
so the approved review waits as `blocked` until that session ends (or you
end it), and then starts.

The same guard is why an `on_session = "idle"` flow must use `command=`,
and `board.toml` validation rejects one with `start=`. The idle session
that fired the event is itself a live session on the ticket, so the launch
guard would hold the run for as long as the session stays idle, and the run
is superseded as soon as it does anything else. It could never launch. To
act on an idle agent, use a `command=` flow (one that messages the session,
say), or use `on_session = "died"` to start a fresh one after it ends.

## Commands and keys

| Command | Does |
|---|---|
| `ephor flows check` | Validate board.toml, print the search JQL and every flow, probe Jira `/myself` |
| `ephor flows board [--refresh]` | Print the lanes from the last snapshot (`--refresh` runs a tick first) |
| `ephor flows tick [--dry-run]` | Run one tick. `--dry-run` reads everything and writes and launches nothing |
| `ephor flows watch [--interval 60]` | Tick every interval until Ctrl-C, printing only when something happened |
| `ephor flows ls [--all]` | Runs, newest first (finished ones for a day, or all with `--all`) |
| `ephor flows approve <run>` / `deny <run>` | Answer a run that is awaiting confirmation. `<run>` may be a unique prefix, but the ticket key must be typed whole |
| `ephor flows pause` / `resume` | Stop / restart all dispatching |

In the dashboard, `g` cycles to the `status` grouping. Tickets with no live
session appear as ticket rows, and rows show a badge for their ticket's
most pressing run:

| Badge | Meaning |
|---|---|
| `⏸ review?` | awaiting your confirmation (`(stale)` after a gap) |
| `… review` | claimed, about to launch |
| `⊘ review` | blocked by a guard |
| `▶ review` | launched |
| `⚠ review stuck` | gave up after 3 attempts (shown for a day) |

The header counts runs awaiting confirmation. `f` opens the flows screen,
where `y` approves, `n` denies and `escape` closes. Approving also starts a
tick straight away.

## Examples

### Peer Review → `/review-pr`

Offer a review session when a ticket you review lands in Peer Review. By
default the board holds only tickets assigned to you, so widen the JQL to
take in the tickets you review and accept any assignee. The reviewer field
is site-specific; use whatever your Jira calls it:

```toml
[board]
projects = ["DR"]
jql = '(assignee = currentUser() OR Reviewer = currentUser()) AND (statusCategory != Done OR updated >= -7d)'
columns = ["To Do", "In Progress", "Peer Review", "QA", "Done"]

[repos]
DR = "~/projects/work/aim-myt"

[[flow]]
name = "review"
on_enter = "Peer Review"
assignee = "any"
start = "/review-pr {ticket}"
worktree = false        # review from the repo root; no branch of your own needed
```

When a ticket moves into Peer Review, its row shows `⏸ review?`. Press `f`,
then `y`, and a detached window `flow:DR-8222:review` opens with
`claude "/review-pr DR-8222"` running in the repo.

### Implementation → review hand-off on PR open

```toml
[[flow]]
name = "self-review"
on_pr = "opened"
start = "/review-pr {ticket}"
```

This fires once per PR. Approve it, and it starts when the session that
opened the PR is no longer running (see the launch guard above).

### A script on merge, automatically

```toml
[[flow]]
name = "shipped"
on_pr = "merged"
command = "~/.config/ephor/flows/shipped.sh {ticket}"
mode = "auto"
```

```sh
#!/bin/sh
# ~/.config/ephor/flows/shipped.sh — chmod +x
set -eu
pr_url=$(jq -r '.pr.url' "$EPHOR_FLOW_PAYLOAD")
curl -fsS -d "$1 merged: $EPHOR_FLOW_TITLE — $pr_url" https://ntfy.sh/my-topic
```

### Tell me when an agent dies

```toml
[[flow]]
name = "died"
on_session = "died"
command = "notify-send ephor {ticket}"
mode = "auto"
```

### A LangGraph program as a `command =` flow

ephor doesn't import LangGraph and doesn't need it installed. A graph is
just a program that a `command =` flow runs. It reads its event from
`EPHOR_FLOW_PAYLOAD` and reports success through its exit status. This one
triages a newly opened PR and leaves a note in the ticket's worktree:

```toml
[[flow]]
name = "triage"
on_pr = "opened"
command = "uv run --with langgraph --with langgraph-checkpoint-sqlite ~/.config/ephor/flows/triage.py"
mode = "auto"
```

```python
# ~/.config/ephor/flows/triage.py
"""Triage a freshly opened PR: size it up, and flag it if it needs a careful review."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph


class State(TypedDict, total=False):
    ticket: str
    pr: int
    repo: str
    files: list[str]
    verdict: str


def load(state: State) -> State:
    diff = subprocess.run(
        ["gh", "pr", "diff", str(state["pr"]), "--name-only"],
        cwd=state["repo"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {"files": diff.stdout.split()}


def route(state: State) -> str:
    risky = any(f.startswith(("migrations/", "infra/")) for f in state["files"])
    return "careful" if risky or len(state["files"]) > 30 else "routine"


def careful(state: State) -> State:
    return {"verdict": f"careful review: {len(state['files'])} files, touches migrations/infra"}


def routine(state: State) -> State:
    return {"verdict": f"routine: {len(state['files'])} files"}


def main() -> int:
    payload = json.loads(Path(os.environ["EPHOR_FLOW_PAYLOAD"]).read_text())
    if not payload.get("pr") or not payload.get("repo_path"):
        print("no PR or repo in the payload", file=sys.stderr)
        return 2

    graph = StateGraph(State)
    graph.add_node("load", load)
    graph.add_node("careful", careful)
    graph.add_node("routine", routine)
    graph.add_edge(START, "load")
    graph.add_conditional_edges("load", route, ["careful", "routine"])
    graph.add_edge("careful", END)
    graph.add_edge("routine", END)

    # The run id is a stable thread id: the same event always maps to the
    # same checkpoint thread.
    store = Path("~/.local/state/ephor/flows/langgraph.sqlite").expanduser()
    with SqliteSaver.from_conn_string(str(store)) as checkpointer:
        app = graph.compile(checkpointer=checkpointer)
        result = app.invoke(
            {
                "ticket": payload["ticket"],
                "pr": payload["pr"]["number"],
                "repo": payload["repo_path"],
            },
            {"configurable": {"thread_id": payload["run_id"]}},
        )
    print(f"{payload['ticket']} PR #{payload['pr']['number']}: {result['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

The flow's tmux window stays open until the program exits, so you can
watch it. `ephor flows ls` then shows the run as `done` or `failed`, with
its exit code. A graph that should start an agent can run `ephor start
"$EPHOR_TICKET" --prompt "..."` as one of its steps.

## Running flows when the dashboard is closed

**In a tmux window (recommended).** This keeps your Jira credentials in
your shell environment:

```sh
tmux new-window -d -n ephor-flows 'ephor flows watch'
```

**As a systemd user timer.** This keeps flows running across reboots,
without anything open. Copy
[`docs/examples/systemd/ephor-flows.service`](examples/systemd/ephor-flows.service)
and [`ephor-flows.timer`](examples/systemd/ephor-flows.timer) to
`~/.config/systemd/user/`, then:

```sh
install -m 600 /dev/null ~/.config/ephor/flows.env
$EDITOR ~/.config/ephor/flows.env        # EPHOR_JIRA_URL=… EPHOR_JIRA_EMAIL=… EPHOR_JIRA_TOKEN=…
systemctl --user daemon-reload
systemctl --user enable --now ephor-flows.timer
journalctl --user -u ephor-flows.service -f
```

```ini
# ~/.config/systemd/user/ephor-flows.service
[Unit]
Description=ephor flows tick

[Service]
Type=oneshot
EnvironmentFile=%h/.config/ephor/flows.env
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=%h/.local/bin/ephor flows tick
```

```ini
# ~/.config/systemd/user/ephor-flows.timer
[Unit]
Description=Run ephor flows tick every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
```

A tick run from systemd has no tmux client of its own, so set
`[flows] tmux_session` to a session that exists (for example `main`). A
flow whose window can't be opened, because the tmux server isn't running
or the session doesn't exist, stays `blocked` and is retried on each tick.
`PATH` has to reach `ephor`, `gh`, `tmux` and your agent CLIs.

## Troubleshooting

- **Nothing fires.** Run `ephor flows tick --dry-run`. On the first tick,
  and the first tick after you add a rule, the rule adopts the board
  (`adopted …`), and only later moves fire it. Check `ephor flows check`
  for errors: any error stops all dispatch.
- **A run is blocked.** `ephor flows ls` shows the reason. A `launch guard`
  reason means an agent is already on the ticket; `no repo` means you need
  a `[repos]` entry.
- **on_pr is silent.** Lookups need a repo (worktree or `[repos]`) and a
  working `gh auth status`. They run at most once every `pr_interval`, and a
  PR that was already open the first time ephor looked is a baseline.
- **Stop everything now:** `ephor flows pause`.
- **What happened to a ticket:** `ephor log DR-8222` shows its
  `ticket_transitioned`, `flow_dispatched`, `flow_blocked`,
  `flow_finished` and `flow_cancelled` events.

## Where state lives

Everything is under `$XDG_STATE_HOME/ephor/flows/` (normally
`~/.local/state/ephor/flows/`), and every file is 0600:

| Path | Holds |
|---|---|
| `snapshot.json` | The last tick's board, plus `rules_seen`, the cached accountId, the PR observation (`prs`, `pr_checked_at`) and the session observation (`sessions_seen`). Written last, by atomic rename. |
| `runs/<run_id>.json` | One file per flow firing. |
| `runs/<run_id>.exit` | The exit status of a `command =` flow. |
| `payloads/<run_id>.json` | What the flow was told. |
| `paused` | Present while flows are paused. |
| `tick.lock`, `runs/.lock` | Locks; see [architecture.md](architecture.md#who-writes-what). |

Finished runs are pruned after 30 days. Their history stays in the event
log.
