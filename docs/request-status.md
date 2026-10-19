# Recovering a request after a lost reply

`router request-status` reads the durable binding between a parent session, its
request ID and a Router task. It never submits, authorizes, restarts or cancels work.
Use the original identities after a disconnected mutation call:

```sh
router request-status --session session-a --request-id request-a --json
```

An accepted request returns:

```json
{"schema_version":1,"host_id":"host-a","session_id":"session-a","request_id":"request-a","status":"found","operation":"start","task_id":"task-a"}
```

The operation can be `start`, `review`, `cancel` or `close`. The receipt identifies
the original operation and task, not its current execution state. Follow up with
`router status task-a --json`. It does not expose the invocation, instructions,
responses, local paths or credentials. Request and session IDs are printable text
without surrounding whitespace, at most 240 characters; host and task IDs are at
most 128 characters. Labels are never used as identities.

The binding uses the existing operations table and survives daemon and client
restart. A start registers its task and request binding in one transaction. Reading
uses one database read snapshot and does not consume a new authorization. It works
after the original submission has been superseded.

## Selected fleet host

```sh
router fleet request-status --host worker-a --host-id host-a \
  --session session-a --request-id request-a --json
```

`--host` must select exactly one configured local or SSH host. `--host-id` is the
expected existing Router host ID, obtainable from that host's snapshot. The SSH
path uses the existing configured target, credentials and host-key policy; it never
starts a daemon or tries another host. The same session/request pair on a different
host may identify different work. A host mismatch returns `unknown` with
`identity_mismatch`, including the actual serving host ID.

This read is a prerequisite for remote-child recovery. It does not yet provide
remote task submission, snapshot transfer or integration. Retained patches are
available separately through [task-diff](task-diffs.md).

## Outcomes and exit codes

| Status | Meaning | Exit code |
| --- | --- | --- |
| `found` | This host durably recorded the operation and task binding. | 0 |
| `not_found` | No committed binding was visible in this host's read snapshot. | 0 |
| `unknown` | Transport, identity, response or stored-binding validation failed. | 1 |
| No JSON | Invalid arguments or unknown configured host. | 2 |
| No JSON | Read interrupted; no mutation was attempted. | 130 |

**Neither `not_found` nor `unknown` authorizes a new start.** The original mutation
may still be in flight when the read occurs. Preserve its request and session IDs;
repeat the read on the same host instead of generating another request, retrying
the command or falling back locally.

Every result contains schema version 1, status, host ID, session ID and request ID.
`found` adds only operation and task ID. `not_found` adds no fields. `unknown` adds
`error: {"code": "..."}`; its host ID is null when a serving identity could not be
validated. Unknown fields, duplicate JSON fields, truncated replies, trailing
messages and responses over 16 KiB are rejected. Local reads have a 15-second
overall deadline, including response EOF. Fleet reads use the configured SSH
timeout. No transport failure is represented as an empty successful response.

Error codes distinguish `identity_mismatch`, `binding_invalid`, `invalid_response`,
`request_failed`, `transport_unavailable`, `transport_deadline` and `ssh_*` failures
(`spawn_failed`, `exit_failed`, `deadline`, `output_limit`, `transport_failed`,
`interrupted`, `cleanup_failed`). The CLI prints the code on stderr; raw remote
diagnostics are not forwarded. A corrupt or orphaned binding is `unknown`, not
`not_found`. Existing commands and their JSON remain unchanged.

## Verification outside CI

Automated tests use independent local processes and state directories with an
emulated SSH command. Physical SSH and independent reboot are **NOT CHECKED**.
An operator with access should record the exact Router commit on both machines,
use a disposable `example-project`, and:

1. Record a normally authorized task's original parent session ID, request ID and
   task ID, and the remote Router host ID. Do not submit the task again.
2. Run the fleet read above from the other machine. Check the exact host, operation
   and task identities. Restart the serving daemon and repeat the same read.
3. Disconnect that host. Confirm exit 1 and `unknown`, then reconnect and confirm
   the original binding returns, with no additional task.
4. Supply a different expected host ID. Confirm `identity_mismatch` and no mutation.
5. Only if separately authorized, reboot the serving machine and repeat the read.

These checks validate request lookup. They do not validate remote execution,
automatic snapshot shipping, native provider behavior or child tracing.
