# Agent Router

Local, parent-supervised delegation between coding agent CLIs. The first integration is Claude Code as parent and OpenCode as child. Agent Router runs the requested model in a separate Git worktree; the parent reviews and integrates the changes.

The first version supports one local user, Python 3.14+, an ordinary Git repository with an existing `HEAD`, and OpenCode's standalone CLI. The host integration targets Claude Code with `session_id` and `prompt_id` on its submission and tool hooks. macOS is the verified host; Linux process ownership is implemented but needs its own live-host verification. Windows, submodules, sparse checkouts, and unresolved merges are unsupported.

## Install

Install OpenCode and configure access to your chosen provider first. Then install Agent Router from this checkout:

```sh
uv tool install --python 3.14 .
```

For development:

```sh
uv venv --python 3.14
uv pip install --python .venv/bin/python -e .
source .venv/bin/activate
```

There is one direct runtime dependency: `pydantic-settings`. Tests use the standard library.

## Configure

Copy [the example configuration](examples/router.env.example) to `~/.agent-router/.env` and make it readable only by you. Set every required operator value: concurrency cap, per-attempt deadline, child-launch ceiling, OpenCode command, model-alias registry, and repository setup commands. Missing or invalid values abort startup. Process environment variables override values in the env file. Restart the daemon after changing configuration.

Provider credentials can live in that same env file. The router passes them through the child process environment, not invocation/result JSON. It stores no submitted user prompt bodies. Task instructions, review feedback, child output, and artifact paths are persisted locally, so treat the state directory as private.

Each configured repository needs an explicit non-interactive permission profile in `opencode.json`, for example:

```json
{"permission": {"*": "allow", "external_directory": "deny"}}
```

This example permits writes within the child's normal workflow; it is not a sandbox. Choose the denials your task requires. A denied operation must be denied through every tool that can perform it. `ask` is unsupported. The router freezes the profile for the task and supplies it on every launch using OpenCode's [inline configuration](https://opencode.ai/docs/config/#precedence-order). Administratively managed OpenCode configuration can take precedence.

Keep `.venv/`, `node_modules/`, and credential files untracked and ignored. Setup runs inside the child worktree and must create its own dependencies. The router does not symlink the parent's environment. Setup-generated non-ignored files become part of the reviewed delta.

Setup commands must keep their descendants in the launched process group; daemonizing or detached setup commands are unsupported. Verify containment for each repository's setup workflow before relying on cancellation.

Start the daemon explicitly in a separate terminal:

```sh
router daemon start
```

It runs in the foreground. Clients never auto-spawn it. State defaults to `~/.agent-router/`, with a private Unix socket, SQLite database, operator log, and per-task artifacts. `router daemon stop` requests graceful cancellation of active work before stopping. A crash preserves records and artifacts for restart reconciliation; uncertain execution is never automatically relaunched.

Load the bundled plugin in your parent session:

```sh
claude --plugin-dir "$(router plugin-path)"
```

`router` must be on the parent host's `PATH`. The plugin is bundled in installed wheels as well as this checkout; no marketplace setup is required. With the daemon stopped, ordinary prompts still work, but delegation is unavailable.

## Delegate and review

Ask the parent to delegate an explicit task to your configured OpenCode model alias. The plugin captures submission metadata and supplies a router-owned instruction ID. The parent emits one canonical, literal `router start --json '<object>'` command. A host hook authorizes that exact request; registration consumes the authorization transactionally. Unsupported shell forms and unauthorized/mismatched requests fail closed. No file-path dispatch or authorization-bypass CLI exists.

Start returns a durable task ID promptly. Watch from a terminal, without model polling:

```sh
router status --watch
router status task-<id> --json
router status --filter needs_decision --json
```

The parent inspects the recorded patch, child files, and execution/test evidence. Its five task operations are:

| Operation | Purpose |
|---|---|
| `start` | Register a delegation for the current host submission |
| `status` | Inspect one task, list tasks, or filter tasks needing a decision |
| `review` | Accept artifacts or request one same-session correction |
| `cancel` | Stop active work, or establish stopped execution after interruption |
| `close` | Record integration/discard and clean task-owned Git artifacts |

Mutations use fresh request IDs; identical retries return the original operation's current result. Review also checks the current attempt number. The plugin authorizes mutations under the host session, so later submissions can review an existing task without rebinding its original instruction. The executor and resolved model remain fixed across corrections.

Acceptance does not copy code. The parent applies the recorded snapshot-relative patch using `git apply --check` and plain `git apply`. Context conflicts stop integration; there is no three-way fallback, partial application, or automatic merge. Dirty-parent staging choices remain unchanged. Empty patches need no application.

After integration, the parent closes with an `integrated` disposition; final rejection uses `discarded`. Close removes the child worktree (including its installed dependencies), child branch, and task refs by default. `keep_artifacts: true` retains them. Operation/attempt history and captured output remain in local state. Cleanup failures remain visible and can be retried by replaying the same close request.

Timeouts and executor failures preserve partial work. The parent can explicitly accept stopped failed artifacts for salvage, but failure evidence remains recorded and is excluded from normal first-pass acceptance. An `interrupted` task cannot be accepted or cleaned up until stopped execution is established through cancellation. OpenCode does not produce structured blockers in this MVP.

Limits are operator configuration, never parent-raisable task inputs. A launch ceiling is not a dollar budget. The router does not check whether a delegation matches natural-language intent, guarantee code correctness, isolate children from the host user, or automatically recover failed infrastructure.

Hooks establish provenance for the normal host workflow, not an adversarial security boundary against processes running as the same host user. Such processes can access the local socket and state. Do not use this MVP for hostile agents or multi-tenant isolation.

## Verify

```sh
python -m unittest discover -s tests -v
```

The suite uses real Git repositories, SQLite transactions, Unix sockets, and controlled subprocesses. It makes no model/provider calls and installs no fake import modules.

An opt-in live check exercises the actual Claude hooks and two OpenCode attempts on a generated repository, including native memory continuity, isolated setup, dirty-parent integration, and cleanup:

```sh
python scripts/live_smoke.py --model '<your-exact-OpenCode-provider/model>'
```

This spends provider tokens (four Claude turns plus two child runs). It prints its scratch directory and preserves artifacts for diagnosis. It never modifies an existing user repository.

## License

The open-source license is awaiting the owner's choice. No redistribution license is granted yet.
