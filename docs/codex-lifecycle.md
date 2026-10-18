# Codex lifecycle observation: design note

This note records what the Codex CLI's hooks provide, what they do not, and how Agent
Router should map them onto its existing session and agent lifecycle. It is the
output of TODO item 1 and the input to items 3 (transition table) and 4 (native Codex
observation). No product code changes accompany it.

**Pinned version:** `codex-cli 0.162.1`. Everything below was checked against that
version only. Expect drift in other versions; item 4 must re-verify on upgrade.

**Evidence markers:**
- **V** verified in a real local Codex session (hook payloads captured to a log),
- **D** stated in the official hook documentation (<https://learn.chatgpt.com/docs/hooks>),
- **S** read from the source at tag `rust-v0.162.1` (`codex-rs/hooks`, `codex-rs/core`),
- **I** inferred, not verified. Inferred behaviour must be tested in item 4 before it
  is relied on.

## Events

Codex 0.162.1 has twelve hook events. Hooks are enabled by default
(`features.hooks` is stable and on). [V, D]

| Event | Fires | Router use |
|---|---|---|
| `SessionStart` | Root session starts; `source` is `startup`, `resume`, `clear`, `compact`, or `fork`. Not fired for subagents. [V, D, S] | Session running. `resume` and `compact` keep the same session. |
| `UserPromptSubmit` | Before a prompt is sent. [V, D] | Session running; new turn. |
| `PreToolUse` | Before a supported tool runs. Carries `agent_id` when the call is made by a subagent. [V, D, S] | Session or agent activity; start tool correlation. |
| `PostToolUse` | After tool output, including non-zero shell exits. [V, D] | End tool correlation. |
| `PermissionRequest` | Before an approval prompt. Has no `tool_use_id`. [D, S] | Needs input. |
| `SubagentStart` | A subagent spawned by `spawn_agent` starts (or is forked). [V, D, S] | Agent running. |
| `SubagentStop` | A subagent's turn ends; fires per turn, not once per lifetime. [V for one turn; per-turn I from source] | Agent idle (turn complete), not stopped. See below. |
| `Stop` | A root turn ends normally. [V, D, S] | Session idle. |
| `Interrupt` | The user interrupts an active root turn. [D, S] | Session idle, reason "interrupted". |
| `SessionEnd` | The root thread ends (close, archive, delete, or idle shutdown). Root only. [V, D, S] | Session stopped; its agents follow Router's existing session-end rule. |
| `PreCompact` / `PostCompact` | Before and after context compaction. [D] | Ignored. |

Hosted tools (for example web search) do not fire tool hooks, and continuing a running
command with `write_stdin` does not re-fire `PreToolUse`. [D]

## Payload fields

Every payload has `session_id`, `transcript_path` (nullable), `cwd`, and
`hook_event_name`. Every event except `SessionEnd` has `model`. [V, S]

| Field | Present on | Meaning |
|---|---|---|
| `session_id` | all | Root thread ID, a UUIDv7, equal to `thread.started.thread_id`. Shared by the root and all its subagents. Stable across `resume`; `fork` creates a new one. [V, S] |
| `agent_id` | subagent start/stop; tool, permission, prompt, and compact events made by a subagent | The subagent's own thread ID. Absent on root events, which is how root and subagent events are told apart. [V, S] |
| `agent_type` | with `agent_id` | Role name (`default` if none). [V, S] |
| `turn_id` | all except session start and end | Per turn; a subagent has its own turn IDs. [V, S] |
| `tool_use_id` | tool events | Pairs `PreToolUse` with `PostToolUse`. [V] |
| `tool_name` | tool events | Shell appears as `Bash`; collaboration tools as `collaborationspawn_agent`, `collaborationwait_agent`. [V] |
| `source` | `SessionStart` | See the events table. [V, D] |
| `reason` | `SessionEnd` | Always `"other"` in 0.162.1. [V, S] |
| `permission_mode` | most events | `default` or `bypassPermissions` only. [V, S] |

No payload carries a timestamp. Router stamps events on receipt. [S]

Payloads also contain content: `prompt`, `tool_input`, `tool_response`, and
`last_assistant_message`. Per the Langfuse boundary, Router must not forward or retain
any of these. It forwards identifiers, lifecycle kind, `tool_name` as activity, `cwd`,
and `model` only.

## Identifier mapping

Codex fits Router's existing keys, `(host_id, source, session_id)` for sessions and
`(host_id, source, session_id, agent_id)` for agents, with `source = "codex"`.

| Router field | Claude Code | Codex |
|---|---|---|
| `source` | `claude` | `codex` |
| `session_id` | `session_id` | `session_id` (root thread ID) |
| `agent_id` | `agent_id` | `agent_id` (subagent thread ID) |
| `parent_session_id` | from hook when present | not applicable: subagents share the root `session_id` |
| `parent_agent_id` | from launch metadata | **unavailable** (see gaps) |
| tool correlation | `tool_use_id` | `tool_use_id` |

`turn_id` is used only for deduplication within a delivery and is not stored. The
deduplication key for a Codex event is `(session_id, agent_id, turn_id, tool_use_id,
hook_event_name)`. [I]

This namespace is shared with the Langfuse correlation bridge (TODO item 9): runtime is
`claude` or `codex`, and Langfuse traces join on `(runtime, session_id)`.

## Lifecycle mapping notes

- **Subagent turns, not lifetimes.** A subagent can run more than one turn (for
  example after `send_input`), and `SubagentStart` fires only at spawn or fork.
  `SubagentStop` therefore maps to *idle*; later tool events carrying the same
  `agent_id` mean it is running again. A subagent becomes *stopped* only when its root
  session ends. [I from source; verify in item 4]
- **Root `Stop` can be continued.** Another hook may block `Stop` and create a
  continuation prompt, so further events can follow a `Stop` in the same session. The
  transition table must treat this like a new turn, not a reopened terminal state. [D]
- **A tool start without an end is possible.** A tool that errors internally fired
  `PreToolUse` with no `PostToolUse` in the captured session. Router's existing tool
  deadline handles this; the tool must not stay active forever. [V]
- **Parent and subagent events interleave** in one Codex process. [V]

## Gaps (shown as unavailable, never inferred)

1. **Parent agent of nested subagents.** No payload names a subagent's parent agent,
   and the `spawn_agent` result returns only a task name, not the child `agent_id`.
   Nested subagents appear flat under their root session.
2. **Tool failure.** `PostToolUse` has no exit code or status; a failed command looks
   like any other result. Router does not parse output text to guess.
3. **Turn failure.** There is no `StopFailure` equivalent: a turn that errors fires no
   hook. A failed turn surfaces only through Router's existing staleness rule
   (`unknown`).
4. **Crashes.** A killed Codex process fires nothing; staleness applies. [I]
5. **Internal subagents** (review, compaction) fire no hooks and are invisible. [S]
6. **Hosted tools** fire no tool hooks. [D]

Router's `failed` agent state therefore never appears for Codex.

## Installation and prerequisites

- **Packaging.** Ship Router's Codex hooks as a Codex plugin (`hooks/hooks.json`,
  alongside the existing Claude plugin), registering the events above. [D]
- **Trust.** Every non-managed hook must be trusted against its current hash via
  Codex's `/hooks` command. Enabling a plugin does not trust its hooks, and a hook
  update changes its hash and requires re-trust. [D, S]
- **Fail open, in order.** Background (`async: true`) hooks cannot block the session,
  but they may finish in any order, and Router stamps events on receipt: a delayed
  `PreToolUse` after `Stop` would reopen finished work. Router's Codex hooks are
  therefore synchronous with a 2 s timeout, like its Claude observe hooks. The hook
  always answers `{}` and measured 60-90 ms locally, so it delays but never blocks Codex. [D, V]
- **Execution environment.** Hooks run through `$SHELL -lc` with a cleared environment
  replayed from the session snapshot. Profile scripts run on every hook, which adds
  latency and can print to stdout. The Router hook must not depend on inherited
  `AGENT_ROUTER_*` variables; the plugin's hook command must locate the Router socket
  itself. [S]
- **Detectability.** Router cannot tell an untrusted or disabled hook from an idle
  Codex: both produce no events. Item 4 must decide how to report this. Options:
  record the last Codex event seen per host, and, on explicit request, read Codex's
  config read-only for Router's trusted-hash entries.

## Live verification (item 4b)

Verified on 2026-10-17 with Codex CLI 0.162.1. Real `codex exec` sessions ran with
this repository's `router/plugin/codex/hooks.json` supplied as per-run overrides,
calling `router/plugin/codex/hook.sh` against an isolated Router daemon:

- A root session with a shell command and one spawned subagent appeared in Router as
  a `codex` session and a `codex/observe` agent, with the subagent's tool activity,
  timing, and `stopped` ("Codex session ended") after the session ended. No snapshot
  section was partial.
- Resuming the session (`codex exec resume <id>`) did not continue the earlier
  subagent: the model could only `wait` with no receivers. Treating a Codex
  session's end as the end of its subagents therefore holds for exec sessions.
- Installed-plugin path: with the plugin added from the local marketplace and its hooks
  trusted only by writing the hashes `router/codex_plugin.py` computes (no bypass
  flag), Codex ran Router's hooks and Router recorded the session and subagent. This
  confirms the trust key format (`agent-router@agent-router:router/plugin/codex/hooks.json:<event>:<group>:<handler>`,
  the manifest's `./` removed) and that `${PLUGIN_ROOT}` is hashed unexpanded (Codex
  passes it as an environment variable).
- Not observed live: `PermissionRequest` (exec runs without approvals), `Interrupt`,
  and compaction. Their mapping rests on the documentation and source.

## Installation and trust (implemented)

`router setup` registers the Codex marketplace when Codex is installed, without
enabling the plugin globally. This step is best effort and never blocks setup.
`router enable` merges the project plugin entry; refresh plugins in Codex to populate
its cache. See [project enablement](project-enablement.md), including confirmed
migration of legacy global entries. The repository root ships `.codex-plugin/plugin.json`
so Codex loads its own hooks file rather than falling back to the Claude manifest.
Hooks must then be trusted once in Codex (`/hooks`). `router status` reports Codex
observation readiness read-only (`router/codex_plugin.py`): it reads the hooks file from
the plugin copy Codex loads (`$CODEX_HOME/plugins/cache/agent-router/agent-router/<version>`,
`local` first, else the highest version, as Codex selects it), recomputes Codex's trust
hash for each hook, and checks the `[features] hooks` switch. An untrusted, modified, or
disabled hook is reported instead of looking like an idle Codex. Configuration comes
from bounded native `config/read` and `hooks/list` requests to the selected home's
existing control socket, including Codex's project trust and configuration precedence,
rather than the user file alone. Hook trust comes from the native inventory, which
excludes project-layer trust entries. Missing
project admission or unavailable probe evidence cannot report `ready`. The selected
home is explicit `CODEX_HOME` or its default; no other homes are searched. See the
[readiness contract](project-enablement.md#codex-prerequisites).
Router starts no process or daemon and requests no plugin refresh. Missing native
daemon/API support reports `unknown`. Codex keeps a Git marketplace snapshot until
`codex plugin marketplace upgrade agent-router`; status
reflects that installed copy, not Router's package.

Readiness verification on 2026-10-18 used Codex 0.163.0 with isolated configuration:
an unmarked directory reported `project_not_enabled`; enabled roots, subdirectories
and worktrees reported ten untrusted Router hooks, rather than the global disabled
entry. The fixture daemon was started separately before probing. User configuration
and hook trust were unchanged, zero probe processes started, and the daemon remained
running with its user-agent identity unchanged after the checks. The three
enabled-directory queries took 0.033, 0.006 and 0.013 seconds respectively in this
local sample. A marked but
untrusted project reported `plugin_disabled`; its project enablement was ignored.
No agent turn was started. The native non-originating initialization path was checked
against Codex 0.163.0's source and a policy fixture on success and RPC failure.
Long directory and socket aliases, including dangling aliases, were tested with an
isolated socket fixture. Live OAuth behavior, enabled hook execution and observation
after manual trust are **NOT CHECKED** in this verification.

A separate isolated Codex 0.163.0 cache with a missing declared hook file reported
`unknown` with its native discovery warning in enabled roots, subdirectories and
worktrees. Configuration, daemon identity and the running daemon were preserved.
Whether ordinary interactive or exec startup creates the required control socket
is **NOT CHECKED**; this fixture did not exercise that client startup path. See the
[explicit fixture setup](project-enablement.md#codex-prerequisites).

## Testing implications for item 4

- Router-launched Codex children disable delegation and ignore user config
  (`router/child.py`), so they cannot stand in for native Codex sessions.
- Build test fixtures from the captured payload shapes above (content fields replaced
  with placeholders), and verify the inferred behaviours marked **I** against a real
  session: per-turn `SubagentStop`, `send_input` turns, `Stop` continuation,
  `Interrupt`, and whether a `resume`d session continues existing subagent threads.
  Router marks a Codex session's subagents stopped when the session ends. Live
  verification showed resume does not continue them, so they stay stopped.
- Hooks are stamped when they run, so a subagent event can still be stamped after the
  session's `SessionEnd`. Decided: such an event leaves the agent stopped unless a later
  root `SessionStart` (`resume`) has reopened the session (`router/native.py`), tested
  for new and existing subagent records.
- Capturing a session with subagents requires a persisted (non-ephemeral) session.
