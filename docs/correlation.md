# Correlation export

`router correlation --json` exports one read-only page of identifiers and lifecycle
metadata from Router's existing database. It does not query Langfuse, read native
transcripts, install tracing plugins or upload traces. `traced` is always `unknown`.
The export contains no prompts, responses, task instructions, labels, paths, tool
contents, credentials, usage or costs.

This is the local part of Stage A. Fleet transport is a separate follow-up. Live
trace reconciliation, child tracing and Stages B/C are not covered.

```sh
router correlation --json --limit 100
router correlation --json --limit 100 --cursor 'CURSOR_FROM_PREVIOUS_PAGE'
```

The existing daemon operation is `correlation`, with an object containing optional
`limit` and `cursor`. `limit` defaults to 100 and accepts integers from 1 through
100. The CLI uses one fifteen-second transport deadline. It never starts a daemon,
command, retry or subscription. Interrupting the export exits 130 without changing
state. Successful JSON exits 0, including visibly partial pages. Router or payload
errors exit 1; malformed CLI syntax exits 2. Other commands retain their contracts.

## JSON contract, version 1

The envelope contains `schema_version`, the producing `host_id`, `generated_at`
(host epoch milliseconds), `status_scope: "recorded"`, `records`, `next_cursor`,
`partial`, `invalid_records` and a bounded `errors` list. A page reads at most
`limit + 1` source rows, including lookahead, and returns at most `limit` records. Its JSON is bounded to
one MiB; a record deferred by the byte bound remains available on the next page.

Each record has `kind`, `host_id`, `traced`, and these nullable metadata fields:

| Fields | Meaning |
| --- | --- |
| `runtime`, `model_provider` | Execution runtime and model provider are separate. Unknown runtime strings pass through. No provider is inferred from a runtime or model name. |
| `session_id`, `agent_id` | Known native identities in that runtime's namespace. |
| `parent_session_id`, `parent_agent_id`, `parent_runtime` | Only stored parent identities; never inferred from labels, filenames or nearby times. |
| `router_task_id`, `managed_id`, `work_item_id`, `event_id` | Separate Router task, managed command, supplied native team-task and completion identities. |
| `status`, `lifecycle_event`, `timestamp` | Last recorded status, stored event kind and its host timestamp when available. This is durable evidence, not a new liveness check. |
| `created_at`, `updated_at`, `started_at`, `finished_at`, `uncertain_at` | Stored timing, in host epoch milliseconds; null when unavailable. |
| `attempt_number`, `attempts`, `review_retries`, `continuations` | Existing attempt identity and counters; no execution retries are added. |

Kinds are `session`, `agent`, `work_item`, `router_task`, `attempt`, `managed` and
`completion`. A Router task's `session_id` is its **child** native session, when
known; its stored submission session is `parent_session_id`. Attempt identity is
the tuple `(host_id, router_task_id, attempt_number)`. Current task and retained
attempt records may overlap; they are separate kinds, not additional executions.
An `agent` record keeps its hosting session in both `session_id` and
`parent_session_id`; `agent_id` is the observed child identity. For Codex, that
agent ID is the child thread ID. A Claude agent ID does not establish a separate
native session. Session-only joins locate the hosting session and do not establish
child trace attribution. An observation's generated row ID is not exported as a
Router control-task ID.
Parent runtime requires the referenced submission to match the task's durable
parent session. A contradictory link leaves runtime unknown with a partial
diagnostic. Task and attempt rows without a stored host claim use the producing
Router identity, as existing task views do; explicit null, malformed or foreign
claims are never silently relabelled.

Managed records carry only explicitly supplied session/agent identities. They do
not currently record runtime; even an executable or label named `codex` cannot
establish it. A completion keeps the existing `completion-N` event ID and stored
phase/timestamp. Retained event identity tombstones can have unknown outcome
metadata. A phase timestamp is stored evidence and does not establish when a
detached job ended: wrapping a detaching launcher observes the launcher.
Completion-row timestamps can also reflect imported upgrade evidence; only a
known `finished_at` reports the retained command-end timestamp. Retained legacy
rows are included without reapplying project admission to historical records.

Identifiers keep their exact printable spelling, including repeated internal spaces;
controls, surrounding whitespace and oversized values are rejected without rewriting.
Missing metadata remains null. Durable database keys take precedence over conflicting
JSON copies, with a partial diagnostic. Malformed values become unknown, malformed records
are omitted, and bounded field-level diagnostics make the page partial without
copying the rejected content. Records belonging to another host are omitted with
a partial diagnostic. One database read snapshot covers each page; it holds no
write transaction and does not reconcile process spools or move completion cursors.

Pagination follows table order and row insertion order, not global event time.
Pass `next_cursor` unchanged until it is null. Cursors are versioned and bound to
the producing host. Each page is a new read snapshot: updates, pruning or database
replacement between pages can change the inventory. Start a fresh scan for a new
inventory; use [completion wait/ack](completions.md) for durable delivery.

## Mapping IDs outside Router

Keep an explicit mapping between the external collector/trace host and Router's
existing `host_id`. A fleet display name, hostname or trace user ID is not that
mapping. Preserve the Router ID as returned; do not regenerate it from labels.
Join session identities by exact runtime, mapped Router host and native session.
Missing or ambiguous mappings and identities remain unmatched. A model provider
such as `openai` does not establish runtime `codex`.

The [pinned plugin audit](langfuse-audit.md) supplies the synthetic contract shapes:
Claude ordinary attributes use `session_id`; attached mode retains observation
`metadata.session_id`. Standard Claude subagent metadata has no authoritative
native agent or parent ID, so a transcript basename cannot establish that join.
Codex ordinary attributes use `sessionId`, and turn observations carry
`codex.thread_id`. A child observation can share its root trace: use its explicit
thread ID when matching a Codex agent, not the root trace's session ID alone.
The literal `unknown` emitted for a missing ID is not a usable join key.

Synthetic checks cover these shapes, missing IDs, runtime/host mismatches, explicit
child identity and resumed sessions retaining their session identity. These checks
establish export compatibility only. Live traces, real resumed-child capture,
physical SSH, independent reboot and two-host smoke are **NOT CHECKED**.
