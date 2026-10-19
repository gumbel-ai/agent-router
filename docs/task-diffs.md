# Retained task patches

Read one exact Router task attempt without changing the task:

```sh
router task-diff task-example --attempt 1 --json
router fleet task-diff task-example --attempt 1 --host worker-a --host-id HOST_ID --json
```

Fleet retrieval requires one configured host and its actual Router host ID, obtained
from `router fleet host worker-a --json`. A configured host name is a routing label,
not an identity. Router preserves its existing host IDs and SSH host-key and credential
boundaries. The remote host needs a Router version supporting `task-diff`; missing or
broken integration returns an error and never launches a replacement command.

The request selects a task ID and positive attempt number explicitly. There is no
latest-attempt selection, directory scan, arbitrary file path, or fleet fan-out. A
saved, stopped capture is required. A running or uncertain current attempt is refused
even if an older saved frame exists for that number. Earlier stopped captured attempts
remain readable while a later attempt runs, if their artifacts remain retained.

To bind a previously inspected capture, add any known `--snapshot-commit`,
`--snapshot-tree`, and `--final-tree` values. A supplied value must match exactly.
Object IDs are nonzero lowercase Git SHA-1 or SHA-256 IDs, with one object format per
receipt. A missing task, attempt, capture context, or patch is an explicit error.

## JSON contract

A successful schema-version-1 receipt contains only:

```json
{
  "schema_version": 1,
  "status": "ok",
  "host_id": "host-a",
  "task_id": "task-example",
  "attempt_number": 1,
  "snapshot_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "snapshot_tree": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "final_tree": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "byte_count": 0,
  "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "patch_base64": ""
}
```

The example is an empty patch. The object IDs illustrate the receipt shape. Router
returns the producing snapshot-to-final `git diff --binary` bytes unchanged, including
binary changes. The maximum raw patch is 256 KiB; oversized patches are refused rather
than truncated. Empty bytes require identical snapshot and final tree IDs, and identical
tree IDs require empty bytes; contradictory capture evidence is refused. Strict base64,
decoded length, SHA-256, identity, version, field allowlist
and 512 KiB wire bounds are checked on local and fleet reads. Repeated reads may safely
retrieve the same retained artifact; the digest describes the bytes actually returned.
Local replies must also have a complete newline-terminated daemon envelope, strict
boolean `ok`, and exactly the matching `result` or `error` field. Duplicate JSON keys,
nonfinite values, excessive nesting and trailing data are refused before parsing loses
that evidence. The raw-byte bound includes the local envelope and its terminator.

An unavailable receipt has `schema_version`, `status: "unavailable"`, `host_id`,
`task_id`, `attempt_number`, and `error: {"code": "..."}`. The serving host ID is null
when transport failed before it could be established. An identity-mismatch receipt may
report the different actual serving host without returning any patch. Error codes are:

| Codes | Meaning |
| --- | --- |
| `task_unknown`, `attempt_unknown` | The exact retained selection does not exist. |
| `attempt_active`, `state_unknown` | The selected execution is active or not known stopped. |
| `capture_unavailable` | Retained task/frame capture context is absent or inconsistent. |
| `artifact_missing`, `artifact_unavailable` | The known patch is missing or could not be read. |
| `artifact_unsafe` | A path, symlink, or nonregular file violates the owned-artifact boundary. |
| `artifact_unstable` | The opened patch changed or was replaced during the read. |
| `artifact_oversize` | The patch exceeds the raw-byte limit. |
| `identity_mismatch`, `invalid_response` | Expected identity differs, or the receipt is invalid. |
| `request_failed`, `transport_unavailable`, `transport_deadline` | The local daemon request failed or timed out. |
| `ssh_spawn_failed`, `ssh_exit_failed`, `ssh_deadline`, `ssh_output_limit`, `ssh_transport_failed`, `ssh_interrupted`, `ssh_cleanup_failed` | The existing bounded SSH reader failed in the named phase. |

Invalid arguments produce a diagnostic on stderr. Other unsuccessful reads produce a
bounded unavailable JSON receipt when possible. A caller interrupt can end before a
receipt exists.

| Exit code | Meaning |
| --- | --- |
| 0 | A complete, validated patch receipt, including a valid empty patch. |
| 1 | Unavailable artifact/transport or invalid configuration/response. |
| 2 | Invalid task-diff arguments or unknown selected configured host. |
| 130 | Read interrupted; repeat the same read if still needed. |

## Content and ownership

This command intentionally exports code content. Patch bytes may contain any tracked
or captured Git-visible file contents; Router does not claim those contents are
secret-free. Receipt metadata contains no task instructions, prompts, responses,
transcripts, tool contents, environment, credentials, or local artifact paths. Keep
these explicit artifact receipts separate from content-free correlation exports and
fleet monitoring. Base64 encoding is transport encoding, not redaction or encryption.

Router reads only its known `tasks/TASK/attempt-N.patch`, checks the retained frame and
current task in one database read snapshot, rejects symlink/nonregular artifacts, and
checks descriptor and path identity/size/timestamps around a bounded read. Missing
legacy capture context is refused; paths are never guessed. The selected host is
authoritative for its retained artifact under the existing same-account access boundary.
A digest is not independent source attestation or proof that a patch is safe to apply.

Retrieval does not grant execution, review, acceptance, application, cancellation,
acknowledgment, or cleanup. A timeout, disconnection, or interrupt changes none of those
states. It never retries or falls back to another host, executor, or command. Managed
completion `wait`/`ack` remains a separate contract and does not represent Router child
task completion. Remote child dispatch and durable patch application remain separate
work.
