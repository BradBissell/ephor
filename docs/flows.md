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
another tick holding the lock returns immediately, and a tick that finds the
snapshot less than 45 seconds old is **skipped** before it asks Jira
anything, so three tickers cost one search a minute. `ephor flows tick
--force` (and `ephor flows board --refresh`) tick anyway, and so does the
tick right after you approve a run.

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
   `EPHOR_JIRA_TOKEN` (an API token) in the shell that runs `ephor`. The
   names other tools use work too: `JIRA_URL`, `JIRA_EMAIL` (or
   `JIRA_USERNAME`) and `JIRA_API_TOKEN`. `ephor flows check` says which
   one is missing.
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
   the lanes. In the dashboard, press `B` for the `status` grouping.
5. **Add flows** (below), and run `ephor flows tick --dry-run` to see what
   they would do without launching anything.

## board.toml reference

Errors are collected rather than raised. `ephor flows check` lists all of
them. While `[board]` itself is valid the lanes still render, but **any**
error anywhere stops every flow from dispatching: a rule that was only half
understood is the one most likely to launch something you didn't ask for.

**Warnings** are a separate, milder level: things that are probably a
mistake but can't launch anything you didn't ask for. They never disable a
flow. `ephor flows check` and `ephor doctor` show them as `warn`. Today they
are:

- an `on_enter`, `on_exit` or `from` status that no column names (its
  tickets sit in `Other`, which is fine if you meant it, and a typo if not);
- with a snapshot to compare against (`flows check` only), a status no
  ticket on the board is in right now;
- `[flows] tmux_session` empty while the shell isn't inside tmux, so a
  tick run from there has no session to open flow windows in.

### `[board]`

| Key | Default | Meaning |
|---|---|---|
| `projects` | `$EPHOR_TICKET_PROJECTS` | Jira project keys. The search is always scoped to them (`project in (...) AND (<jql>)`), so the board can never become a search of the whole site. |
| `jql` | `assignee = currentUser() AND (statusCategory != Done OR updated >= -7d)` | Which tickets are on the board. A trailing `ORDER BY` is allowed. |
| `columns` | `["To Do", "In Progress", "Done"]` | The lanes, in order. Each entry is a status name, or a table such as `{ name = "Review", statuses = ["Peer Review", "QA"] }` to gather several statuses. Status names match case-insensitively. A status no column claims goes by its Jira status category: done → the last column, to-do → the first, anything else → an `Other` lane. |

A column table also takes three optional limits. They change how the lane is
drawn, and nothing else: no run is blocked or fired on them.

| Column key | Meaning |
|---|---|
| `wip` | How many tickets the lane should hold at most (a whole number ≥ 1). |
| `warn_after` | A ticket in the lane longer than this is drawn as ageing, e.g. `"36h"`. |
| `alert_after` | … and longer than this, as stuck, e.g. `"3d"`. Must not be shorter than `warn_after`. |

"How long a ticket has been in its status" is the tick's own clock from the
tick that saw it move. For a ticket ephor has not seen move (the first time
it is seen), it is Jira's `statuscategorychangedate`, which only moves when
the status *category* changes. That makes it an estimate: the earliest the
current status can have started.

`Other` and `Unticketed` are reserved lane names.

### `[repos]`

```toml
[repos]
DR = "~/projects/work/aim-myt"
```

This maps a project key to a checkout. Paths must be absolute (or start
with `~`): a tick runs from the dashboard, `ephor flows watch` or a timer,
each with its own working directory, and a relative path would resolve
differently in each. A flow runs in the ticket's own worktree when ephor
knows one (from its work record, and only a linked `git worktree` counts —
a session that ran in the repo root never makes the main checkout the
ticket's worktree). Otherwise it runs in the `[repos]` path for the
ticket's project. With neither, an agent flow is
`blocked: no repo` rather than started in some other checkout, and a
`command =` flow runs in `$HOME`. `on_pr` uses the same lookup to know
which repo to ask `gh` about.

### `[flows]`

| Key | Default | Meaning |
|---|---|---|
| `tmux_session` | `""` | Session to open flow windows in. `""` means tmux's current session. Set it when ticks run outside tmux (the systemd timer). |
| `max_active_agents` | `4` | Agent slots. A slot is a session still at work, started by a flow or by hand; one idle (or waiting at its prompt) longer than `settle` has finished and frees its slot. An approved agent run that finds every slot taken is **queued** (see [The agent queue](#the-agent-queue)). |
| `max_runs_per_hour` | `6` | Agent (`start =`) launches across all flows in the last hour. |
| `ticket_breaker` | `3` | Agent launches for one ticket in the last hour before that ticket's agent flows are blocked. |
| `max_commands_per_hour` | `60` | `command =` launches across all flows in the last hour. A separate budget: a notification script neither uses up nor waits behind the agent caps. |
| `max_event_age` | `"30m"` | A tick after a longer gap creates every run as `stale` and waiting for confirmation. |
| `confirm_ttl` | `"24h"` | A run that nobody approves within this time expires. So does an approved (or `auto`) run that a guard or a pause has held this long since it was approved (or created): a yes from days ago does not launch today. |
| `queue_ttl` | `"3d"` | How long an approved agent run may wait in the queue for a slot before it expires (`queued past queue_ttl`). Used instead of `confirm_ttl` while the run is queued. |
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
| `command` | one of `start` / `command` | Run any executable in a tmux window, e.g. a shell script or a Python program. |
| `assignee` | no (`"me"`) | `"me"` fires only for tickets assigned to you (compared by Jira accountId), `"any"` for every ticket on the board. |
| `mode` | no (`"confirm"`) | `"confirm"` waits for you to approve each run. `"auto"` dispatches on the tick that created it, and is **only allowed on `command =` flows**. |
| `provider` | no (`"claude"`) | `start =` only: which agent CLI to launch. |
| `worktree` | no (`false`) | `start =` only. `true` creates or reuses the ticket's worktree through `ephor start`, branching a new one off the repo's default branch (`origin/HEAD`, else `origin/main` or `origin/master`). `false` opens the agent in the ticket's existing worktree, or else in the repo root. |
| `from` | no | `on_enter` only. A status name or a list of them: fire only when the ticket came *from* one of these. A ticket that appears on the board already in the status came from nowhere, and never matches. |
| `jql` | no | An extra Jira condition the ticket must also meet, e.g. `"labels = needs-review"`. On a tick with candidate events for the rule, ephor runs **one** search, `key in (<candidates>) AND (<jql>)`, and drops the candidates it doesn't return. No `ORDER BY`. |
| `repo` | no | Run this flow in this checkout (absolute, or starting with `~`) instead of the ticket's worktree or `[repos]` entry. A `repo` that doesn't exist is `no repo`, never a fall back to another checkout. For `on_pr`, it is also where PRs are looked up for tickets that have no repo of their own. |

**Templates.** `start` and `command` may use these placeholders and nothing
else. Write `{{` and `}}` for literal braces. A value the event doesn't
carry renders as empty.

| Placeholder | Value |
|---|---|
| `{ticket}`, `{title}` | The ticket key and summary |
| `{status}` | The status the rule matched (for `on_pr` / `on_session`, the ticket's status when the event fired) |
| `{from_status}` | The status the ticket came from, for status triggers |
| `{pr_url}`, `{pr_number}` | The PR, for `on_pr` |
| `{repo}` | The checkout the flow runs in |
| `{session_id}` | The session, for `on_session` |

- A `start =` prompt must begin with literal text, such as `/review-pr
  {ticket}`, not with a placeholder or `-`. A ticket title can therefore
  never reach the agent CLI as a command-line option.
- In a `command =`, every placeholder must be a separate, unquoted word.
  ephor shell-quotes the value for you. `notify.sh {ticket} {title}` is
  fine; `"{title}"` or `--title={title}` are rejected. To use ticket text
  inside a larger string, use the environment instead:
  `"$EPHOR_FLOW_TITLE"`, `"$EPHOR_FLOW_STATUS"`, `"$EPHOR_TICKET"`.

**A `jql` filter that can't be checked.** If the filter's search doesn't
answer, nothing fires on it: the tick keeps the previous snapshot, so the
same moves are seen (and the filter asked) again on the next tick. If Jira
*rejects* the query (HTTP 400), asking again won't help: the candidates are
dropped, the tick's note says so, and `ephor flows check` runs every rule's
`jql` once so you find out before a move does.

**Why agent flows are confirm-only.** Your skills move tickets through Jira
with your own token, so ephor can't tell your move from an agent's. An
automatic agent launch triggered by an agent's own move is how one ticket
becomes six sessions.

## Triggers

| Trigger | Fires when | Generation (one run per…) | The run stays current while… |
|---|---|---|---|
| `on_enter = "Peer Review"` | a ticket's status becomes the named one, including a ticket that appears on the board already in it | `<status_id>-<seq>`: one visit to the status | the ticket is still on that visit |
| `on_exit = "In Progress"` | a ticket's status stops being the named one | `<status_id>-<seq>` of the visit that ended | the ticket hasn't moved again |
| `on_pr = "opened"` | a PR naming the ticket is open and the previous lookup (in the same repo) did not have it open. Fires once per PR number: a PR closed and reopened later does not fire again | `pr<number>`: one PR | the PR is still open |
| `on_pr = "merged"` | a PR naming the ticket is merged and the previous lookup (in the same repo) saw it **open**. A PR first seen already merged never fires | `pr<number>` | always (a merge is final) |
| `on_session = "died"` | a session bound to the ticket was alive at the previous tick and its agent process has now exited | the session id's first 12 characters | always |
| `on_session = "idle"` | a session bound to the ticket has been IDLE, or waiting for your input, with no further hook event for `settle`, and wasn't at the previous tick. `command=` only (see the launch guard below) | the session id's first 12 characters plus when the idle spell began: one run per idle spell | the session stays in that idle spell |

A run that stops being current before it launches is `expired`, with a
reason that says why (`ticket moved on (now QA)`, `PR #12 is no longer
open`, `flow changed since this run was created`).

Notes on the newer triggers:

- **on_pr** runs one `gh pr list --state all --limit 200` per repo (the
  ticket's worktree, or its `[repos]` checkout) and matches the board's
  tickets against it locally. A PR counts only if its branch or title names
  the ticket (or its body says `Closes DR-1` or similar). The same call
  fills in each PR's CI rollup, which the board shows. Lookups happen at
  most once every `pr_interval`, and cover tickets on the board that a
  rule's `assignee` filter accepts, plus Done tickets only while one of
  their PRs was last seen open. The first time ephor looks at a ticket,
  whatever PRs it already has are a **baseline** and fire nothing.
- **A different repo is a different question.** ephor records which repo
  each ticket's PRs were read from. When that changes (the ticket's
  worktree appears, or is removed), the new answer is a silent baseline.
  Without that, every PR the other repo happens to hold would look as if it
  had just opened or merged, and an `auto` flow on them would run with
  nobody watching.
- A repo with more than 200 PRs fills the listing. A ticket whose PR has
  aged off the end of it is asked about on its own (`gh pr list --search
  <KEY>`), and a PR that search turns up for the first time is a baseline,
  not an opening.
- **on_session** reads the state files the dashboard already keeps. A
  session you kill from the dashboard counts as died. An older session id
  left behind by `--resume`, whose process is still running, does not.
  `idle` fires on the crossing: a session that was already idle past
  `settle` when ephor first saw it is a baseline. The generation is the
  session plus the start of the idle spell, so a session that works, goes
  quiet, works again and goes quiet again fires once per quiet spell.
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
that records its exit status and tees its stdout and stderr to
`runs/<run_id>.log` (still showing them in the window; the output is a pipe,
not a terminal). A later tick turns the exit status into `done` (exit 0) or
`failed`; `ephor flows show <run>` prints the end of the log. A `start =`
flow is `done` when its agent window closes.

## Runs, approval and guards

```
awaiting_confirm ──approve──► claimed ──► launched ──► done | failed
      │                         │  ▲
      ├─deny──► denied          ▼  │ (retried while the event is current)
      └─ttl / event over──► expired      blocked
                                │
      event over, or flow edited ──► expired
      held past confirm_ttl ──► expired
      queued past queue_ttl ──► expired
      too many attempts ──► stuck
      stuck | failed | expired ──ephor flows retry──► awaiting_confirm
```

Before a claimed run launches, the guards are checked in this order:

1. the pause flag (`ephor flows pause`);
2. `command =` flows: `max_commands_per_hour`, and that is all;
3. agent (`start =`) flows: `max_runs_per_hour`, `ticket_breaker`,
   the **agent queue** (`max_active_agents`), and the **launch guard**. The guard refuses if a
   live session is already bound to the ticket (by a fact — a pin,
   `EPHOR_TICKET`, its branch — not a session that merely mentions the key),
   a session was launched on it in the last 10 minutes (a dashboard restart
   seeing existing sessions does not count), or its work record names a
   session that is still live. A session that has been idle (or waiting at
   its prompt) for longer than `settle` doesn't count as on the ticket: it
   has finished with it, and it frees its agent slot too. A
   session scan that fails blocks too: an unknown count is not zero;
4. an uncached re-read of the ticket from Jira. An `on_enter` run whose
   ticket has already left the status expires, and so does any
   `assignee = "me"` run whose ticket is no longer yours. A re-read that
   comes back without a status blocks the run (unknown, not moved).

Only a launch that actually opened a window counts toward the hourly caps.
An attempt that failed (tmux down, the session missing) costs nothing.

If a guard says "not now", the run is `blocked` with the reason, and every
tick retries it for as long as its event is current — up to `confirm_ttl`
from its approval (or, for an `auto` run, its creation), after which it
expires as `held past confirm_ttl`. A blocked run is logged once per guard,
not once per tick.

A launch that **fails** (tmux reports an error) is retried with an
exponential backoff: 1, 2, 4 and then 8 minutes after each failure. After 5
attempts, a quarter of an hour of tmux being down, the run is `stuck`. A
launch that never reports a window at all (the tick died mid-launch) is
retried after 10 minutes, within the same 5 attempts. Before any retry the
tick looks for the earlier attempt's window and exit status, and adopts
them rather than starting the flow twice. An agent flow is never relaunched
on a guess: if its earlier attempt left no window, the event log decides
whether that agent ran (`done`) or nobody can tell (`stuck`).

**Retrying a run by hand.** `ephor flows retry <run>` puts a `stuck`,
`failed` or `expired` run back to `awaiting_confirm`, with its attempts
reset and any old approval dropped. Approve it to launch it. The tick still
judges it: if its event is over (the ticket moved on, the PR closed) or its
flow has been edited since, it expires again on the next tick, with the
reason.

A run launches the flow that created it. If you edit a flow in
`board.toml` so that it runs something else (another template, provider,
`worktree` or `repo`, or `command =` changed to `start =`), its pending runs
expire (`flow changed since this run was created`) rather than launch, and
its waiting ones expire before you can approve them. A flow that now needs
a confirmation only launches runs somebody approved.

**Pause holds runs; it does not drop them.** Events that arrive while
flows are paused still create runs, parked as `blocked: paused`.
`ephor flows resume` says how many are held; they dispatch on the next
tick, except any held longer than `confirm_ttl`, which expire.

The launch guard is what makes `on_pr = "opened"` → `/review-pr` work as a
hand-off. The implementing session opens the PR and is usually still alive,
so the approved review waits as `blocked` until that session ends, or has
sat idle at its prompt for longer than `settle`, and then starts.

An `on_session = "idle"` flow must use `command=`, and `board.toml`
validation rejects one with `start=`. Its run lasts only as long as the idle
spell does, and it expires the moment the session does anything else, so a
second agent started on it would be working alongside the first. To act on
an idle agent, use a `command=` flow (one that messages the session, say),
or use `on_session = "died"` to start a fresh one after it ends.

### The agent queue

`max_active_agents` is a number of agent slots, and a slot is a session that
is still at work. An approved agent run that finds every slot taken is
`queued` (badge `⧗`, shown as `queued` in the flows screen) rather than
blocked:

- **First approved, first launched.** Held runs are tried in the order they
  were approved (created, for an `auto` run), so when a slot frees, the run
  that has waited longest gets it. The rest stay queued: an agent launched
  this tick takes a slot straight away.
- **Idle sessions free their slot.** A session idle (or waiting at its
  prompt) longer than `settle` doesn't take a slot. An agent that opened its
  PR and went quiet stops holding the queue without anybody closing its
  window. A session waiting on a permission prompt still takes one.
- **It waits longer.** A queued run expires after `queue_ttl` (default 3 days)
  from its approval, not `confirm_ttl`. Its event must still be current: a
  queued review whose ticket leaves Peer Review expires as usual.
- **Other guards come first.** The hourly caps and the per-ticket breaker are
  checked before the queue. The launch guard is checked after it, so a run at
  the head of the queue that the launch guard refuses doesn't take the slot.
  The next run in line does.

The flows screen (`f`) shows each queued run's place (`queued #2 of 3`) in
its detail panel. The header and lane lines count queued runs as `⧗N`, apart
from `⊘` blocked ones.

## Commands and keys

| Command | Does |
|---|---|
| `ephor flows check` | Validate board.toml, print the search JQL, every flow and every warning, probe Jira `/myself`, run the board search once, and check each flow's `jql` |
| `ephor flows board [--refresh]` | Print the lanes from the last snapshot (`--refresh` runs a tick first, even on a fresh snapshot) |
| `ephor flows tick [--dry-run] [--force]` | Run one tick. `--dry-run` reads everything and writes and launches nothing. `--force` ticks even when the snapshot is under 45 seconds old |
| `ephor flows watch [--interval 60]` | Tick every interval until Ctrl-C, printing only when something happened |
| `ephor flows ls [--all] [--ticket K] [--flow F]` | Runs, newest first (finished ones for a day, or all with `--all`), optionally for one ticket or flow |
| `ephor flows show <run>` | One run in full: state, reason, attempts, every timestamp, its payload, and the last lines of its output (`command =` flows) |
| `ephor flows explain <KEY> [flow]` | For each flow (or the one named), whether it would fire on the ticket now, and if not, the first gate that stops it: `not on board`, `adopting`, `assignee`, `status match`, `no event`, `run exists` (with its state), `jql`, `no repo`, `PR lookup not due`, `paused`, `caps`, `launch guard`. Uses the tick's own checks against the last snapshot. Read-only: it writes and launches nothing |
| `ephor flows approve <run>` / `deny <run>` | Answer a run that is awaiting confirmation. `<run>` may be a unique prefix, but the ticket key must be typed whole |
| `ephor flows retry <run>` | Put a stuck, failed or expired run back to awaiting confirmation |
| `ephor flows pause` / `resume` | Stop / restart all dispatching. `resume` says how many held runs will go |

In the dashboard, `B` switches to the `status` grouping (and back); `g` also cycles to it, and the choice is remembered. Tickets with no live
session appear as ticket rows, and rows show a badge for their ticket's
most pressing run:

| Badge | Meaning |
|---|---|
| `⏸ review?` | awaiting your confirmation (`(stale)` after a gap) |
| `… review` | claimed, about to launch |
| `⧗ review` | queued for a free agent slot |
| `⊘ review` | blocked by a guard |
| `▶ review` | launched |
| `⚠ review stuck` | the tick gave up (shown for a day); `ephor flows retry` re-arms it |

The header counts runs awaiting confirmation, blocked and stuck. `f` opens
the flows screen, where `y` approves, `N` pressed twice denies, and `escape`
closes; a panel shows exactly what the highlighted run will start. Approving
also starts a tick straight away. `?` lists every key and badge.

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

### A `command =` flow that starts agents itself

To start an agent, use a `start =` flow: it is confirm-only, queued
for an agent slot (`max_active_agents`) and the agent caps, and held by the launch
guard. A `command =` flow that launches agents itself (say, a script that
runs `ephor start`) bypasses all of those, so give it `mode = "confirm"`.

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
`[flows] tmux_session` to a session that exists (for example `main`);
`ephor flows check` warns when it is empty outside tmux. A flow whose
window can't be opened, because the tmux server isn't running or the
session doesn't exist, stays `blocked` and is retried with a backoff (1, 2,
4, 8 minutes) until it is `stuck`.
`PATH` has to reach `ephor`, `gh`, `tmux` and your agent CLIs.

## Troubleshooting

- **The header says `FLOWS ✗`.** The dashboard could not refresh the board
  or run its flows: board.toml has errors, Jira is not configured or
  rejected the search, or the snapshot has not been refreshed for a while.
  The text beside it says which; `ephor flows check` has the detail.
- **Nothing fires.** Run `ephor flows explain <KEY>`: it names, for each
  flow, the first thing that stops it. On the first tick, and the first
  tick after you add a rule, the rule adopts the board (`adopted …`), and
  only later moves fire it. Check `ephor flows check` for errors: any error
  stops all dispatch.
- **A run is blocked.** `ephor flows ls` shows the reason, `ephor flows show
  <run>` everything else. A `launch guard` reason means an agent is already
  on the ticket; `no repo` means you need a `[repos]` entry (or the flow's
  `repo`).
- **The board search fails.** The message carries Jira's HTTP status and
  its own words (`HTTP 401` is the token, `HTTP 400` usually the JQL). On
  `HTTP 429` ephor honours Jira's `Retry-After`: ticks don't search again
  until it runs out, even with `--force`.
- **A `command =` flow failed.** `ephor flows show <run>` prints the end of
  its output; the whole of it is in `runs/<run_id>.log`.
- **on_pr is silent.** Lookups need a repo (worktree or `[repos]`) and a
  working `gh auth status`. They run at most once every `pr_interval`, and a
  PR that was already open the first time ephor looked is a baseline.
- **Stop everything now:** `ephor flows pause`.
- **What happened to a ticket:** `ephor log DR-8222` shows its
  `ticket_transitioned` events and every step of its runs: `flow_created`,
  `flow_approved`, `flow_dispatched` (a window opened), `flow_blocked`,
  `flow_finished` (done or failed), `flow_expired`, `flow_stuck`,
  `flow_cancelled` (denied) and `flow_rearmed` (`ephor flows retry`).

## Where state lives

Everything is under `$XDG_STATE_HOME/ephor/flows/` (normally
`~/.local/state/ephor/flows/`), and every file is 0600:

| Path | Holds |
|---|---|
| `snapshot.json` | The last tick's board (each ticket with when it entered its status, whether it is flagged, and where it came from), plus `rules_seen`, the cached accountId and Flagged field id, the PR observation (`prs`, `pr_repos`, `pr_checked_at`) and the session observation (`sessions_seen`). Written last, by atomic rename. A ticket missing from every search for an hour is dropped. |
| `runs/<run_id>.json` | One file per flow firing. |
| `runs/<run_id>.exit` | The exit status of a `command =` flow. |
| `runs/<run_id>.log` | The output of a `command =` flow, appended per attempt. |
| `backoff.json` | Present while a Jira `Retry-After` is in force. |
| `payloads/<run_id>.json` | What the flow was told. |
| `paused` | Present while flows are paused. |
| `tick.lock`, `runs/.lock` | Locks; see [architecture.md](architecture.md#who-writes-what). |

Finished runs are pruned after 30 days, with their payload, exit and log
files. Their history stays in the event log.
