# Langfuse integration audit

Audit date: 2026-10-18. This is a source and installation audit, not live trace
reconciliation. Router does not query or upload to Langfuse. Session coverage,
resumed sessions, child sessions, content capture and costs remain **NOT CHECKED**.

## Host A

Host A is the local audit target. Its private inventory and identity mapping stay
outside this repository. Only the selected client homes were inspected.

| Capability | Claude Code | Codex |
| --- | --- | --- |
| Client version checked | 2.1.302 | 0.163.0 |
| Installed Langfuse plugin | Absent in native plugin list and installation registry | Absent in selected-home configuration and plugin cache |
| Live session/agent IDs | NOT CHECKED | NOT CHECKED |
| Live metadata channel | NOT CHECKED | NOT CHECKED |
| Live truncation | NOT CHECKED | NOT CHECKED |
| Live hook environment | NOT CHECKED | NOT CHECKED |

No fleet configuration or remote targets were supplied for this audit. Remote
installation versions, reachability and behavior are **NOT CHECKED**. Add a separate
anonymized table for each audited target; keep the table-to-host mapping private.
Absence on Host A establishes nothing about another host.

## Pinned source capabilities

These public checkouts were read without installing or running either plugin.
Source support does not establish that a host has enabled, trusted or exercised it.

### Claude plugin 1.2.1

Pin: [`langfuse/Claude-Observability-Plugin` at `b5b7cc364996467e856dc17575b5674f4799ad87`](https://github.com/langfuse/Claude-Observability-Plugin/tree/b5b7cc364996467e856dc17575b5674f4799ad87).

The [hook source](https://github.com/langfuse/Claude-Observability-Plugin/blob/b5b7cc364996467e856dc17575b5674f4799ad87/hooks/langfuse_hook.py)
propagates the native session ID as Langfuse `session_id`, with `metadata.session_id`
on the turn observation. In attached mode, trace-level session, user and tag
propagation is skipped; observation `metadata.session_id` remains. Standard
subagent metadata contains transcript basename,
type and description, without an explicit native `agent_id` or `parent_agent_id`.
A basename or nearby timestamp does not establish an authoritative agent join.

`CC_LANGFUSE_TRACE_TAGS` accepts a JSON array or comma-separated values: at most
20 tags, each at most 200 characters. No general caller metadata-object channel was
found. `CC_LANGFUSE_MAX_CHARS` defaults to 20,000 for text passed to `truncate_text`;
it is not a universal recursive payload limit. Some truncation metadata carries
original length and a digest; structured inputs, images and history need separate
coverage checks.

Environment values precede `CLAUDE_PLUGIN_OPTION_*` fallback values. This pin does
not check `TRACE_TO_LANGFUSE`: usable credentials permit export when the plugin runs.
Scope plugin enablement explicitly; do not rely on that environment flag alone.

The [hook declaration](https://github.com/langfuse/Claude-Observability-Plugin/blob/b5b7cc364996467e856dc17575b5674f4799ad87/hooks/hooks.json)
runs on Stop and SessionEnd, selecting `uv` from PATH or falling back to `python3`.
The [runtime requirements](https://github.com/langfuse/Claude-Observability-Plugin/blob/b5b7cc364996467e856dc17575b5674f4799ad87/README.md#prerequisites)
are Python 3.10+ and `langfuse>=4.7,<5`. Interpreter selection and inherited
`UV_PYTHON`/PATH still require host verification; no personal interpreter fixes were
applied here.

### Codex plugin 0.4.2

Pin: [`langfuse/codex-observability-plugin` at `586910c81eeaf21d8f54c1c86c89a29a5466c1ef`](https://github.com/langfuse/codex-observability-plugin/tree/586910c81eeaf21d8f54c1c86c89a29a5466c1ef).

The [rollout parser](https://github.com/langfuse/codex-observability-plugin/blob/586910c81eeaf21d8f54c1c86c89a29a5466c1ef/plugins/tracing/src/parse.ts)
reads native `session_meta.id` as the session/thread ID; absent IDs become the
literal `unknown`, which is not a usable join key. Explicit `parent_thread_id`
and spawn results can identify child threads.
The [emitter](https://github.com/langfuse/codex-observability-plugin/blob/586910c81eeaf21d8f54c1c86c89a29a5466c1ef/plugins/tracing/src/trace.ts)
propagates `sessionId` for ordinary turn traces and emits `codex.thread_id` on turn
observations, including children. Child observations can share the root trace;
the trace's session ID alone does not identify every nested child. Attached mode
does not propagate the ordinary trace-level session, tags or caller metadata.

The [configuration loader](https://github.com/langfuse/codex-observability-plugin/blob/586910c81eeaf21d8f54c1c86c89a29a5466c1ef/plugins/tracing/src/config.ts)
accepts a JSON object through `LANGFUSE_CODEX_METADATA` or the JSON `metadata` key;
values normalize to strings. Supply `runtime`, `router_host_id` or
`router_task_id` explicitly; the plugin does not invent Router identities.
Precedence is defaults, home JSON, current-directory `.codex/langfuse.json`, then
environment. Parent directories are not searched. `CODEX_HOME` selects the auth
file; global Langfuse JSON still uses the home `.codex` directory. Router honors
the selected Codex home without discovering others.

Without an explicit `user_id`, the plugin reads the auth-token email and exports
it as the ordinary trace's user ID. This is source behavior, not a live check.
Project JSON can enable tracing and select `base_url` while retaining inherited
credentials. The [exporter](https://github.com/langfuse/codex-observability-plugin/blob/586910c81eeaf21d8f54c1c86c89a29a5466c1ef/plugins/tracing/src/instrumentation.ts)
uses that destination: trusting the hook does not validate its configuration.

This pin removed truncation: `LANGFUSE_CODEX_MAX_CHARS`/`max_chars` do not impose
a capture limit. Its [configuration tests](https://github.com/langfuse/codex-observability-plugin/blob/586910c81eeaf21d8f54c1c86c89a29a5466c1ef/plugins/tracing/test/config.test.ts)
explicitly accept and ignore stale `max_chars`. Live truncation remains
**NOT CHECKED**; do not assume a cap. The [Stop hook](https://github.com/langfuse/codex-observability-plugin/blob/586910c81eeaf21d8f54c1c86c89a29a5466c1ef/plugins/tracing/hooks/hooks.json)
uses Node with a 30-second timeout. [Codex hooks require manual trust](https://developers.openai.com/codex/hooks);
this audit wrote or bypassed none.

## Hook environment and correlation boundaries

Codex 0.163.0's [command runner](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/hooks/src/engine/command_runner.rs)
replays a captured session environment plus hook overrides, filtering
[non-inheritable names](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/protocol/src/shell_environment.rs).
That differs from tool-shell environment policy. A new launcher's environment is
not proof that an already-running app-server sees its values. Actual delivery,
login-shell PATH changes and resumed-child behavior are **NOT CHECKED**.

External analysis must map Router's existing host ID explicitly and join exact
runtime, mapped host and native session identities. Keep runtime separate from
model provider. Missing, ambiguous or mismatched identities stay unknown; time
proximity and display labels never establish parentage. Correlation must leave `traced` unknown; Router does not consult Langfuse.

Verification used installation/cache inspection, pinned source reading, and six
credential-free checks of AST-extracted Claude tag/truncation functions. No plugin
module was imported, no tracing plugin was installed, no tracing credentials or real
session content were used, and no trace was uploaded. These checks establish
source behavior only; all live capture and remote-host claims remain NOT CHECKED.
