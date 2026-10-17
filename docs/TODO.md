# Agent Router TODO

Work proceeds strictly in this order. Each item is one small, single-subsystem PR (or
a short series) that follows [the review process](../CODE_REVIEW_PROCESS.md): internal
adversarial review before the first push, CI green on Ubuntu and macOS, and the P1/P2
stopping rule for external review.

Context: the owner's local planning documents `agent-router-fleet-monitor-spec.md`
(the fleet spec) and `agent-router-langfuse-correlation-spec.md` (the Langfuse spec).
They are not part of this repository.

## Guiding boundary: no overlap with Langfuse

Most users will run Langfuse alongside Agent Router. Langfuse's official Claude Code and
Codex integrations own content and per-call analytics: prompts, responses, tool
inputs and outputs, per-call token usage, cost analysis, trace storage, and dashboards.
Agent Router owns lifecycle and fleet state: which hosts, sessions, and agents exist,
their status and relationships, host capacity, managed processes, and Router-dispatched
tasks. Where the two meet, Router exposes identifiers (`host_id`, `session_id`,
`agent_id`, `router_task_id`) and never a second copy of content.

Before adding any feature, ask whether Langfuse already provides it. If it does, do not
build it in Router.

## Decided

- **Error text stays raw.** Errors (host snapshot errors, SSH stderr, sampler failures)
  are passed through as written, made safe only for terminal output and bounded in
  length. Categorizing or scrubbing errors is not Router's job.
- **Recorded departures from the fleet spec.** Raw errors supersede §11.3.6 ("secrets
  redacted"). Removing native usage and cost (item 2) changes the §20.8 promise that
  existing `router status` output stays compatible. Both are owner decisions.

## Standing rules for every item

- Consolidate the tests the PR touches into table-driven or contract tests (process
  rule 5a) as part of that PR; do not leave cleanup for later.
- Attach the evidence the change affects: performance measurements for hook, sampler,
  or snapshot paths, and a privacy check for anything that leaves the host.
- "Done when" criteria are behavioural. Line counts and suite time are reported as
  measurements, not used as pass conditions.

---

## 1. Codex lifecycle audit (no product code)

**Why:** The first target project switches between Claude Code and Codex. Lifecycle
handling must be designed with Codex's events in view, not only Claude's.

**Scope:**
- Pin a Codex CLI version (installed: 0.162.1) and document its lifecycle surface:
  session start and end, subagent start and stop, prompt, tool, permission, stop, and
  interrupt hooks, and the identifiers each carries.
- Record the gaps against Claude: no documented nested `parent_agent_id`; no
  equivalents of `StopFailure` and `PostToolUseFailure`; stop hooks can request
  continuation; session end differs from switching conversations.
- Record prerequisites: plugin installation, hook trust, hooks enabled, supported
  version, and fail-open forwarding. Version detection alone cannot tell whether
  observation is disabled or untrusted.
- Define identifier namespaces for both sources (`provider/runtime`, `host_id`,
  `session_id`, `agent_id`, parent identity), shared with item 9.
- Note that Router-launched Codex children disable delegation and ignore user config
  (`router/child.py`), so they cannot stand in for native Codex sessions in tests.

**Done when:** a short committed design note lists supported events, identifiers,
semantics, gaps (reported as unavailable, never inferred), prerequisites, and the
identifier namespace, verified against a real local Codex session.

## 2. Remove Langfuse-overlapping features from native observation

**Why:** Router parses native Claude subagent transcripts to compute token usage, cost,
and model history, and retains assistant summaries and touched-file lists. These
duplicate Langfuse and cause most of the complexity in `router/native.py`
(`refresh()`, prefix digests, mid-read checks, legacy upgrades).

**Scope:**
- Delete transcript reading for native agents: `refresh()`, transcript cursors and
  digests, usage aggregation, transcript-derived model history, and native cost
  estimation.
- Drop host-reported native cost, which capacity does not use.
- Stop retaining `summary` (last assistant message) and `files_touched` for native
  agents.
- Keep task labels, current tool activity, identities, timing, and requested and
  resolved model with their provenance (the hook already supplies the resolved model).
- Keep Router-dispatched task results, summaries used for parent review, and their
  operational accounting (`router/codex.py`). These serve a different purpose; revisit
  only in item 9.
- Legacy rows: removed fields must stay absent from every view, including detailed
  status, not merely stop being collected.
- Update README and the plugin README ("use Langfuse for usage and content").

**Done when:** no code path opens a native transcript; no status, snapshot, or CLI
output includes native usage, cost, summaries, or file lists, for new or legacy rows;
old databases still open and read; CI green.

## 3. Per-agent ordering for waiting state

**Why:** A deferred PR #4 finding: waiting-for-input state was ordered per session, so a
delayed completion for one agent was dropped once another agent produced a newer event,
leaving the first agent falsely waiting.

**Scope:** Track waiters per agent with their own timestamps and apply waiter updates
before the session-wide staleness check, keeping the existing reset boundaries and
arrival order at exact ties. Migrate rows that stored waiters as a list.

**Decision (recorded):** The original item proposed replacing both observers' ordering
logic with an event log replayed through one transition table. A design review against
the contract tests showed that chronological replay changes deliberate behaviour (a
late `launch` must not extend a stopped agent's duration; a stop plus later result must
stay stopped even if an older start arrives; a delayed pre-restart tool start must stay
live), and adds migration, compaction, and per-hook write costs. The redesign was
rejected. Codex observation (item 4) needs observers that accept a second source, not a
rewrite.

**Done when:** a delayed completion for one agent is applied after another agent's
newer events; same-agent ordering and legacy rows are covered by tests; existing
contract tests pass unchanged.

## 4. Native Codex activity observation

**Status:** 4a (observer mapping, restart reconcile, sessions and source in status) and
4b (Codex plugin, setup install, readiness in status, live check) done. Live results:
[codex-lifecycle.md](codex-lifecycle.md#live-verification-item-4b). Fleet check is item 5.

**Why:** Parity with Claude Code for the Claude/Codex switching workflow.

**Scope:**
- Observe Codex sessions and native subagents through the hooks audited in item 1, as
  a second source for the existing observers. No transcript parsing; lifecycle and
  identifiers only.
- Restart reconciliation for Codex records (both observers currently reconcile Claude
  only).
- Show top-level sessions in local `router status` (today it lists Router tasks and
  native agents only), and show `source` (claude or codex) in status and fleet output.
- Report Codex observation as unavailable when prerequisites are missing (not
  installed, untrusted, disabled, unsupported version), never as "no activity".
- Test with a real native Codex session, not a Router-launched child.

**Done when:** a native Codex session and its subagents appear with correct lifecycle in
`router status` and `router fleet status`; unsupported events are shown as unavailable;
missing prerequisites are visible.

## 5. Fleet visibility check with both sources

**Status:** Router-task projection done (`router_tasks` snapshot section, fleet table rows,
older peers accepted). Emulated two-host check is automated; the real two-host check
runs with the manual Linux testing.

**Scope:**
- Verify `router fleet status` end to end with Claude and Codex activity on at least
  two hosts (subset of fleet spec §19.3).
- Add a bounded lifecycle projection of Router-dispatched tasks to fleet snapshots.
  Today snapshots expose Claude task records only.

**Done when:** a two-host check shows sessions, agents, and Router tasks from both
sources with correct host identity and capacity.

## 6. Simplify fleet snapshot validation

**Status:** Done. Change and omission accounting removed; unknown fields are dropped and
text is sanitized without marking a snapshot partial. Hostile-input tests unchanged.

**Why:** `_validate_snapshot` and `_project_record` treat every peer as hostile and
track every projection change. Fleet hosts are the user's own machines over the user's
own SSH. Item 2 also removes the native usage validation.

**Scope:**
- Keep schema projection (unknown fields dropped), type, finiteness, and length bounds,
  identity safety, and the one MiB cap.
- Remove change-accounting and per-field transformation bookkeeping that only feeds
  diagnostic messages.

**Done when:** malformed or oversized snapshots are still rejected or marked partial,
and the existing hostile-input tests still pass.

## 7. Replace the regex command matcher in hooks

**Why:** `router/hooks.py` finds `router start|review|cancel|close --json` in Bash
commands with a regex. It is the authorization gate, so it must be exact.

**Scope:** Reuse the canonical parser in `router/dispatch.py`, which already checks the
token count and the `shlex.join()` round-trip. Plain token matching would accept
double-quoted substitutions that the canonical parser rejects. Keep current allow and
deny behaviour.

**Done when:** no regex remains in `router/`; dispatch tests pass, plus cases for
quoting, chained commands, and substitutions.

## 8. Milestone B: process visibility, rebuilt in small PRs

**Why:** Open PR #5 (+4,338 lines, `processes.py` alone 1,203) targets the old PR #4
branch and predates the text-handling and simplification changes. Rebasing it as one
PR would repeat the PR #4 review churn.

**Scope:** Close PR #5 after salvaging its work into a series on top of `main`:
1. Claude and Codex process-tree sampling with attribution confidence (`exact`,
   `session`, `host`, `unknown`).
2. `router run` managed-command wrapper and completion evidence.
3. Managed-process alerts and fleet output, with a managed-command smoke test.

Keep the spec's limits: no command-text capture by default, no kill or cancel, and no
exact attribution without the wrapper.

**Done when:** the fleet spec's Milestone B outcomes are met, each PR is reviewed and
merged separately, and PR #5 is closed with a pointer to the series.

## 9. Langfuse correlation bridge

**Why:** Join Router lifecycle records to Langfuse traces without moving content
through Router.

**Scope:**
- After the Langfuse integrations are pinned and audited, expose through Router's
  existing API: the item 1 identifier set, lifecycle event, timestamp and status, and
  existing retry and attempt identity. Include existing work-item IDs only when
  supplied.
- Join at analysis time by runtime, host, and session; never re-ingest or update traces.
- Fail open: correlation failures never block agent work, and missing joins are
  visible.
- Revisit Router child-task token and cost accounting: keep it only if Langfuse cannot
  attribute Router-launched children.

**Done when:** Langfuse spec Stage A synthetic contract checks pass. Stage A cannot
establish live plugin coverage; Stages B and C need separate data-handling approval.

## 10. Milestone A acceptance and Milestone C polish

**Scope:**
- Remaining fleet spec §22 evidence not already attached to earlier PRs: privacy audit,
  upgrade and rollback notes, and the full two-host live smoke test (§19.3), including
  manual Linux testing (user-run).
- Persistent Linux user service, health reporting gaps (version-mismatch reporting
  already exists), and a deployment guide.
- Final pass on `CODE_REVIEW_PROCESS.md`: reduce it to a checklist that keeps every
  defect pattern.

**Done when:** fleet spec acceptance criteria 1–12 are demonstrated on real hosts.

## Deferred

- Milestone D (OTLP export, Grafana dashboards): reconsider only if Langfuse plus the
  fleet view leave a real gap.
