# Agent Router

Use [`router wait` and `router ack`](docs/completions.md) for durable delivery of
managed-command completions to an active agent's blocking tool call.

Delegate coding tasks to another agent while the parent stays in control.

## What you get

- Delegate across agent CLIs: a Claude Code or [Codex](docs/codex-delegation.md) parent to OpenCode, Codex, or Claude Code children.
- Choose the exact child model, with no silent substitution.
- Run tasks in parallel while the parent keeps working.
- Watch native Claude Code and Codex sessions and subagents, and Router-launched children, across projects with `router status --watch`.
- View configured macOS and Linux hosts, Claude sessions, subagents, and available capacity with `router fleet status --watch`.
- See the same fleet view as a live local web page with `router dashboard`.
- Give each child its own Git worktree and dependencies, not a security sandbox.
- Start from your current files, including uncommitted changes.
- Let the parent inspect, request corrections, accept, or discard the work.
- Continue corrections in the same child session, preserving context.
- Track tasks durably and prevent duplicate launches on request retries.
- See who started each agent, its model, elapsed time, and activity, plus tokens and estimated cost for Router-launched children. Use Langfuse for content, per-call usage, and cost of native Claude Code and Codex sessions.
- Set limits on concurrency, runtime, and correction attempts.
- Preserve partial work when a child fails or times out.
- Integrate reviewed changes explicitly and stop on patch conflicts.
- Clean up child worktrees while retaining execution and review history.

## Install

Requires Git, uv, and Claude Code 2.1.292+ (sign in to run agents). Automatic background-service setup supports macOS. Python 3.14 is installed by uv if needed. Linux supports manual daemon startup and has been verified on a live host; Windows is unsupported.

```sh
uvx --python 3.14 --from git+https://github.com/gumbel-ai/agent-router router setup
```

Approve the one-time CLI installation and marketplace registration. On macOS, setup
also installs the background service. On Linux, setup leaves daemon startup manual:
run `router daemon start` in a separate terminal and keep it running. An existing
manual daemon must be stopped with `router daemon stop` before setup and restarted
afterward. Setup registers the Claude and, when installed, Codex marketplaces.
Enable observation separately in each Git project:

```sh
router enable
git add .agent-router.json .claude/settings.json .codex/config.toml
```

`router enable` installs Claude's plugin at project scope and merges both clients'
project configuration. Commit these files so clones and worktrees carry the opt-in.
Start fresh Claude and Codex sessions. Codex loads project configuration only in
trusted projects; refresh its plugins and trust Router's hooks manually with `/hooks`.
Router never writes hook trust. See [project enablement](docs/project-enablement.md)
for prerequisites, preservation rules and migration from older global installations.

Then use ordinary `claude` or `codex` there; no launch flags are needed. Router records a session once any of its hook events comes from inside a marked project (the nearest marked ancestor of the session's working directory) and follows it from then on, even if it leaves. Sessions that never enter a marked project are not recorded. The marker must be a regular file containing exactly `{"observe": true}`; anything else fails observation visibly (the hook names the marker) instead of silently opting out. Removing the marker stops new admission; remove Router's client configuration entries too to prevent hook startup in fresh sessions. Sessions already recorded are still followed, including sessions recorded before marker admission existed. Watch the observed projects in another terminal:

```sh
router status --watch
```

For external delegation only, install OpenCode and configure its provider access, then run `router init` in each target Git repository. Confirm the exact model, child dependency setup command, explicit OpenCode permissions, and global limits. Init records setup; it does not run it. Keep dependencies and credentials untracked and ignored; setup must not launch detached processes. [Advanced environment configuration](examples/router.env.example) remains supported.

For Codex instead, install and sign in to the Codex CLI, then run `router init --executor codex`. Confirm an exact `alias=openai/model-id` and a `read-only` or `workspace-write` sandbox. Router uses the CLI's saved login and ignores Router provider-key/endpoint overrides; it does not create or request an API key. The integration targets CLI 0.162.1 and was live-checked with 0.163.0. Codex children do not spawn native subagents in this version. Cost is a labelled token estimate, including reported cache writes; unreported charges are excluded and missing data stays unknown. Dependency setup runs outside the Codex sandbox.

`router service stop` stops the idle managed service; `router service start` starts it again and lets it recover interrupted work. `router uninstall` removes the plugin, service, and CLI while retaining configuration and history. Installation/configuration changes refuse active Router work or pending cleanup; failed cleanup stays visible without blocking maintenance. Native subagents remain Claude-owned.

For Claude children, see [Claude executor configuration and verification](docs/claude-executor.md).

Optional Langfuse tracing of Router-launched children has been live-verified for OpenCode, Codex (including a remote Linux host over SSH), and Claude via the pinned plugin fork, including separate traces per resumed attempt; see [child tracing](docs/tracing.md). Native Codex hook observation is described in [Codex lifecycle](docs/codex-lifecycle.md).

If a Codex or Claude shutdown stays unconfirmed, an operator must inspect and stop its remaining commands. Stop the daemon with `router daemon stop`, then run `router recover TASK_ID --reason 'what you inspected'` in an interactive terminal. Recovery requires the recorded process group to be stopped and a typed acknowledgement; it releases the slot but preserves uncertainty evidence and the worktree. Restart the service afterward. Agents must not perform recovery.

Related commands:

- `router request-status` recovers a request's task after a lost reply; see [request status](docs/request-status.md).
- `router task-diff` reads one attempt's retained patch; see [task diffs](docs/task-diffs.md).
- `router correlation --json` exports content-free identifiers for joining with Langfuse; see [correlation](docs/correlation.md).
- `router wait`, `router ack`, and `router consumer list|remove` manage durable completion delivery; see [completions](docs/completions.md).

The plugin is also installable through Claude's normal marketplace command after the Router runtime is installed:

```sh
claude plugin install agent-router --marketplace gumbel-ai/agent-router --scope project
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
name = "worker-a"
transport = "ssh"
target = "worker-a"
tags = ["batch-worker"]
minimum_available_memory_gib = 12
```

Run `router snapshot --json` for the local bounded schema, `router fleet status --watch` for the combined table, or `router fleet status --json` for machine-readable output. Combining `--watch --json` emits one compact JSON document per line. `router fleet capacity` shows only capacity summaries, and `router fleet host worker-a --json` returns one configured host. Add `--strict` when automation must fail if any host marked `required = true` is unavailable.

`router dashboard` serves the fleet view as a live web page at `http://127.0.0.1:8765/` (`--port` to change it). It listens on loopback only, refreshes every 3 seconds and shows hosts, capacity, sessions with their subagents nested beneath, Router children, managed runs and alerts. Stop it with Ctrl-C. It reads the same data as `router fleet status`, so it needs `fleet.toml` above, the Router service or daemon running on each configured host, and `router enable` in each project whose sessions you want to see. To view the dashboard of a remote host instead, start it there and forward the port: `ssh -L 8765:127.0.0.1:8765 <host>`, then open the same URL locally.

Fleet commands run Router remotely over SSH: `router fleet status`, `capacity`, and `host` invoke `router snapshot --json`; `router fleet request-status`, `task-diff`, `wait`, `ack`, and `correlation` run the matching command on one explicit or each configured host. Fleet monitoring does not copy SQLite files, change SSH configuration, schedule work, kill processes, or write project state. Capacity states are summaries over explicit CPU, load, memory, and disk thresholds; inspect the accompanying raw measurements and reason list for job-specific decisions. Snapshots include `router_tasks` when any exist: open or recently changed Router-dispatched tasks with lifecycle fields only (no cost, usage, or worktree; task text and error messages follow `AGENT_ROUTER_FLEET_SHARE_TEXT`). `inferred_processes` lists the processes of live Claude Code and Codex sessions: the session's own process and its descendants, with PID, executable name, start time, CPU, and resident memory, attributed `session`. Router finds a session's process as the ancestor its hook calls share, and checks its creation identity so a reused PID is never attributed; it never reads command lines or environments. `managed_processes` lists commands started with `router run --label NAME [--session ID --agent ID] -- command args` inside an observed project; outside one, `router run` still runs the command but records nothing, even with `--session` naming an observed session. The command runs in its own session and process group, with stdin closed and no controlling terminal (a prompt for input fails rather than hanging). Router records its PID, group, start time, executable name, argument count (never the argument text or environment), working directory, and on completion the exit code or signal, elapsed time, the largest single process's resident memory, and whether anything in its group outlived it (`group_outlived`). While a recent command's outcome is unknown, the host's managed-process count and capacity read unknown. Labels follow `AGENT_ROUTER_FLEET_SHARE_TEXT`. A failed or signalled command raises `managed_process_failed`, and an unknown outcome or a group that outlived its command raises `managed_process_shutdown_uncertain`; `router fleet status` shows managed commands as rows and a one-line summary of session processes per host. Naming both the session and the agent gives `exact` attribution. Evidence survives a Router restart: if the daemon is down when the command ends, the wrapper leaves a completion file the daemon reads later, and a wrapper that disappears without evidence is reported `unknown`. Router never stops a managed command; Ctrl-C, TERM, and HUP sent to `router run` are passed to the command's group. A child that starts its own session (setsid) leaves the group and is not tracked; suspending `router run` (Ctrl-Z) does not suspend the command.

Remote children are separate from monitoring: a parent can start a child on a configured SSH host, which serves each request with `router remote --json`; see [remote children](docs/remote-snapshots.md).

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

License: [MIT](LICENSE).
