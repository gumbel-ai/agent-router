# Correlation export

`router correlation --json` exports one read-only page of identifiers and lifecycle
metadata from Router's existing database. It does not query Langfuse, read native
transcripts, install tracing plugins or upload traces. `traced` is always `unknown`.
The export contains no prompts, responses, task instructions, labels, paths, tool
contents, credentials, usage or costs.

Local and fleet exports provide Stage A metadata for synthetic compatibility
checks. Live trace reconciliation, child tracing and Stages B/C are not covered.

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
| `router_task_id`, `managed_id`, `work_item_id`, `event_id` | Separate Router task, managed command, explicitly supplied work-item and completion identities. |
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

Callers may declare one optional `work_item_id`: use `router run --work-item-id ID`
for a managed command or a top-level `"work_item_id":"batch-job"` in an authorized
`router start --json` invocation. It follows the identifier bounds below; explicit
JSON null is invalid. The declaration remains fixed across task attempts and
survives evidence replay and daemon restart. It is carried in managed/completion
records, task/attempt records and their local/fleet views. Historical or omitted
values stay absent in those records and null in this correlation schema.

For native `work_item` records, the ID remains the stored native task ID. For
`router_task`, `attempt`, `managed` and `completion`, it is the caller's declaration.
Router assigns no meaning or authority to either value and does not infer costs,
parentage or trace attribution from matching strings. Labels, environment variables
and nearby timestamps never provide this field. There is no arbitrary metadata
object; callers must supply identifiers rather than content or secrets.

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

## Fleet pages

```sh
router fleet correlation --json --config fleet.toml --limit 100
router fleet correlation --json --config fleet.toml --host worker-b --limit 100 --cursor 'CURSOR_FROM_THAT_HOST'
```

The first command requests one page from each configured host concurrently, using
its existing local socket or SSH transport. `--host` selects a configured display
name; it does not establish a Router identity. A cursor requires `--host` and is
validated against the responding Router host even on a final page. Follow each
host's `page.next_cursor` separately. Pagination never automatically drains a host.

The fleet envelope has `schema_version: 1`, `generated_at`,
`status_scope: "recorded"`, `partial` and ordered `hosts`. Each host contains its
configured `name`, known `host_id` or null, and `ok`. A successful host has `page`
with the local JSON contract. A failed host has an `error` object with a structural
`code` and, when available, `exit_code`, numeric `errno` or a fixed field-level
`detail`. No SSH target, tags, raw SSH stderr or raw rejected data is forwarded.
Errors can be inspected using the existing transport's operator diagnostics.

| Error code | Meaning |
| --- | --- |
| `spawn_failed` | The local SSH executable could not start; `errno` may identify the OS failure. |
| `exit_failed` | SSH or the remote Router command exited unsuccessfully; `exit_code` is retained. This includes unreachable hosts and peers without the CLI operation. |
| `deadline` | Collection exceeded its transport deadline. |
| `output_limit` | The SSH response exceeded the one-MiB wire limit. |
| `transport_failed`, `cleanup_failed` | The transport failed while collecting or cleaning up its owned reader process. |
| `unavailable` | The local Router socket could not be reached. |
| `request_failed` | The local Router request failed; inspect the daemon log. |
| `invalid_response` | The host did not supply a valid, bounded version-1 page envelope or cursor. |
| `duplicate_host_id` | Selected configured hosts claimed the same Router identity. Pages are omitted to prevent ambiguous joins; reported IDs remain unchanged. |

The receiver projects known metadata fields again. Unknown properties are omitted.
An invalid scalar becomes null, and an invalid kind or foreign-host record is
omitted while other records remain usable. `traced` stays `unknown` even if a peer
claims otherwise. Such changes make the page partial. Fleet pages add
`wire_error_count` and up to ten `wire_errors` entries, containing only `section`,
a zero-based list `index` and fixed `fields`. Producer `invalid_records` and safe
producer diagnostics remain separate, so receiver errors are not double-counted.
Missing identities stay null; the envelope host does not fill an unknown record
host. Duplicate identity detection covers selected hosts only; external mappings
must still be unambiguous across the inventory they join.

`--limit` remains 1–100 per host; fleet configuration allows at most 32 hosts. Each
received and projected page is capped at one MiB, and the compact fleet JSON is
capped at `32 * (1 MiB + 16 KiB) + 4 KiB`. Collection uses the configured
`ssh_timeout_seconds` for each host, including one overall deadline for local
socket reads, and the existing bounded SSH process cleanup. There are no retries,
job launches, completion acknowledgments, daemon starts, credential changes or
host-key bypasses. Interruption waits for bounded outstanding readers to finish
cleanup and leaves Router state unchanged.

| Fleet correlation exit code | Meaning |
| --- | --- |
| 0 | Every selected host supplied a complete page. |
| 1 | A partial result was printed, or configuration/transport arguments failed. Inspect each host and page. |
| 2 | Invalid CLI syntax. |
| 130 | Interrupted; no completed fleet JSON is promised. |

Other fleet commands retain their existing exit contracts. Automated checks use
separate local daemon processes and state directories, with the SSH subprocess
boundary replaced by a real local CLI process. They exercise process restart,
per-host pagination and failure isolation. This is simulated transport, not a
physical SSH or independent-machine reboot check.

## Mapping IDs outside Router

An external analysis tool can join Router records with already authorized trace
metadata using the tuple `(runtime, host_id, session_id)`. Router does not query
Langfuse, re-ingest traces or update them. A match does not change Router's
`traced: "unknown"` or prove complete tracing coverage.

Keep an explicit mapping file owned and read by that external tool. For example,
this JSON maps separately generated collector/trace host identities to Router's
existing IDs:

```json
{
  "trace_host_to_router_host": {
    "collector-a": "host-a",
    "collector-b": "host-b"
  }
}
```

This is an analysis-tool example, not a Router configuration format. Router never
reads the file. Replace `host-a` and `host-b` with the exact `host_id` values
returned by their Router exports, after independently verifying which collector
belongs to each Router host. The tracing integration must supply the collector
identity explicitly; the plugin audit does not establish a universal trace host
field. Keep real mappings outside the public repository. Do not change Router's
IDs, recompute them, or derive a mapping from a fleet display name, hostname,
trace user ID or nearby timestamp. An ambiguous mapping leaves the join unknown.

For each candidate, require a known execution `runtime`, its explicitly mapped
Router host, and a known native session identity. Compare all three strings
exactly; do not trim, case-fold or otherwise normalize IDs. Missing values remain
unmatched, including a null record `host_id` even when the enclosing page has a
known host. Runtime is separate from `model_provider`: `openai` does not establish
`codex`. Unknown runtime strings can pass through the export, but need an explicit
identity contract before an external tool can interpret them.

### Native session and child identities

The pinned Langfuse Claude and Codex plugin sources supply these contract shapes:

| Runtime and evidence | Native identity for analysis |
| --- | --- |
| Claude ordinary trace attributes | `session_id` identifies the hosting session. |
| Claude turn observation metadata, including attached mode | `metadata.session_id` identifies the hosting session. Standard subagent metadata supplies no authoritative native agent or parent ID. |
| Codex ordinary trace attributes | `sessionId` identifies the trace's session. |
| Codex turn observation metadata | `codex.thread_id` identifies that observation's thread, including a child sharing its root trace. Use this explicit identity for the observation rather than assuming the root `sessionId` identifies the child. |

The literal `unknown` emitted by the audited Codex parser for a missing ID is not
a usable key. Missing, contradictory or ambiguous native identities stay unknown;
do not select a convenient match. A resumed session can retain the same native
session ID, so that match alone does not identify a particular run or attempt.

A Router `agent` row's `session_id` identifies its hosting session. Its known
`agent_id` has the runtime-specific meaning described above: for Codex, an explicit
child thread ID can match an observation's `codex.thread_id`; for Claude, a session
match does not establish a child identity. A transcript basename, display label or
time window never establishes parentage. Keep supplied `parent_session_id`,
`parent_agent_id`, `parent_runtime`, `router_task_id`, `managed_id` and attempt
identity distinct. A session match is neither proof of a child trace nor authority
to attribute costs; the export contains no usage or costs to aggregate.

### Bounded collection and incomplete joins

Collect one local page or one page per selected fleet host using the commands
above. Follow each non-null `next_cursor` unchanged, with `--host` for fleet pages;
never use one host's cursor for another. The CLI bounds each page and each fleet
request, not an external tool's entire scan. Set a page, record and elapsed-time
budget in that tool. If a budget is reached while a cursor remains, mark the scan
incomplete rather than claiming no further matches.

Inspect fleet `partial`, every host's `ok` and `error`, and each page's `partial`,
`invalid_records`, `errors` and fleet-added `wire_errors`. Retain usable records
from successful hosts while reporting failed hosts and incomplete pages. An
unreachable host, an empty page and an unmatched identity are different results.
Do not replace a failed host with an empty successful inventory, fill missing IDs
from an envelope, or override duplicate-host diagnostics using the mapping file.

Each page is a new database snapshot, not a complete historical event stream.
Several record kinds can describe the same session or attempt; they do not prove
separate executions. Time-window matches may be listed as diagnostic candidates
only, never as authoritative joins, parentage or cost attribution. Report missing
or ambiguous joins explicitly and let agent work continue when collection or
analysis fails. Keep optional correlation analysis out of execution decisions.
An export or join failure never authorizes restarting work, inferring command
ownership or acknowledging an unmatched completion. Durable delivery and handling
use [wait/ack](completions.md) with explicitly known command identities.

### Verification on another machine

Synthetic checks cover these shapes, missing IDs, runtime/host mismatches, explicit
child identity and resumed sessions retaining their session identity. These checks
establish export compatibility only. Live traces, real resumed-child capture,
physical SSH, independent reboot and two-host smoke are **NOT CHECKED**.

A teammate with the required access can check the instructions below against the
PR's tested commit. Live reconciliation and Stages B/C require separate owner and
data-handling approval. Until that approval exists, use synthetic metadata and
leave live cases **NOT CHECKED**. These checks do not require installing a plugin,
starting a traced session, uploading content or changing hook trust.

1. Run `router correlation --json --limit 1`, or
   `router fleet correlation --json --config fleet.toml --limit 1` on an approved
   fleet. Follow each returned cursor on its own host. Confirm that IDs remain
   unchanged and that all remaining cursors and partial/error flags are reported.
2. In the external tool, create a private mapping as above. Using synthetic
   metadata first, confirm an exact runtime/mapped-host/session tuple matches.
   Change one component at a time, remove the host mapping, and supply an ambiguous
   mapping: each case must remain unmatched or explicitly unknown.
3. Confirm missing native IDs, Codex's reserved `unknown`, and Claude subagent
   basenames cannot produce an agent join. A root-session match must not be
   reported as child trace coverage, parentage or cost attribution. Reaching the
   scan budget with a remaining cursor must report an incomplete scan.
4. If live metadata access is separately approved, repeat the identity checks with
   an already authorized, content-free extract from the audited plugin version.
   Record version differences and unmatched identities; retain `traced: "unknown"`.
   Report only sanitized PASS/FAIL and remaining **NOT CHECKED** cases in the PR,
   keeping real mappings, host inventories and trace content private.
