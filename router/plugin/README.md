# Agent Router

See native Claude subagents and Router-controlled OpenCode children in one local terminal view. Track the spawning session, task, exact model, observed state, activity, elapsed time, tokens, and estimated cost. Native agents remain Claude-owned; Router controls only the children it launches.

Run `router setup` after installing the Agent Router runtime. It installs this plugin at user scope and asks before registering a macOS background service. Normal Claude sessions then load the plugin across projects. `router init` separately enables OpenCode delegation for an approved repository. Hooks never download packages, start a daemon, or grant permissions.

Hooks execute the installed runtime in Python's isolated mode, not a second copy of the package from the plugin cache. Only configured delegation projects receive instruction IDs; unconfigured projects remain observation-only. The bootstrap interpreter pointer stays in the default bootstrap directory even when advanced environment configuration moves data state.

The hooks read Claude's native subagent transcripts locally for usage and activity metadata. Router retains metadata, limited output summaries, task instructions for external children, execution artifacts, and history in its private state directory. The native observer does not retain prompt bodies. It does not upload telemetry. External OpenCode work uses your configured provider and credentials and can read or modify host-user files; worktrees are not security sandboxes. Costs are estimates, not bills, and missing data stays unknown.

`router status --watch` opens the shared terminal view. `router uninstall` removes the user plugin and managed service but retains local configuration and history. On Linux the daemon is started manually; Windows is unsupported. This integration targets local Claude Code sessions, not ordinary Claude chat.

License: [MIT](LICENSE).
