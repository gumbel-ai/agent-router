# Durable managed-command completions

An active agent can use a blocking shell/tool call to wait for commands launched with
`router run` in an observed project:

```sh
router wait --consumer worker-a --timeout 300
```

Set the tool's own timeout longer than Router's deadline. Router returns one bounded
JSON object with `status` equal to `completed`, `failed`, `uncertain`, `deadline`, or
`unavailable`. It does not wake an ended chat. The active tool call waits locally,
checking the daemon with short requests rather than repeatedly asking the user for
status. It opens no write transaction between checks and does not occupy a daemon
connection between requests. No service is automatically started.
Empty polls inspect wrapper identities without a write transaction. Reconciliation
opens a short write transaction only when spool evidence is present, a wrapper cannot
be confirmed live, or the 60-second retention check is due. New completion evidence and
vanished wrappers are handled on the next poll without waiting for that interval.
These transactions do not span the wait's sleep; acknowledgment remains immediate.

## Subscription and acknowledgment

A consumer name subscribes to **all managed-command completion events in this Router
state directory**, across projects, sessions, and labels. The first successful request
persists the subscription before checking for events and includes all retained backlog.
Events already removed by ordinary retention cannot be recovered. Existing retained
terminal records are imported when upgrading. Import does not re-check project
markers: retained evidence from before project admission was required is also imported,
including evidence from unmarked projects. This interface does not include
Router-dispatched agent tasks or native session lifecycle events.

Event results include the existing `host_id` and an `event` object containing a stable
`event_id`, `managed_id`, process outcome, and known session/agent attribution. The
event's `host_id` preserves its producing host, including when state has moved to a
different serving host. Deduplicate using the event's host and event IDs, subject to
the database namespace requirements below. Malformed
managed IDs remain absent rather than blocking the stream. Labels are display
text and are never used to establish identity. No command
arguments, environment, output, working directory, or label appear in these events.
`deadline` and `unavailable` results contain only `status` and `consumer`; they carry
no event or host identity.

Delivery is ordered by durable insertion, one event per result. Reading never advances
the persisted cursor. After **durably recording handling**, acknowledge that event:

```sh
router ack --consumer worker-a completion-1
```

Acknowledgment is explicit and idempotent, including after the acknowledged event has
expired. Only the oldest pending event may advance a consumer's cursor. Distinct
consumer names have independent cursors. Concurrent processes sharing a name may
receive the same event: a name is a subscription, not an exclusive worker lease.
Reusing the same name after a consumer or daemon restart replays pending evidence.
A lost wait reply replays the same event; a lost acknowledgment reply can be retried.
Consumers should deduplicate durable handling using `(event.host_id, event.event_id)`.
The top-level serving host is not the producing identity. Delivery is
at least once; Router does not guarantee exactly-once consumer effects.

Ordinary seven-day retention deletes events only after **every** registered consumer
has acknowledged them, and keeps the corresponding managed record while any event
payload remains. Small identity tombstones remain indefinitely so old spool replays and restart
imports cannot recreate expired events with new IDs; expired event payloads are removed.
With no subscriptions, ordinary retention still applies. An abandoned
consumer can therefore retain events indefinitely. There is no automatic subscription
expiry. Use explicit removal when a consumer is abandoned, as described below.
For old evidence without a valid timestamp, payload retention starts at import; the
source time remains unknown.

Event IDs are scoped to this database's lifetime. Replacing or rolling back the database
can reuse sequence IDs: reconcile or reset external consumer deduplication records as
part of that recovery. Router does not coordinate database restores with consumer effects.
Deduplication also requires one database per producing host identity. Independent
databases sharing a cloned machine ID can emit identical host/event pairs for different
commands. Those pairs are ambiguous: reconcile their identity mappings before handling
or acknowledging across them. Router preserves existing host IDs and does not repair
clones automatically.

New consumer names also receive any backlog retained by an abandoned consumer. Reuse
a small stable set of names. A consumer name does not filter ownership: a returned
`completed` event may describe another command. Match known `managed_id`, `session_id`,
and `agent_id` values before acting on an outcome. Supply known session and agent IDs
when launching (`router run --session SESSION_ID --agent AGENT_ID -- batch-job`) for
explicit attribution. If the command identity is unknown, keep that attribution unknown;
do not treat the oldest event or a matching display label as proof of your command's result.

`router run` sets `ROUTER_MANAGED_ID` in the wrapped command's environment. It matches
that command's completion `event.managed_id`, including when evidence is replayed after
a daemon restart. Each invocation replaces any inherited value with its own new ID;
other environment entries are preserved. A launcher can durably record this ID to match
its own event without importing Router code. An unrecorded run also receives an ID,
but stderr explicitly says that no completion event will follow. The variable alone
does not establish project admission or guarantee event delivery. For a detaching
launcher, the ID still identifies the launcher rather than its detached job.

## Outcomes and deadlines

| Status | Meaning |
| --- | --- |
| `completed` | The wrapped process exited successfully and reported no group survivors. |
| `failed` | The wrapped process exited unsuccessfully or was signalled. Inspect `exit_code`, `signal`, and `group_outlived`. |
| `uncertain` | The wrapper vanished without completion evidence, or the successful process left group survivors or has unknown group state. |
| `deadline` | A valid poll found no pending event before the wait deadline. |
| `unavailable` | The daemon, database, or response transport could not provide a valid result. |

`event.status` preserves the original managed-process state. A vanished wrapper's event
is immutable: late completion evidence creates a second event with a new ID, after the
uncertain event. A failed process can also leave survivors; failure takes precedence in
the top-level status and `group_outlived` preserves that uncertainty.

The timeout must be finite, greater than zero, and at most 3600 seconds.
After subscription admission, database contention, slow responses, and disconnected replies are retried within that
same deadline. Diagnostics go to stderr, leaving stdout as one JSON result. An unreachable
daemon returns `unavailable` immediately. If delivery still fails at the deadline,
the result is `unavailable`; a successful pending poll restores the normal `deadline`
outcome, including when the deadline truncates the last poll. A validated event is
returned even if validation finishes at the deadline. Already-pending events are read
before reconciliation, so SQLite write contention does not block their delivery.
A busy acknowledgment fails with a clear diagnostic: retry the same consumer
and event ID. Ctrl-C during `router wait` prints `unavailable` JSON and an interruption
diagnostic, and exits 130.
A timeout, interrupted consumer, disconnected waiter, or unavailable Router never
cancels, restarts, or duplicates a command. Retry delivery with the same consumer;
do not relaunch a command merely because integration or delivery failed. A command
outside an observed project still runs without a completion event.

`router run` observes its immediate process and reports survivors in that process's
group. Wrapping a detaching launcher observes **the launcher**, not its detached job.
For example, a launcher that starts `batch-job` in a new session and exits may produce
`completed` while `batch-job` continues running. Use a foreground command whose lifetime
matches the work to be observed. Start failures before process creation are reported by
`router run`; they are not evidence that a managed command completed. Missing start
replies can be recovered from the existing completion spool, without launching again.

## Fleet completion delivery

Use the existing fleet configuration to wait once on every configured host, or select
one configured name. Results are JSON without a separate `--json` flag:

```sh
router fleet wait --consumer worker-a --timeout 300
router fleet wait --host worker-b --consumer worker-a --timeout 300 --config fleet.toml
```

Each host independently subscribes to its entire retained local backlog using the same
consumer name. Cursors are not shared between databases. The call returns after every
selected host supplies one event, a wait deadline, or an explicit error; it does not stop
at the first completion. Hosts run concurrently, in configured output order, with at
most 32 selected hosts. A host wait lasts up to `--timeout` seconds; SSH collection adds
at most the configured `ssh_timeout_seconds` for connection, remote CLI startup, and
reply delivery, plus bounded process cleanup (up to 1.5 seconds). Use a tool timeout longer than that budget.
If connection/startup/reply overhead exhausts that configured margin, the result is an
explicit transport deadline, even if the remote wait was still healthy. Increase the
configured margin for slower transports. There is no automatic SSH retry or remote
command fallback.

The envelope contains `schema_version: 1`, `consumer`, `hosts`, and `partial`. Each host
wrapper has its configured `name`, serving `host_id` (or `null` when unknown), and `ok`.
A successful wrapper contains `result` with the local wait contract plus `subscription_id`.
Fleet waits use `router wait --include-subscription`, which adds the persisted subscription
binding and serving `host_id` once admission succeeds, including on a normal `deadline`.
The default local wait output remains unchanged. A normal `deadline` is successful
delivery with no event. `failed` and
`uncertain` are delivered command outcomes, also with `ok: true`. An unavailable host
has `ok: false` and an `error` object; it is never an empty successful response. Any
failed host makes the envelope `partial: true`, independently of the fleet `required`
flag. Known duplicate serving IDs among selected results fail those wrappers with
`duplicate_host_id`; unknown IDs remain unknown. This check covers only hosts selected
in one call. Callers must maintain consistent identity mappings across separate calls;
`--host` selects a route and cannot resolve ambiguous identities.

A configured name routes a request; it is not a machine identity. The top-level result's
`host_id` identifies the serving daemon. `result.event.host_id` identifies the producing
host and may differ after a state directory moves. Preserve both IDs along with the
consumer, subscription ID, and event ID when durably handling an event. Deduplicate
effects using the producing host and event IDs under the database namespace requirements
above. A subscription ID fences an acknowledgment to one consumer incarnation; it is
not a stable identity for consumer effects and changes after removal and resubscription.
After recording handling, acknowledge that exact tuple:

```sh
router fleet ack --host worker-b --host-id serving-host --event-host-id producing-host \
  --consumer worker-a --subscription-id subscription-token completion-1
```

`ack` returns one host wrapper, with the acknowledgment under `result` on success.
The daemon checks the expected serving ID before opening a write transaction and
checks the stored producing ID and consumer subscription ID within the acknowledgment
transaction, before advancing the cursor. This fences a replacement subscription or
another database even when hosts share a machine ID. Host IDs are preserved; Router
neither repairs machine-ID clones nor distinguishes copies of the same database and
subscription state. Local `router ack` also accepts `--host-id` and `--event-host-id`
together, with optional `--subscription-id`, for these checks; its existing syntax and
output stay valid. A confirmed bound acknowledgment echoes `subscription_id`.
Older Router installations that lack these flags refuse the request.

A failed or interrupted acknowledgment has an unknown commit outcome. Failure wrappers
include `error.acknowledgment: "unknown"`. Retry the same configured host, consumer,
serving ID, producing ID, subscription ID, and event ID. Router makes no automatic
acknowledgment retry. If the remote CLI refuses the request (`exit_failed` with code 1),
the wrapper cannot distinguish database contention from a permanent refusal. After
durably recording handling, run the same bound acknowledgment directly on that host
to inspect its stderr; this is idempotent. Blindly repeating a permanent identity mismatch
cannot repair it. Restore or verify changed host mappings. After explicit consumer removal,
a new wait receives a new subscription ID; verify that the event matches durable handling
before acknowledging under that new binding. Waits do not
acknowledge. Restart, dropped SSH replies, or killed wait clients replay unacknowledged
events on reconnection without rerunning the managed command. Ctrl-C stops local wait
readers and owned SSH process groups; it never signals the managed command. After a
transport disconnect, a remote wait process may continue until its own bounded deadline;
local reader cleanup does not prove remote reader termination.

Only known completion metadata is projected from remote replies. Unknown fields are
dropped. Invalid optional managed/session/agent IDs, attribution, or timestamps are
omitted with `warning: {"code":"invalid_metadata","fields":[...]}` on the host wrapper;
that event remains deliverable with `ok: true`, and the envelope is visibly `partial`.
Do not infer missing attribution. Invalid event identities, subscription bindings,
process status/exit evidence, contradictory outcomes, and oversized replies fail that
host. A partial envelope may therefore contain events that can be durably handled and
acknowledged. Each accepted reply
is at most 16 KiB before projection; SSH stdout collection is capped at one MiB, stderr
at 64 KiB, and both streams are drained under one deadline. Free-form remote stderr,
SSH targets, command contents, and environment values are not forwarded in JSON errors.
Host-key verification, SSH configuration, and credentials use the existing SSH path;
Router adds no trust bypass and never starts a remote daemon.

| Error code | Meaning |
| --- | --- |
| `unavailable` | The daemon cannot supply a wait result or the local socket is unreachable. |
| `database_busy` | A local acknowledgment encountered database contention; retry the same tuple. |
| `request_failed` | A local delivery request was refused or could not provide usable evidence. |
| `invalid_response` | Missing, malformed, inconsistent, or oversized reply. |
| `output_limit` | SSH stdout exceeded its one-MiB collection limit. Replies above the separate 16-KiB acceptance limit yield `invalid_response`. |
| `invalid_metadata` (warning) | Optional metadata omitted; the event remains available. |
| `spawn_failed` | SSH could not start; `errno` is included when known. |
| `exit_failed` | SSH or the remote CLI exited unsuccessfully; `exit_code` is included. |
| `deadline` | SSH collection exceeded its overall budget; the managed command is unaffected and acknowledgment outcome is unknown. |
| `transport_deadline` | A local acknowledgment reply exceeded its deadline. |
| `transport_failed` | The transport failed; local and SSH reader OS errors include numeric `errno` when known. |
| `cleanup_failed` | The SSH reader did not exit after collection. |
| `interrupted` | An owned SSH reader was interrupted during collection. |
| `duplicate_host_id` | More than one selected result claims the same serving host. |

| Exit code | Fleet wait and acknowledgment |
| --- | --- |
| 0 | All selected waits delivered an outcome/deadline, or acknowledgment was confirmed. |
| 1 | Partial/degraded/unavailable delivery (JSON emitted), or invalid fleet configuration (stderr). |
| 2 | Invalid completion arguments, host selection, or CLI syntax; stderr, no result JSON. |
| 130 | Interrupted; no complete JSON result is promised, acknowledgment commit state unknown. |

Separate-process simulated hosts exercise distinct state directories, real daemon
sockets and CLI readers, replay after restart and lost replies, and host-routing
mismatches. Component tests cover subscription fences and malformed evidence;
local SSH-reader fixtures cover failed-reader descendants. Interruption during an
already-running fleet collection is **NOT CHECKED** by these tests.
Physical SSH, host-key negotiation, independent machine reboot, and the
two-host live smoke test are **NOT CHECKED** by these tests.

## Completion CLI exit codes

This table applies to `wait` and `ack`; other commands keep their existing exit behavior.

| Exit code | `router wait` | `router ack` |
| --- | --- | --- |
| 0 | One JSON result: `completed`, `failed`, `uncertain`, or `deadline`. | Acknowledgment confirmed by JSON containing `acknowledged: true`. |
| 1 | `unavailable` JSON, with a diagnostic on stderr. | Delivery failed or the daemon refused the acknowledgment; diagnostic on stderr. |
| 2 | Invalid consumer, timeout, or command-line syntax; stderr, no result JSON. | Invalid consumer, event ID syntax, or command-line syntax; stderr, no result JSON. |
| 130 | Interrupted while waiting: `unavailable` JSON and an interruption diagnostic. | Interrupted: commit state is unknown; diagnostic on stderr, no reliable result JSON. |

An interrupted acknowledgment may already have committed, or may commit after the
waiter disconnects. Do not infer rollback from exit 130 or empty stdout. Retry the
same consumer and event ID; idempotent acknowledgment cannot skip the next pending
event. The consumer must durably record its handling before any acknowledgment attempt.

## Managing consumers

Inspect subscriptions before removing an abandoned name:

```sh
router consumer list
router consumer list --limit 100 --offset 100
router consumer remove worker-a
```

`list` returns compact JSON containing `consumers`, `total`, `limit`, `offset`, and
`host_id`. Each consumer has its exact `name`, acknowledged `cursor`, and `pending`
event count. The list is ordered by name. Pages default to 100 entries; `limit` must
be 1–1000 and `offset` nonnegative. Counts and rows share one database snapshot.
Consumer names are identities and are never inferred from command labels.

`remove` returns `{"consumer":"worker-a","removed":true,"host_id":"..."}` even
when the name was already absent. It changes no other consumer's cursor and deletes
no command or completion evidence directly. Retention resumes on its next ordinary
pass for old events no remaining consumer needs. Removing the last consumer permits
ordinary seven-day retention for the retained backlog.

Current waiters bind to a persisted subscription identity. Removing that subscription
ends those waits with `unavailable` and a diagnostic on stderr. A newly invoked wait
with the same name creates a fresh subscription at the retained backlog; expired
payloads do not return, and old waiters cannot attach to the replacement. An in-flight
result read before removal may still arrive and is not an acknowledgment. Acknowledgments
remain explicitly addressed by consumer name and event ID. Stop a consumer that will
automatically invoke new waits before removing its subscription. Wait CLIs predating
consumer management must also be stopped: their legacy polling requests can subscribe
again. Daemon restarts preserve existing subscription identities and cursors.

A lost initial subscription reply returns `unavailable` because registration may have
committed; it is not automatically repeated across a possible removal. Retry a new wait
with the same name. Confirmed database contention during admission remains retryable.
Once joined, transient completion reads retry within the original deadline as before.
Existing local `wait` and `ack` syntax and output remain valid; optional binding flags
and bound acknowledgment output are described above.

There is no idle expiry. An intentionally offline consumer is indistinguishable from an
abandoned one; expiring it silently could prune unacknowledged evidence needed after a
restart. Bounded inspection and explicit, idempotent removal provide operator control
without weakening durable replay for consumers that remain subscribed.
