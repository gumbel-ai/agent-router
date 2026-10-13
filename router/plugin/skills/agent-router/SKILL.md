---
name: agent-router
description: Delegate an explicitly requested task to an operator-configured OpenCode model, then review its isolated changes.
---

# Agent Router

Delegate only when the user requests it. Do not replace a requested executor or model. The MVP supports OpenCode; unsupported targets fail without fallback. Semantic intent checking is your responsibility, not the router's.

The UserPromptSubmit hook supplies `ROUTER_INSTRUCTION_ID`. Use that ID for every delegation from this submission. If it is absent, do not invent it. Check that the operator has started the daemon and configured the repository, model alias, setup command, and explicit allow/deny permissions in `opencode.json`.

Call each mutating operation as one standalone shell command in canonical literal form:

```text
router start --json '<invocation JSON>'
router review --json '<review JSON>'
router cancel --json '<cancel JSON>'
router close --json '<close JSON>'
```

Use single quotes around the JSON. For an apostrophe inside it, use the canonical shell escape `'"'"'`. No variables, substitutions, pipelines, command prefixes, or JSON files. Generate a fresh `request_id` for a new action; replay the identical payload with its existing ID after a transport failure.

Start requires `request_version: "1"`, `request_id`, `instruction_id`, `executor: "opencode"`, the configured `model` alias, `task`, absolute `parent_repo` and `parent_worktree` root paths, and `mode: "write"`. Optional `context` contains arrays `relevant_paths` and `constraints`. The returned task ID means registration, not completion.

Inspect a task with `router status <task_id> --json`. Use `router status --filter needs_decision --json` when you need to act. Do not watch by repeatedly polling from model turns; a person can run `router status --watch` without consuming model context.

When `awaiting_review`, inspect the recorded patch, child files, and test/output artifacts. Review JSON contains `task_id`, `request_id`, `expected_attempt` (the current `attempts` count), and `outcome: "accepted"` or `"rejected"`. Rejection also requires actionable `feedback`; one correction uses the same worktree, session, and resolved model. Limits cannot be changed through task inputs. Process completion does not establish code quality.

Acceptance does not apply code. In the parent worktree, run `git apply --check <recorded-patch>` followed by plain `git apply <recorded-patch>`. If context has changed and the check fails, stop and preserve the child's work for reconciliation. Do not use `--3way`, `--reject`, or merge the child branch. Empty patches need no application.

After integration, close with `task_id`, `request_id`, and `disposition: "integrated"`. For final rejection use `"discarded"`. Close removes the dirty child worktree, branch, and task refs by default. Set `keep_artifacts: true` to retain them. Inspect `cleanup_pending` and `cleanup_error`; replay the same close request to retry failed cleanup.

Cancel JSON contains `task_id` and `request_id`. Wait for verified `cancelled`; uncertain execution remains `interrupted` and cannot be closed. Use cancel to establish stopped execution after an interruption; never launch a replacement while old work may still run.

Failed work remains inspectable. You may explicitly accept its artifacts for salvage only when execution is verified stopped and a current final tree exists. Its failed execution outcome and reason remain recorded. Do not retry failed infrastructure in the same task. OpenCode does not produce the structured blocker outcome in this MVP.

Worktrees and dependency environments protect the parent checkout during normal execution. They are not a sandbox: children still have the host user's filesystem, credentials, and network access.
