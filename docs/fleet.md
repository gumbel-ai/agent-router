# Fleet monitoring

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
name = "worker-a"
transport = "ssh"
target = "worker-a"
tags = ["batch-worker"]
minimum_available_memory_gib = 12
```

Run `router snapshot --json` for the local bounded schema, `router fleet status --watch` for the combined table, or `router fleet status --json` for machine-readable output. Combining `--watch --json` emits one compact JSON document per line. `router fleet capacity` shows only capacity summaries, and `router fleet host worker-a --json` returns one configured host. Add `--strict` when automation must fail if any host marked `required = true` is unavailable.

`router dashboard` serves the fleet view as a live web page at `http://127.0.0.1:8765/` (`--port` to change it). It listens on loopback only, refreshes every 3 seconds and shows hosts, capacity, sessions with their subagents nested beneath, Router children, managed runs and alerts. Stop it with Ctrl-C. It reads the same data as `router fleet status`, so it needs `fleet.toml` above, the Router service or daemon running on each configured host, and `router enable` in each project whose sessions you want to see. To view the dashboard of a remote host instead, start it there and forward the port: `ssh -L 8765:127.0.0.1:8765 <host>`, then open the same URL locally.

Fleet commands run Router remotely over SSH: `router fleet status`, `capacity`, and `host` invoke `router snapshot --json`; `router fleet request-status`, `task-diff`, `wait`, `ack`, and `correlation` run the matching command on one explicit or each configured host. Fleet monitoring does not copy SQLite files, change SSH configuration, schedule work, kill processes, or write project state. Capacity states are summaries over explicit CPU, load, memory, and disk thresholds; inspect the accompanying raw measurements and reason list for job-specific decisions. Snapshots include `router_tasks` when any exist: open or recently changed Router-dispatched tasks with lifecycle fields only (no cost, usage, or worktree; task text and error messages follow `AGENT_ROUTER_FLEET_SHARE_TEXT`). `inferred_processes` lists the processes of live Claude Code and Codex sessions: the session's own process and its descendants, with PID, executable name, start time, CPU, and resident memory, attributed `session`. Router finds a session's process as the ancestor its hook calls share, and checks its creation identity so a reused PID is never attributed; it never reads command lines or environments. `managed_processes` lists commands started with `router run --label NAME [--session ID --agent ID] -- command args` inside an observed project; outside one, `router run` still runs the command but records nothing, even with `--session` naming an observed session. The command runs in its own session and process group, with stdin closed and no controlling terminal (a prompt for input fails rather than hanging). Router records its PID, group, start time, executable name, argument count (never the argument text or environment), working directory, and on completion the exit code or signal, elapsed time, the largest single process's resident memory, and whether anything in its group outlived it (`group_outlived`). While a recent command's outcome is unknown, the host's managed-process count and capacity read unknown. Labels follow `AGENT_ROUTER_FLEET_SHARE_TEXT`. A failed or signalled command raises `managed_process_failed`, and an unknown outcome or a group that outlived its command raises `managed_process_shutdown_uncertain`; `router fleet status` shows managed commands as rows and a one-line summary of session processes per host. Naming both the session and the agent gives `exact` attribution. Evidence survives a Router restart: if the daemon is down when the command ends, the wrapper leaves a completion file the daemon reads later, and a wrapper that disappears without evidence is reported `unknown`. Router never stops a managed command; Ctrl-C, TERM, and HUP sent to `router run` are passed to the command's group. A child that starts its own session (setsid) leaves the group and is not tracked; suspending `router run` (Ctrl-Z) does not suspend the command.

Remote children are separate from monitoring: a parent can start a child on a configured SSH host, which serves each request with `router remote --json`; see [remote children](remote-snapshots.md).

Snapshots carry text as written, such as task descriptions, task subjects, and error messages. Router removes terminal control sequences and bounds length, but it does not scan text for secrets or rewrite it. Each host decides whether its task text leaves the machine: set `AGENT_ROUTER_FLEET_SHARE_TEXT=false` on a host to withhold its task descriptions, subjects, and Router task error messages from fleet snapshots. The default is to share.

An external coordinator can reserve a host without granting Router any control authority by atomically writing `~/.agent-router/reservation.json` on that host:

```json
{
  "schema_version": 1,
  "reserved_by": "batch-scheduler",
  "reserved_until": 1792173600000,
  "reserved_cpu_slots": 8,
  "reserved_memory_bytes": 34359738368,
  "reservation_source": "batch-queue"
}
```

`reserved_until` is a Unix timestamp in milliseconds. Router only reads this file. An active reservation prevents the host from being classified as `available`; a reservation consuming every logical CPU or leaving critically little unreserved memory makes it `saturated`. Missing files mean no reservation, while malformed files mark capacity `unknown` and the snapshot partial. Writers should replace the file atomically so readers never observe a partial record.
