# Agent Router

Delegate coding tasks to another agent while the parent stays in control.

## What you get

- Delegate across agent CLIs: Claude Code to OpenCode or Codex.
- Choose the exact child model, with no silent substitution.
- Run tasks in parallel while the parent keeps working.
- Watch native Claude subagents and Router-launched children across projects with `router status --watch`.
- View configured macOS and Linux hosts, Claude sessions, subagents, and available capacity with `router fleet status --watch`.
- Give each child its own Git worktree and dependencies, not a security sandbox.
- Start from your current files, including uncommitted changes.
- Let the parent inspect, request corrections, accept, or discard the work.
- Continue corrections in the same child session, preserving context.
- Track tasks durably and prevent duplicate launches on request retries.
- See who started each agent, its model, elapsed time, activity, tokens, and estimated cost.
- Set limits on concurrency, runtime, and correction attempts.
- Preserve partial work when a child fails or times out.
- Integrate reviewed changes explicitly and stop on patch conflicts.
- Clean up child worktrees while retaining execution and review history.

## Install

Requires Git, uv, and Claude Code 2.1.292+ (sign in to run agents). Automatic background-service setup supports macOS. Python 3.14 is installed by uv if needed. Linux supports manual daemon startup but needs live-host verification; Windows is unsupported.

```sh
uvx --python 3.14 --from git+https://github.com/gumbel-ai/agent-router router setup
```

Approve the one-time user-scoped plugin and background-service installation. Then use ordinary `claude` in any project; no launch flags or project setup are needed for native visibility. Restart already-open Claude sessions. Watch all connected projects in another terminal:

```sh
router status --watch
```

For external delegation only, install OpenCode and configure its provider access, then run `router init` in each target Git repository. Confirm the exact model, child dependency setup command, explicit OpenCode permissions, and global limits. Init records setup; it does not run it. Keep dependencies and credentials untracked and ignored; setup must not launch detached processes. [Advanced environment configuration](examples/router.env.example) remains supported.

For Codex instead, install and sign in to the Codex CLI, then run `router init --executor codex`. Confirm an exact `alias=openai/model-id` and a `read-only` or `workspace-write` sandbox. Router uses the CLI's saved login and ignores Router provider-key/endpoint overrides; it does not create or request an API key. The integration targets CLI 0.162.1. Codex children do not spawn native subagents in this version. Cost is a labelled token estimate, including reported cache writes; unreported charges are excluded and missing data stays unknown. Dependency setup runs outside the Codex sandbox.

`router service stop` stops the idle managed service; `router service start` starts it again and lets it recover interrupted work. `router uninstall` removes the plugin, service, and CLI while retaining configuration and history. Installation/configuration changes refuse active Router work or pending cleanup; failed cleanup stays visible without blocking maintenance. Native subagents remain Claude-owned.

If a Codex shutdown stays unconfirmed, an operator must inspect and stop its remaining commands. Stop the daemon with `router daemon stop`, then run `router recover TASK_ID --reason 'what you inspected'` in an interactive terminal. Recovery requires the recorded process group to be stopped and a typed acknowledgement; it releases the slot but preserves uncertainty evidence and the worktree. Restart the service afterward. Agents must not perform recovery.

The plugin is also installable through Claude's normal marketplace command after the Router runtime is installed:

```sh
claude plugin install agent-router --marketplace gumbel-ai/agent-router --scope user
```

## Fleet monitoring

Fleet monitoring is read-only and independent of external delegation. Each host keeps its own lifecycle history and capacity sample; the viewing machine pulls bounded JSON snapshots concurrently through the user's existing SSH configuration. An unavailable host is shown as offline and does not hide healthy hosts.

Create `~/.agent-router/fleet.toml` on the viewing machine:

```toml
schema_version = 1
ssh_timeout_seconds = 3
refresh_seconds = 2

[capacity_defaults]
minimum_available_memory_gib = 4
maximum_cpu_busy_for_available_pct = 65
maximum_load_per_logical_cpu_for_available = 0.75
minimum_disk_available_gib = 10

[[host]]
name = "laptop"
transport = "local"

[[host]]
name = "ryzen"
transport = "ssh"
target = "ryzen"
tags = ["numeric", "canonical-research-host"]
minimum_available_memory_gib = 12
```

Run `router snapshot --json` for the local bounded schema, `router fleet status --watch` for the combined table, or `router fleet status --json` for machine-readable output. Combining `--watch --json` emits one compact JSON document per line. `router fleet capacity` shows only capacity summaries, and `router fleet host ryzen --json` returns one configured host. Add `--strict` when automation must fail if any host marked `required = true` is unavailable.

The fleet command invokes only `router snapshot --json` remotely. It does not copy SQLite files, change SSH configuration, schedule work, kill processes, or write project state. Capacity states are summaries over explicit CPU, load, memory, and disk thresholds; inspect the accompanying raw measurements and reason list for job-specific decisions.

Router samples known Claude process trees and displays their current CPU and memory use with `session` attribution. Use the foreground managed-command wrapper when exact session-and-agent attribution is required:

```sh
router run --label freeze-suite --session "$CLAUDE_SESSION_ID" --agent "$CLAUDE_AGENT_ID" -- pytest tests/freeze
```

The wrapper preserves the command's arguments and inherited terminal I/O, runs it in a distinct process group, and returns its shell-compatible exit status. Registration is durable before the command begins. Snapshots retain the executable name, argument count and hash, working directory, completion status, elapsed time, aggregate process-group CPU/RSS, and worker count; they do not retain full arguments or environment values. A wrapper exit is not treated as proof that its descendants stopped, and uncertain shutdown remains visible. Omitting both attribution flags records host-level attribution; `--agent` requires `--session`, and exact attribution requires both. Terminal evidence is retained for seven days, capped at the latest 10,000 completed records per host; completion files are removed after their evidence is durably committed to SQLite.

Process monitoring is observational only: it adds no fleet cancellation or kill operation. Inferred descendants never receive agent attribution. Processes that detach before any sample can observe them remain an explicit visibility limitation. A coordinator can explicitly register an already-running configured project process with `router process register --json JSON`. The version-1 object requires `request_version`, `managed_process_id`, `label`, PID and PGID, process and group creation identities, process start time, executable, argument count and SHA-256 hash, working directory, an empty or approved-name environment allowlist, and wall-clock `started_at`. Router verifies the live identities and assigns host attribution; coordinator registration cannot claim session or agent ownership. It does not guess ownership from command text or process names. When an externally registered process exits without trusted completion evidence, the default snapshot shows the uncertainty for five minutes and SQLite retains the evidence for seven days.

```json
{"request_version":"1","managed_process_id":"configured-build-1","label":"project build","pid":4102,"pgid":4102,"process_start_time":"928112","creation_identity":"linux:BOOT_ID:928112","group_creation_identity":"linux:BOOT_ID:928112","executable":"pytest","argument_count":2,"argument_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","cwd":"/work/project","environment_allowlist":{},"started_at":1792180000000}
```

`argument_count` excludes the executable. `argument_hash` is lowercase SHA-256 over the bytes `agent-router-managed-argv-v1\0` followed by canonical UTF-8 JSON for `{"arguments": ARGV_WITHOUT_EXECUTABLE}` (sorted keys, compact separators, and no NaN values). `managed_process_id` is 1–128 ASCII characters, begins with an alphanumeric character, and otherwise contains only alphanumerics, `.`, `_`, or `-`. Other IDs and creation identities are non-empty bounded strings; PID, PGID, argument count, and timestamps are non-negative JSON integers, with PID and PGID strictly positive. Approved environment-name entries are `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, and `TZ`; values are never accepted.

Configured registration always follows the registered PID's descendant tree. Router includes other members of the supplied process group only when the registered PID is that group's leader (`pid == pgid`); a shared shell process group does not make sibling jobs part of the configured process.

An external coordinator can reserve a host without granting Router any control authority by atomically writing `~/.agent-router/reservation.json` on that host:

```json
{
  "schema_version": 1,
  "reserved_by": "research-scheduler",
  "reserved_until": 1792173600000,
  "reserved_cpu_slots": 8,
  "reserved_memory_bytes": 34359738368,
  "reservation_source": "experiment-queue"
}
```

`reserved_until` is a Unix timestamp in milliseconds. Router only reads this file. An active reservation prevents the host from being classified as `available`; a reservation consuming every logical CPU or leaving critically little unreserved memory makes it `saturated`. Missing files mean no reservation, while malformed files mark capacity `unknown` and the snapshot partial. Writers should replace the file atomically so readers never observe a partial record.

License: [MIT](LICENSE).
