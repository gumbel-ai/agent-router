# Project observation enablement

Run `router setup` once to install the CLI and register the plugin marketplaces.
On macOS it also installs the background service. On Linux it installs no service:
run `router daemon start` in a separate terminal and keep it running, then use
`router enable` from another terminal. Before rerunning setup, stop any manual daemon
with `router daemon stop`; restart it after setup. The macOS `router service`
commands do not manage a Linux daemon. Linux live-host verification is **NOT CHECKED**.
`router uninstall` removes the CLI and Router's user plugin installation while
retaining configuration and history; on Linux, stop the manual daemon first.

Setup does not newly enable Router's observer plugins globally. Run `router enable`
from a Git project or one of its subdirectories.

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

`router status` checks readiness for a fresh Codex session in its current directory.
It uses only the explicit `CODEX_HOME`, or the default home when unset. It connects
to that home's already-running Codex daemon control socket for `config/read` and
`hooks/list`, so Codex applies project trust, subdirectory/worktree inheritance and
configuration precedence. Router does not
guess which project layer Codex would load. It checks the project marker and the
selected home's installed hook declarations against the native hook inventory.
Project configuration cannot grant user hook trust.

The probe starts no process, agent turn or daemon, refreshes no plugins and writes
no hook trust. It uses Codex's native non-originating daemon-probe handshake, which
preserves daemon-wide authentication policy and client identity. Socket aliases
are resolved, including a long selected-home path pointing to a short native
rendezvous socket. An existing native control socket is required. Whether an
ordinary Codex session creates that socket is **NOT CHECKED**; launching Codex is
not a verified remedy for its absence. A missing or unreachable endpoint reports
`unknown`; Router does not start a daemon as a fallback. Only a missing endpoint or
an initially refused connection suggests starting Codex normally in the selected
home. Access, handshake, protocol and hook-discovery failures retain their cause
without startup advice. These failure diagnostics render the cause as single-line
terminal-safe text bounded to 4096 characters; they never classify native error
messages by their wording.
The daemon's initialization reply must identify the selected home; an alias into
another home's daemon reports `unknown` before its configuration or hooks are read.
The native control socket/API was verified with Codex 0.163.0. Unsupported clients
also report `unknown`. Queries and normal connection close share a five-second
deadline with a one MiB output bound; cleanup after cancellation allows up to one
further second. When only normal connection close exceeds that deadline, fully
received and validated configuration and hook evidence still determine readiness.
A deadline before that evidence is complete reports `unknown`. A missing marker is
`project_not_enabled`; untrusted hooks remain `hooks_untrusted`. Missing support,
malformed replies and transport failures report `unknown` with a reason.
Missing native Router hooks report `unknown` with discovery warnings when supplied;
complete Router hook evidence remains usable despite unrelated discovery warnings.
Readiness describes configuration prerequisites, not proof that an existing
session delivered events. Per-session overrides and an existing app-server's
environment still require checking in that session.

The isolated verification created the selected home's `app-server-control`
directory, then ran Codex's native transport fixture explicitly:

```sh
codex app-server --listen "unix://${CODEX_HOME:-$HOME/.codex}/app-server-control/app-server-control.sock"
```

This tests the native API. It does not establish that ordinary interactive or
exec sessions use this daemon, or that they delivered lifecycle hooks.

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
