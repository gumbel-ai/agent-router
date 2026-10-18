# Project observation enablement

Run `router setup` once to install the CLI and background service and register the
plugin marketplaces. Setup does not newly enable Router's observer plugins globally.
Then run `router enable` from a Git project or one of its subdirectories.

The command installs `agent-router@agent-router` in Claude at **project** scope and
writes at the current Git worktree root:

- `.agent-router.json`: exactly `{"observe": true}` followed by a newline (18 bytes).
- `.claude/settings.json`: `enabledPlugins["agent-router@agent-router"] = true`.
- `.codex/config.toml`: `[plugins."agent-router@agent-router"]` with `enabled = true`.

Unrelated configuration is retained. TOML comments and formatting are preserved.
Each file is replaced atomically; the marker is written last. A failed command can
leave partial client installation or configuration, and reports the error. Correct
it and rerun the idempotent command. Conflicting markers, malformed configuration,
duplicate JSON keys, and linked configuration files or directories are refused.
No Git commit, daemon start, delegated task, or managed command is launched.

Commit all three files. Every collaborator runs `router enable` once to install
Claude's project-scoped cache locally. A new worktree needs the committed files and
its own project-scoped Claude installation. Subdirectory sessions inherit project
configuration in Codex. Claude reads shared settings from its **launch directory**,
so root enablement alone does not enable a Claude session started in a subdirectory.
Run `router enable` in each directory from which you start Claude: it also merges
that directory's `.claude/settings.json`, while retaining the root marker and Codex
configuration. Commit the additional file to share that opt-in. No recursive scan
or global startup hook is installed. A nearer client configuration can override a
project setting. See Claude's [shared-settings location](https://code.claude.com/docs/en/settings#where-claude-code-keeps-the-local-file-in-a-git-repository).

## Codex prerequisites

Codex loads project `.codex/config.toml` only for trusted projects. Refresh local
marketplace plugins in Codex to populate its cache, then approve Router's hooks
manually through `/hooks`. Installing or enabling a plugin does not trust it.
Router never writes or bypasses hook trust, changes authentication, or relaxes the
sandbox. `CODEX_HOME`, when selected, is honored; Router does not search other homes.

The project plugin setting is supported by Codex CLI 0.163.0. The installed cache,
project trust and hook trust remain separate prerequisites. See the upstream
[project plugin configuration](https://developers.openai.com/codex/plugins/build#enable-or-disable-a-plugin-for-a-repo)
and [configuration precedence](https://developers.openai.com/codex/config-basic#configuration-precedence).

The current `router status` readiness check inspects the selected home's user
configuration; it does not establish whether a particular project's layer loaded.
Check project plugin loading and hook trust in Codex.

## Legacy global installations

Setup detects Router's user-scope Claude installation or enablement entry and its
enabled plugin entry in the selected Codex home's user configuration. It offers a
**separate interactive confirmation** to remove only those Router entries:

- Claude: uninstall at user scope with `--keep-data`, and remove Router's user
  `enabledPlugins` entry. Other plugins and project installations are retained.
- Codex: set only Router's user-level `enabled` field to `false`. Keep its cache,
  other plugins, comments, hook trust and session history.

`router setup --yes` does not approve this migration. Without a terminal, or if the
confirmation is declined, legacy entries are retained and setup explains that global
hooks can still start. Return in a terminal to migrate them. Existing daemon history
and already-admitted sessions are retained. Marker admission remains a second check
for legacy installations; a marker alone does not prevent a globally enabled hook
process from starting.

Fresh sessions in projects without client enablement load no Router plugin hook.
To disable a project, remove its Router entries from both project configurations and
remove the marker, then start fresh sessions. `router uninstall` removes the runtime
and any legacy user-scope installation; shared project files and history are retained.

Claude's [installation scopes](https://code.claude.com/docs/en/plugins/install#choose-an-install-scope)
describe the distinction between project configuration and the local plugin cache.
