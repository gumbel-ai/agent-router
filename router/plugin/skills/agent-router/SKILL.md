---
name: agent-router
description: Delegate an explicitly requested task to an operator-configured OpenCode or Codex model, then review its isolated changes.
---

# Agent Router

Monitoring shows every open Router task and up to 100 native observations per page; use `router status --native-offset 100` for the next native page or look up a native ID directly for its full retained record. Compact lists omit full transcript paths, summaries, pricing-source maps and file lists; detail includes the latest 64 reported file paths, not proof of every edit. Router cost/usage/time totals include recorded attempts and exclude parent review waits. Pending or skipped transcript data is explicit and cannot produce a complete token-based cost.

Delegate only when the user requests it. Do not replace a requested executor or model. Router supports OpenCode and Codex; unsupported targets fail without fallback. Semantic intent checking is your responsibility, not the router's.

The globally installed plugin observes native Claude work without per-project setup. External delegation requires the operator to run `router init` in that project, approving its exact model, setup command, permissions, and limits. Use `router setup` for the managed macOS service, or explicit `router daemon start` for manual operation. Hooks never start the daemon or install dependencies. Never run setup, init, or uninstall on the user's behalf without explicit permission.

The UserPromptSubmit hook supplies `ROUTER_INSTRUCTION_ID`. Use that ID for every delegation from this submission. If it is absent, do not invent it. Check that the operator has started the daemon and configured the repository, model alias, setup command, and executor permissions. OpenCode requires explicit allow/deny permissions in `opencode.json`; Codex requires an operator-approved read-only or workspace-write sandbox in Router configuration. These permission formats are not interchangeable.

Call each mutating operation as one standalone shell command in canonical literal form:

```text
router start --json '<invocation JSON>'
router review --json '<review JSON>'
router cancel --json '<cancel JSON>'
router close --json '<close JSON>'
```

Use single quotes around the JSON. For an apostrophe inside it, use the canonical shell escape `'"'"'`. No variables, substitutions, pipelines, command prefixes, or JSON files. Generate a fresh `request_id` for a new action; replay the identical payload with its existing ID after a transport failure.

Start requires `request_version: "1"`, `request_id`, `instruction_id`, `executor: "opencode"` or `"codex"`, the configured `model` alias, `task`, absolute `parent_repo` and `parent_worktree` root paths, and `mode: "write"`. Optional `context` contains arrays `relevant_paths` and `constraints`. The returned task ID means registration, not completion.

Codex aliases use exact `openai/model-id` values. Router passes the model ID to `codex exec --json`, with no approval prompts or unrestricted sandbox bypass. It resumes corrections using the recorded thread ID, never `--last`. Codex native delegation is disabled for Router-launched children; native Codex observation is not implemented. Codex uses its saved CLI login; Router strips provider-key/endpoint overrides from its child environment. Model metadata identifies the configured selection, not an independently reported served model. Token fields retain CLI names: cache reads and writes partition input, and reasoning output is a subset of output. Codex emits cumulative thread counters; Router records per-attempt differences so corrections do not double-count earlier work. `usage_complete: false` means totals contain only reported usage. Cost uses dated standard short-context rates or operator-provided flat rates for input/cache-read/cache-write/output tokens, excludes tool fees and long-context premiums, and remains unknown if required usage or exact pricing is unavailable. A zero exit code without a completed turn and a matching thread ID observed during that attempt is not successful execution.

Inspect a task with `router status <task_id> --json`. Use `router status --filter needs_decision --json` when you need to act. Do not watch by repeatedly polling from model turns; a person can run `router status --watch` without consuming model context.

The status view includes native Claude subagents as `control_mode: "observe"` alongside Router-launched children as `control_mode: "control"`. Native agents remain Claude-managed: never review, cancel, or close their `native-` IDs through Router. A native `stopped` observation is not evidence of successful work; stale or missing lifecycle evidence is `unknown`. Quiet agents are not assumed dead. Task descriptions and activity are summaries, not complete transcripts or proof of file ownership. Model and usage data come from host results and native transcripts; costs are explicitly labelled API-equivalent estimates, not subscription charges or invoices. Unknown rates or token details stay unknown. Additional hosts must preserve the same rule: native subagents for observation, Router-launched children for lifecycle control.

When `awaiting_review`, inspect the recorded patch, child files, and test/output artifacts. Review JSON contains `task_id`, `request_id`, `expected_attempt` (the current `attempts` count), and `outcome: "accepted"` or `"rejected"`. Rejection also requires actionable `feedback`; one correction uses the same worktree, session, and resolved model. Limits cannot be changed through task inputs. Process completion does not establish code quality.

Acceptance does not apply code. In the parent worktree, run `git apply --check <recorded-patch>` followed by plain `git apply <recorded-patch>`. If context has changed and the check fails, stop and preserve the child's work for reconciliation. Do not use `--3way`, `--reject`, or merge the child branch. Empty patches need no application.

After integration, close with `task_id`, `request_id`, and `disposition: "integrated"`. For final rejection use `"discarded"`. Close removes the dirty child worktree, branch, and task refs by default. Set `keep_artifacts: true` to retain them. Inspect `cleanup_pending` and `cleanup_error`; replay the same close request to retry failed cleanup.

Cancel JSON contains `task_id` and `request_id`. Wait for verified `cancelled`; uncertain execution remains `interrupted` and cannot be closed. Use cancel to establish stopped execution after an interruption; never launch a replacement while old work may still run.

Codex cancellation first interrupts its harness so foreground commands in separate process groups can shut down. Abrupt termination or missing shutdown evidence stays `interrupted` with `shutdown_unconfirmed: true`; a dead CLI alone does not prove its commands stopped. Repeating cancel cannot clear that uncertainty. Preserve the worktree and request operator inspection; do not close or replace the task.

Failed work remains inspectable. You may explicitly accept its artifacts for salvage only when execution is verified stopped and a current final tree exists. Its failed execution outcome and reason remain recorded. Do not retry failed infrastructure in the same task. Neither executor produces the structured blocker outcome in this version.

Worktrees and dependency environments protect the parent checkout during normal execution. They are not a sandbox: OpenCode children have the host user's filesystem, credentials, and network access subject to their permission profile. Codex commands run in the approved Codex sandbox. Dependency setup runs outside that sandbox and must be operator-approved.
