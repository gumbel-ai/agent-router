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
different serving host. Deduplicate using the event's host and event IDs. Malformed
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
Consumers should deduplicate durable handling using `(host_id, event_id)`. Delivery is
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

New consumer names also receive any backlog retained by an abandoned consumer. Reuse
a small stable set of names. A consumer name does not filter ownership: a returned
`completed` event may describe another command. Match known `managed_id`, `session_id`,
and `agent_id` values before acting on an outcome. Supply known session and agent IDs
when launching (`router run --session SESSION_ID --agent AGENT_ID -- batch-job`) for
explicit attribution. If the command identity is unknown, keep that attribution unknown;
do not treat the oldest event or a matching display label as proof of your command's result.

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

This interface is local. Fleet waiting and acknowledgment are separate capabilities.

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
The public `wait` and `ack` JSON results and their CLI syntax are unchanged.

There is no idle expiry. An intentionally offline consumer is indistinguishable from an
abandoned one; expiring it silently could prune unacknowledged evidence needed after a
restart. Bounded inspection and explicit, idempotent removal provide operator control
without weakening durable replay for consumers that remain subscribed.
