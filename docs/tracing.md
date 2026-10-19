# Child tracing opt-in

Router can record that an operator requested tracing for the children it launches
in one repository. Tracing stays off by default. Router never uploads traces or
calls Langfuse; only a runtime's own tracing plugin could export a child trace.

```sh
router init --executor codex --sandbox workspace-write --model codex=openai/<exact-model> \
  --setup-command 'true' --tracing --yes
```

Init stores `"tracing": "requested"` in that repository's configuration. Each init
states the choice again: running init without `--tracing` removes it. Any other
stored value is refused by configuration validation, and a task started with such
a value fails before launch.

## Current availability

No route that runs a tracing plugin inside a Router child has been verified.
Every task started in a repository that requested tracing records:

```json
"tracing": {"requested": true, "available": false, "reason": "..."}
```

The task then runs exactly as it would without the opt-in. `router status TASK_ID --json`
and attempt history show this record. Tasks without the opt-in have no `tracing` field.

| Executor | Reason recorded | Evidence |
| --- | --- | --- |
| Codex | Children run with `--ignore-user-config`. Codex then uses an empty user layer, and plugin hooks need trust from the user or session-flag layer. | Codex `rust-v0.163.0` [configuration loader](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/config/src/loader/mod.rs#L512), [hook trust rules](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/config_rules.rs#L8), [hook discovery](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/engine/discovery.rs#L666) |
| Claude | Children run with `--safe-mode` and no setting sources, so installed plugins and hooks do not load. | `claude --help` on 2.1.303; [Claude children](claude-executor.md) |
| OpenCode | No Router-launched route is verified. | [OpenCode audit](langfuse-audit.md#opencode-plugin-052) |

Router does not fix these boundaries by copying hook trust, removing isolation flags,
loading the operator's user configuration or creating another runtime home. A route
needs separate verification first.

## What does not change

- **Child launch.** The opt-in adds no arguments, settings, metadata or credentials to
  the child command or environment.
- **Credentials.** The child environment follows the existing rules. Router's environment
  file is still passed to children as documented in
  [the example configuration](../examples/router.env.example), so keep tracing
  credentials out of that file.
- **Counters.** Router's token and cost counters stay as they are. A future trace
  would not replace or suppress them. Analysis must pick one source for each total
  and never add Router and Langfuse totals together.
- **Correlation.** `router correlation --json` still reports `traced: "unknown"`.

## Metadata channels for a future route

These channels are verified from pinned plugin source only. Router does not set them
today, because neither plugin can run in a child. Setting them anyway would wrongly
suggest that tracing is enabled.

- Codex plugin 0.4.2: `LANGFUSE_CODEX_METADATA`, a JSON object whose values become
  strings ([config loader](https://github.com/langfuse/codex-observability-plugin/blob/586910c81eeaf21d8f54c1c86c89a29a5466c1ef/plugins/tracing/src/config.ts)).
  A route would supply `router_task_id`, `router_host_id` and any declared `work_item_id`.
- Claude plugin 1.2.1: `CC_LANGFUSE_TRACE_TAGS`, at most 20 tags of at most 200
  characters each ([hook source](https://github.com/langfuse/Claude-Observability-Plugin/blob/b5b7cc364996467e856dc17575b5674f4799ad87/hooks/langfuse_hook.py)).
  This plugin has no general metadata object, so identities would be carried as tags.

## NOT CHECKED

- Whether `--plugin-dir` plugins or hooks given through `--settings` run under Claude
  `--safe-mode`. The help text does not say, and no live child was run.
- Effective hook inventory, actual traces and their reconciliation with native records
  for fresh and resumed children.
- Remote children. They depend on the remote-children work and are out of scope here.
