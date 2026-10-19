# Child tracing

Router-launched children are currently untraced. Router never uploads traces or
calls Langfuse; only a runtime's own tracing plugin could export a child trace, and
no route that runs such a plugin inside a Router child has been verified.

Router's token and cost counters remain the record for child usage, and
`router correlation --json` reports `traced: "unknown"`.

| Executor | Why no plugin runs | Evidence |
| --- | --- | --- |
| Codex | Children run with `--ignore-user-config`. Codex then uses an empty user layer, and plugin hooks need trust from the user or session-flag layer. | Codex `rust-v0.163.0` [configuration loader](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/config/src/loader/mod.rs#L512), [hook trust rules](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/config_rules.rs#L8), [hook discovery](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/engine/discovery.rs#L666) |
| Claude | Children run with `--safe-mode` and no setting sources, so installed plugins and hooks do not load. | `claude --help` on 2.1.303; [Claude children](claude-executor.md) |
| OpenCode | No Router-launched route is verified. | [OpenCode audit](langfuse-audit.md#opencode-plugin-052) |

Router does not work around these boundaries by copying hook trust, removing
isolation flags, loading the operator's user configuration or creating another
runtime home. A route needs verification on a live child first.

## Credentials

Router's environment file is passed to children as documented in
[the example configuration](../examples/router.env.example). Keep tracing
credentials out of that file.

## Candidate routes (NOT CHECKED)

- Codex: supplying the plugin hook and its trust through session flags (`-c`).
- Claude: whether plugins given with `--plugin-dir`, or hooks given through
  `--settings`, run under `--safe-mode`.

If a route is verified, it would set the plugins' metadata channels, verified from
pinned source only:

- Codex plugin 0.4.2: `LANGFUSE_CODEX_METADATA`, a JSON object whose values become
  strings ([config loader](https://github.com/langfuse/codex-observability-plugin/blob/586910c81eeaf21d8f54c1c86c89a29a5466c1ef/plugins/tracing/src/config.ts)),
  carrying `router_task_id`, `router_host_id` and any declared `work_item_id`.
- Claude plugin 1.2.1: `CC_LANGFUSE_TRACE_TAGS`, at most 20 tags of at most 200
  characters each ([hook source](https://github.com/langfuse/Claude-Observability-Plugin/blob/b5b7cc364996467e856dc17575b5674f4799ad87/hooks/langfuse_hook.py)).

Router's counters would stay as they are; analysis must pick one source for each
total and never add Router and Langfuse totals together.
