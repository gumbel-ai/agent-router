# Deterministic task checks

The host authorization hook and daemon share `router.lifecycle.validate`. Invalid
start, review, close and cancel schemas are refused before authorization is stored.
The hook checks locally before opening the Router socket. Session ownership,
current instructions, attempt numbers and state transitions retain their existing
checks. Requests are never repaired or rewritten automatically.

Codex and Claude starts may declare two optional objects:

```json
{
  "launch": {
    "task_packets": ["tasks/example.md"],
    "writable_paths": ["results/example.json"],
    "requires_router": false
  },
  "completion": {
    "files": ["results/example.json"],
    "json_files": ["results/example.json"],
    "final_response": true,
    "setup_status_file": "results/setup.json"
  }
}
```

These are fields in the ordinary start payload, alongside its existing required
fields. No extra preflight or completion command is needed. Paths are relative to
the child worktree, have no `..`, and have at most 32 components. Each path list
has at most 32 entries. Declare only requirements the task actually has. Leave
optional files out of these lists; absence of a completion object retains the
existing behavior. Contracts currently support Codex and Claude only; other
executors reject them explicitly.

When a start declares `launch`, preflight runs after dependency setup and
immediately before each child process, including resumed attempts. It checks the actual cwd, executable and
environment, declared packet readability and declared output writability. Setup
may create packets or install the executable. Required files can already exist;
only `launch.writable_paths` requires write capability. Proven incompatible
read-only Codex or non-writing Claude profiles are refused. Unknown native
permission rules and missing authentication files are not proof of failure.
Authentication files and provider APIs are never read by these checks.

`requires_router` makes a bounded local Unix-socket connection check necessary;
it sends no request and consumes no authorization. Timeouts remain uncertain.
This checks socket accessibility from the launcher; it does not prove that a
future native sandbox will permit every operation. An optional observer does
not make Router mandatory. Existing repository, exact configured model-alias,
permission-profile and tracing checks continue in the normal runtime validator.

Completion checks run after stopped execution and final-tree capture, including
failed attempts. They inspect only explicitly declared paths in the stopped
worktree, including ignored output files. They do not follow symlinks, wait on
FIFOs, recursively scan, hash files, or infer success from prose or tool counts.
JSON reads are bounded to 1 MiB per declared file. A file changing during a JSON
read or exceeding that limit is UNKNOWN. A setup-status file uses the explicit
JSON values `{"status":"READY"}` or `{"status":"SETUP_FAILED"}`; other values
remain UNKNOWN. Ordinary process failures and timeouts retain their existing
failure handling.

The task exposes `completion_check` with `satisfied`, `violated` or `unknown`,
violation details and unchecked requirements. `satisfied` describes mechanical
requirements only. For a declared contract, `violated` and `unknown` both block
acceptance, including missing or malformed remote evidence, and leave the existing
bounded review/rejected repair path available. Failed tasks with satisfied mechanical
requirements retain the existing independent salvage review. New attempts
clear earlier diagnostics; remote task projections carry bounded diagnostics.

These are point-in-time checks of the stopped worktree, not immutable artifact
retention or semantic review. Retain required outputs through review. Existing
snapshot integrity, independent review, authorization and scientific gates still
apply. No check accepts or integrates work automatically.

An older serving Router rejects new contract fields. Deploy compatible receiving
code before using contracts remotely; the unchanged `0.4.0` version string does
not prove support. This change adds no wait, polling, scheduling or notification
infrastructure.
