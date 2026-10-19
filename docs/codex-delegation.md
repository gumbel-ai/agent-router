# Delegation from a Codex coordinator

Router's Codex plugin supplies the same delegation skill and literal
`router start`, `review`, `cancel`, and `close` contract as its Claude plugin.
This adapter supports root Codex coordinators. It adds no executor, retry policy,
remote launch path, tracing configuration, or new CLI/JSON envelope.

The operator must already have enabled the project with `router enable`, registered
the repository and exact child model with `router init`, started Router, and
manually trusted the plugin's current hooks in Codex through `/hooks`. See
[project enablement](project-enablement.md). The new delegation handlers require
native trust; the existing observer groups retain their indexes and hashes. Review
changed declarations before trusting their new hashes. Router
never writes hook trust or discovers additional Codex homes. The selected
`CODEX_HOME`, or its native default, continues to select the parent installation.

## Native identity and authorization

The adapter follows the public Codex `rust-v0.163.0`
[hook schema](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/schema.rs)
and [shell hook mapping](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/events/pre_tool_use.rs).
`UserPromptSubmit` and `PreToolUse` carry the authoritative `session_id` and
`turn_id`; Router maps the latter to its existing submission identity. The native
schema omits both `agent_id` and `agent_type` on root events. Any presence of either
field is refused for parent delegation, regardless of its value. Codex subagents
remain observable; their independent turns cannot supersede their root's Router
submission. No label or model name establishes parent authority. Missing or
malformed required identity or an incompatible event schema cannot issue or authorize
a delegation. The root's absent native agent ID remains unknown.

The prompt hook returns `ROUTER_INSTRUCTION_ID` through native
`hookSpecificOutput.additionalContext`. Only the IDs, runtime and working directory
reach Router's submission endpoint; the parent prompt is never forwarded. A repeated
native submission returns its existing instruction ID. A new native turn, including
a new turn in a resumed session, gets a new instruction ID under that same session.

The synchronous shell `PreToolUse` hook uses the existing canonical literal parser
and authorization endpoint. Each operation must be one standalone
`router OPERATION --json 'JSON'` command with canonical shell quoting. The daemon
requires the current submission, exact request payload, and task's parent session.
An identical request replay returns the original operation result without creating
another task; a changed payload with the same request ID is refused. Reading task
status grants no mutation authority. Review, cancellation, acceptance, artifact
integration and close retain their existing guards and operator limits.

An unavailable prompt hook reports a diagnostic and allows ordinary conversation
to continue without a new instruction ID. Missing, broken, or stale hook integration
also allows ordinary shell tools to continue, including legacy globally enabled
plugins in projects without Router enabled. A selected integration's failure is
reported and returned as an empty native response; the wrapper never retries a
different interpreter or entrypoint. A working authorizer's explicit denial still
reaches Codex unchanged.

An unavailable integration's empty response grants no authority. Router's daemon separately refuses
every new `start`, `review`, `cancel`, or `close` without its matching durable grant.
If an authorization reply was lost after its grant was recorded, delivering the
same literal request may complete it once. Replaying an already recorded operation
returns its original task without launching new work or resetting its state. Keep
the exact payload and request ID after lost replies; neither an empty native hook
response nor a transport failure authorizes bypassing Router, restarting a task,
or launching a replacement. Unsupported child adapters fail without fallback.
The [shared delegation skill](../router/plugin/skills/agent-router/SKILL.md)
documents review and shutdown uncertainty.

## Verification on a machine with native access

Synthetic checks exercise the native payload and output shapes through the existing
controller and real hook/daemon transport. Actual fresh/resumed Codex parent sessions
and authenticated child execution are **NOT CHECKED** until the following is run.
No plugins, credentials or live tracing content are needed for the synthetic tests.

Ask a teammate with an already configured Codex/OpenCode installation to test the
PR's exact head in a disposable, enabled `example-project`. Keep native session IDs,
paths and provider details private; report the head, versions, pass/fail and sanitized
errors in the PR.

1. Run `router status` in that project and confirm native readiness. If the changed
   hooks are untrusted, have the operator inspect and trust them through `/hooks`.
   Confirm a fresh Codex root turn receives an instruction ID. An ordinary turn in
   a project without Router enabled must start no Router hook and create no
   observation or delegation record.
2. Explicitly ask that coordinator to delegate a small file edit through the
   configured OpenCode alias. Inspect the one registered task, its child worktree
   and resulting patch. Replay its identical `start` request and confirm it returns
   the same task; changing its payload under that request ID must fail. Do not run
   a replacement command after a lost reply.
3. Resume the same native Codex session. Confirm its new turn receives a different
   instruction ID and can review/accept its own completed task. A stale instruction
   ID or a different coordinator session must not acquire authorization. Inspect
   and apply the accepted patch using the skill's check/apply procedure, then close
   with `integrated` and verify cleanup. Use a second disposable task to verify
   cancellation and `discarded` close after execution is confirmed stopped.
4. Repeat from an enabled subdirectory and separately enabled worktree. Native
   subagent prompts must not replace the root's instruction ID or gain Router
   mutation authority. Stop the disposable Router daemon: ordinary conversation
   must continue with an explicit unavailable diagnostic and no new instruction ID;
   attempted Router dispatch must fail without launching a child.
5. With the disposable daemon running, test a missing or outdated Router runtime
   selected by the plugin. Ordinary conversation and an ordinary shell command must
   continue with a diagnostic and no invented instruction ID or grant. Ungranted
   Router mutations must be refused by the daemon. Restore the selected runtime
   before further delegation; do not try a second launch path after an uncertain
   reply. A legacy global plugin in an unmarked project must also leave ordinary
   shell commands available.

Claude-child execution requires its separate executor change and a separate native
check. No Claude-child, fleet, live tracing, or provider usage/cost coverage is claimed
by this adapter.
