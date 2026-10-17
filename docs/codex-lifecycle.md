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
- **Fail open.** Register hooks with `async: true`. Background hooks cannot block or
  alter the session; at most eight run concurrently and unfinished ones are cancelled
  at session end. A synchronous hook that is slow stalls the agent until its timeout
  (600 s default). [D]
- **Execution environment.** Hooks run through `$SHELL -lc` with a cleared environment
  replayed from the session snapshot. Profile scripts run on every hook, which adds
  latency and can print to stdout. The Router hook must not depend on inherited
  `AGENT_ROUTER_*` variables; the plugin's hook command must locate the Router socket
  itself. [S]
- **Detectability.** Router cannot tell an untrusted or disabled hook from an idle
  Codex: both produce no events. Item 4 must decide how to report this. Options:
  record the last Codex event seen per host, and, on explicit request, read Codex's
  config read-only for Router's trusted-hash entries.

## Testing implications for item 4

- Router-launched Codex children disable delegation and ignore user config
  (`router/child.py`), so they cannot stand in for native Codex sessions.
- Build test fixtures from the captured payload shapes above (content fields replaced
  with placeholders), and verify the inferred behaviours marked **I** against a real
  session: per-turn `SubagentStop`, `send_input` turns, `Stop` continuation,
  `Interrupt`, and whether a `resume`d session continues existing subagent threads.
  Router marks a Codex session's subagents stopped when the session ends; if resume
  can continue them, their later activity must be allowed to reopen them.
- Async hooks are stamped when they run, so a subagent hook can arrive after the
  session's `SessionEnd` with a later timestamp. Decide from the real session whether
  post-end subagent activity is allowed only after a later root `SessionStart`
  (`resume`), and test both new and existing subagent records.
- Capturing a session with subagents requires a persisted (non-ephemeral) session.
