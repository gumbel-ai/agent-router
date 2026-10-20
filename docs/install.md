# Install and operation

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
Router never writes hook trust. See [project enablement](project-enablement.md)
for prerequisites, preservation rules and migration from older global installations.

Then use ordinary `claude` or `codex` there; no launch flags are needed. Router records a session once any of its hook events comes from inside a marked project (the nearest marked ancestor of the session's working directory) and follows it from then on, even if it leaves. Sessions that never enter a marked project are not recorded. The marker must be a regular file containing exactly `{"observe": true}`; anything else fails observation visibly (the hook names the marker) instead of silently opting out. Removing the marker stops new admission; remove Router's client configuration entries too to prevent hook startup in fresh sessions. Sessions already recorded are still followed, including sessions recorded before marker admission existed. Watch the observed projects in another terminal:

```sh
router status --watch
```

For external delegation only, install OpenCode and configure its provider access, then run `router init` in each target Git repository. Confirm the exact model, child dependency setup command, explicit OpenCode permissions, and global limits. Init records setup; it does not run it. Keep dependencies and credentials untracked and ignored; setup must not launch detached processes. [Advanced environment configuration](../examples/router.env.example) remains supported.

For Codex instead, install and sign in to the Codex CLI, then run `router init --executor codex`. Confirm an exact `alias=openai/model-id` and a `read-only` or `workspace-write` sandbox. Router uses the CLI's saved login and ignores Router provider-key/endpoint overrides; it does not create or request an API key. The integration targets CLI 0.162.1 and was live-checked with 0.163.0. Codex children do not spawn native subagents in this version. Cost is a labelled token estimate, including reported cache writes; unreported charges are excluded and missing data stays unknown. Dependency setup runs outside the Codex sandbox.

`router service stop` stops the idle managed service; `router service start` starts it again and lets it recover interrupted work. `router uninstall` removes the plugin, service, and CLI while retaining configuration and history. Installation/configuration changes refuse active Router work or pending cleanup; failed cleanup stays visible without blocking maintenance. Native subagents remain Claude-owned.

For Claude children, see [Claude executor configuration and verification](claude-executor.md).

Optional Langfuse tracing of Router-launched children has been live-verified for OpenCode, Codex (including a remote Linux host over SSH), and Claude via the pinned plugin fork, including separate traces per resumed attempt; see [child tracing](tracing.md). Native Codex hook observation is described in [Codex lifecycle](codex-lifecycle.md).

If a Codex or Claude shutdown stays unconfirmed, an operator must inspect and stop its remaining commands. Stop the daemon with `router daemon stop`, then run `router recover TASK_ID --reason 'what you inspected'` in an interactive terminal. Recovery requires the recorded process group to be stopped and a typed acknowledgement; it releases the slot but preserves uncertainty evidence and the worktree. Restart the service afterward. Agents must not perform recovery.

Related commands:

- `router request-status` recovers a request's task after a lost reply; see [request status](request-status.md).
- `router task-diff` reads one attempt's retained patch; see [task diffs](task-diffs.md).
- `router correlation --json` exports content-free identifiers for joining with Langfuse; see [correlation](correlation.md).
- `router wait`, `router ack`, and `router consumer list|remove` manage durable completion delivery; see [completions](completions.md).

The plugin is also installable through Claude's normal marketplace command after the Router runtime is installed:

```sh
claude plugin install agent-router --marketplace gumbel-ai/agent-router --scope project
```
