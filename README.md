# Agent Router

Delegate coding tasks to another agent while the parent stays in control.

## What you get

- Delegate across agent CLIs: Claude Code to OpenCode in v1.
- Choose the exact child model, with no silent substitution.
- Run tasks in parallel while the parent keeps working.
- Watch native Claude subagents and Router-launched children together with `router status --watch`.
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

Requires Git, uv, Python 3.14+, Claude Code, and OpenCode with provider access configured. macOS is verified; Linux needs live-host verification. Windows is unsupported.

```sh
git clone https://github.com/gumbel-ai/agent-router.git
cd agent-router
uv tool install --python 3.14 .
```

Copy [the example configuration](examples/router.env.example) to `~/.agent-router/.env`, replace its repository path and model ID, and choose the limits and setup command. Keep the file private (`chmod 600 ~/.agent-router/.env`). The target repository needs an existing Git commit and explicit `allow`/`deny` permissions in `opencode.json`; `ask` is unsupported. Keep dependency folders and credentials untracked and ignored. Setup must create the child's own dependencies and must not launch detached processes.

Start the daemon in one terminal:

```sh
router daemon start
```

In your target repository, launch the parent in another terminal:

```sh
claude --plugin-dir "$(router plugin-path)"
```

License: [MIT](LICENSE).
