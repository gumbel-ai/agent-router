# Milestone B review evidence

## Architecture

Milestone B adds two read-only sources of process visibility:

- `router run` starts a foreground command behind a gated supervisor. The command is released only after the daemon has durably registered its PID, process group, creation identities, attribution, and privacy-bounded metadata.
- The process monitor samples structured kernel process data every five seconds, reconciles managed groups, and infers session descendants only from recorded Claude parent identities. Managed worker usage is aggregated into the managed record.

The supervisor owns the command exit status and writes a versioned completion document atomically. The daemon owns observation state. A completion becomes final only when the leader and every verified descendant are gone; ambiguous survivors remain `unknown` with shutdown uncertainty.

Configured coordinators may register a live process through `router process register`, but the daemon assigns `host` attribution and rejects caller-supplied session or agent ownership. No command-text parsing is used for attribution.

Deviations and explicit limitations:

- A process that detaches before any complete sample observes its identity cannot be linked later.
- Leaderless numeric process-group matches are treated as recent uncertainty, not as proven ownership. They are not hidden from session inference.
- Per-process CPU and RSS are best-effort kernel samples. Temporarily unreadable known descendants preserve uncertainty rather than producing a false stopped state.

## Changed files

- `README.md`
- `MILESTONE_B_REVIEW.md`
- `examples/router.env.example`
- `router/capacity.py`
- `router/cli.py`
- `router/config.py`
- `router/daemon.py`
- `router/db.py`
- `router/fleet.py`
- `router/fleet_snapshot.py`
- `router/managed.py`
- `router/managed_completion.py`
- `router/managed_gate.py`
- `router/observer_hook.py`
- `router/plugin/README.md`
- `router/plugin/skills/agent-router/SKILL.md`
- `router/processes.py`
- `router/redaction.py`
- `router/sessions.py`
- `tests/test_config.py`
- `tests/test_fleet_monitor.py`
- `tests/test_hooks.py`
- `tests/test_managed.py`
- `tests/test_processes.py`
- `tests/test_transport.py`

## Persistence and retention

The upgrade adds one additive SQLite table, `managed_processes`, plus indexes for active/recent fleet reads, terminal retention, and session-root inference. Existing Milestone A tables and records are not rewritten. Terminal and non-live uncertain history are each age- and count-bounded; verified live descendants remain visible until their state can be resolved.

Rollback is code-only: stop Router, install the preceding release, and restart it. The preceding release ignores the additive table and indexes. Retain the database if returning to Milestone B is possible; deleting the table is unnecessary and would discard process evidence.

## JSON examples

Managed process:

```json
{
  "managed_process_id": "managed-8f3d",
  "host_id": "host-a",
  "boot_id": "boot-a",
  "pid": 4102,
  "pgid": 4101,
  "process_start_time": "928112",
  "creation_identity": "linux:boot-a:928112",
  "group_creation_identity": "linux:boot-a:928110",
  "label": "freeze suite",
  "session_id": "session-1",
  "agent_id": "agent-2",
  "attribution": "exact",
  "executable": "pytest",
  "argument_count": 3,
  "argument_hash": "d8f6f03a2026e953c5f52edb2e7e37b8fb6b881f7b6d83058f0d249f31d5bd8a",
  "cwd": "/work/project",
  "environment_allowlist": {},
  "status": "running",
  "registered_at": 1792180000000,
  "started_at": 1792180000000,
  "last_sample_at": 1792180005000,
  "finished_at": null,
  "exit_status": null,
  "signal": null,
  "elapsed_ms": 5000,
  "cpu_pct": 81.0,
  "rss_bytes": 8100,
  "peak_rss_bytes": 8100,
  "worker_count": 1,
  "shutdown_uncertain": false,
  "shutdown_was_uncertain": false,
  "descendant_tracking_truncated": false,
  "visibility_reason": null
}
```

Inferred process:

```json
{
  "host_id": "host-a",
  "boot_id": "boot-a",
  "pid": 4200,
  "pgid": 4190,
  "process_start_time": "928300",
  "creation_identity": "linux:boot-a:928300",
  "session_id": "session-1",
  "agent_id": null,
  "attribution": "session",
  "executable": "python",
  "status": "running",
  "observed_at": 1792180005000,
  "cpu_pct": 12.5,
  "rss_bytes": 64000000
}
```

## Verification

- Full automated suite on supported Python 3.14.8: 186 tests and 42 subtests passed in 50.32 seconds.
- Compatibility verification on Python 3.13.3: 186 tests and 42 subtests passed in 50.77 seconds.
- SQLite-contention regression: 30 consecutive runs passed after removing the redundant startup sample.
- Ruff (`uv tool run ruff check router tests`): passed.
- Whitespace/error-marker check (`git diff --check`): passed.
- macOS sampler smoke: successful.
- Measured sampler with 462 sampled processes and 10,000 dormant uncertain records: 13.44 ms average; 13.524 ms maximum; about 0.269% of one CPU at a five-second interval.
- Measured snapshot composition: 2.474 ms, 1,296 bytes for the smoke fixture.
- Measured SessionStart identity lookup: 0.022 ms average, 0.195 ms maximum.
- Measured 50,000-row stopped-session inference query: 1.733 ms.
- Measured 10,000-row terminal managed-process fleet query: 6.30 ms average; 6.62 ms maximum.
- Measured 10,000-row recent uncertain managed-process fleet query: 18.941 ms average; 19.658 ms maximum.

The Python 3.14 verification used a clean virtual environment with Python 3.14.8 and Pydantic 2.14.1. The repository's older local `.venv` still targets Python 3.14.0a6 and should be rebuilt separately before reuse.

## Privacy audit

- Full command arguments are neither persisted nor serialized. Snapshots retain executable name, argument count, and a domain-separated SHA-256 hash.
- Environment values are never accepted. Only an empty value or approved variable names can be recorded.
- Labels, executable names, working directories, sampler errors, and remote display strings pass through bounded redaction.
- Completion files accept an exact schema, reject symlinks/non-regular files, and are size-bounded.
- Remote process records reject unknown fields so a peer cannot smuggle arguments or environment values into fleet output.

## Live verification status

The required two-host live smoke test remains pending. It must cover managed start/activity/completion, a longer worker process, host disconnect, daemon restart uncertainty, and inspection confirming that no prompt body or secret environment value appears. The canonical research host must not be used for the destructive/failure portion.

## Control boundary

Milestone B adds observation and foreground launch only. It adds no fleet scheduling, cancellation, pause, kill, merge, or remote repository mutation capability.
