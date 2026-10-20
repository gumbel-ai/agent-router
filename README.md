# Agent Router

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

Needs Git, uv and Claude Code 2.1.292+. macOS and Linux; no Windows.

```sh
uvx --python 3.14 --from git+https://github.com/gumbel-ai/agent-router router setup
```

On macOS this also installs the background service. On Linux, run `router daemon start` and keep it running.

## Use

```sh
router enable            # in each Git project to observe; commit the files it writes
router status --watch    # sessions, subagents and Router children on this machine
```

Then use plain `claude` or `codex`. To delegate to child agents, run `router init` (OpenCode), `router init --executor codex`, or see [Claude children](docs/claude-executor.md). Optional Langfuse tracing: [child tracing](docs/tracing.md).

## Fleet and dashboard

Create `~/.agent-router/fleet.toml`:

```toml
schema_version = 1

[[host]]
name = "laptop"
transport = "local"

[[host]]
name = "worker-a"
transport = "ssh"
target = "worker-a"   # SSH alias; Router must run there too
```

```sh
router fleet status --watch   # all hosts in one table
router dashboard              # same view, live at http://127.0.0.1:8765/
```

## Docs

- [Install and operation](docs/install.md): setup details, Codex/OpenCode init, service, recovery
- [Fleet monitoring](docs/fleet.md): config, dashboard, snapshots, managed runs, reservations
- [Project enablement](docs/project-enablement.md), [Codex delegation](docs/codex-delegation.md), [Codex lifecycle](docs/codex-lifecycle.md)
- [Completions](docs/completions.md), [Request status](docs/request-status.md), [Task diffs](docs/task-diffs.md), [Correlation](docs/correlation.md), [Remote children](docs/remote-snapshots.md)

License: [MIT](LICENSE).
