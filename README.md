# Agent Router

Delegate coding tasks to another agent while the parent stays in control.

## What you get

- Delegate across agent CLIs: Claude Code to OpenCode in v1.
- Choose the exact child model, with no silent substitution.
- Run tasks in parallel while the parent keeps working.
- Watch native Claude subagents and Router-launched children across projects with `router status --watch`.
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

`router service stop` stops the idle managed service; `router service start` starts it again. `router uninstall` removes the plugin, service, and CLI while retaining configuration and history. Installation/configuration changes refuse active Router work. Native subagents remain Claude-owned.

The plugin is also installable through Claude's normal marketplace command after the Router runtime is installed:

```sh
claude plugin install agent-router --marketplace gumbel-ai/agent-router --scope user
```

License: [MIT](LICENSE).
