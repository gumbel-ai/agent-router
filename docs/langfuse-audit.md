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
Router now pins the fork [`gumbel-ai/claude-observability-plugin` at `d5829be6628cb67dc45eca699575348a152ae565`](https://github.com/gumbel-ai/claude-observability-plugin/tree/d5829be6628cb67dc45eca699575348a152ae565): this source plus one change that saves hook state only after a successful flush.

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

### OpenCode plugin 0.5.2 source audit

The official [Langfuse OpenCode integration](https://langfuse.com/integrations/developer-tools/opencode)
provides an existing tracing route. This audit read plugin 0.5.2 at
[`24389efd13decfb72e103950526669addcb245e8`](https://github.com/langfuse/opencode-observability-plugin/tree/24389efd13decfb72e103950526669addcb245e8),
released October 8, 2026. That source-only pass did not install or run OpenCode
or the plugin. A later follow-up used the pinned 0.5.0 route actually launched by
Router and is recorded below; the source-only statements in this section are not
the current live verification status.

The pinned [compatibility declaration](https://github.com/langfuse/opencode-observability-plugin/blob/24389efd13decfb72e103950526669addcb245e8/README.md#supported-versions)
supports OpenCode 1.18.29 or newer through `plugin` configuration and OpenCode
2.0.4 or newer through `plugins`. These are upstream plugin compatibility
claims. They do not establish Router execution-adapter compatibility.

| Capability | Pinned source evidence | Live result |
| --- | --- | --- |
| Session and parent IDs | Observations carry native `session.id`. Turn metadata contains `messageID` and optional explicit `parentSessionID`; `agent`/`mode` are names, not unique agent IDs. [Emitter](https://github.com/langfuse/opencode-observability-plugin/blob/24389efd13decfb72e103950526669addcb245e8/src/langfuse.ts#L520) | NOT CHECKED |
| Caller metadata | The credential schema exposes destination, environment, user ID and service name; no general Router task metadata input exists. The official plugin reads standard OTel resource attributes and preserves them in Langfuse observation metadata. Router uses `OTEL_RESOURCE_ATTRIBUTES` for non-secret `router.task_id`, `router.host_id`, and `router.child_session_id`. [Runtime](https://github.com/langfuse/opencode-observability-plugin/blob/24389efd13decfb72e103950526669addcb245e8/src/runtime.ts), [resource setup](https://github.com/langfuse/opencode-observability-plugin/blob/24389efd13decfb72e103950526669addcb245e8/src/langfuse.ts#L1765) | VERIFIED in direct and Router-child traces |
| Content and truncation | Prompts, outputs, reasoning, tool arguments/results and history can leave the host. V2 context also includes system instructions. No plugin-wide capture limit was found. [V1 hooks](https://github.com/langfuse/opencode-observability-plugin/blob/24389efd13decfb72e103950526669addcb245e8/src/v1.ts), [V2 hooks](https://github.com/langfuse/opencode-observability-plugin/blob/24389efd13decfb72e103950526669addcb245e8/src/v2.ts#L63) | NOT CHECKED |
| Usage and cost | Generation observations copy native model/provider, message IDs, token buckets and cost. Missing native `total` falls back to input + output + reasoning. Do not combine Router and Langfuse totals. [Generation emitter](https://github.com/langfuse/opencode-observability-plugin/blob/24389efd13decfb72e103950526669addcb245e8/src/langfuse.ts#L680) | NOT CHECKED |
| Flush and resume | V1 flushes on idle/disposal; V2 flushes on execution completion/interruption/failure and disposal. Deduplication state is in memory; there is no confirmed durable replay contract. [V1 hooks](https://github.com/langfuse/opencode-observability-plugin/blob/24389efd13decfb72e103950526669addcb245e8/src/v1.ts#L49), [V2 completion](https://github.com/langfuse/opencode-observability-plugin/blob/24389efd13decfb72e103950526669addcb245e8/src/v2.ts#L249) | NOT CHECKED |
| Credentials | Environment keys precede the fixed home credential file. The loader does not validate file ownership, mode or symlinks. It does not copy file credentials into the environment. [Loader](https://github.com/langfuse/opencode-observability-plugin/blob/24389efd13decfb72e103950526669addcb245e8/src/runtime.ts#L34) | Tool access and inheritance NOT CHECKED |

### Router-launched OpenCode follow-up (2026-10-19)

An approved disposable repository ran one real Router child through the pinned
OpenCode 2 plugin and existing OpenRouter/Langfuse project credentials. The child
returned `ROUTER_OTEL_CORRELATION_OK` with exit code 0. Native OpenCode and
Langfuse generation records reconciled on model `z-ai/glm-5.3-flash`, input 8,408,
output 10, reasoning 184, total 8,602 and cost `0.000434624`.

The Langfuse observations for native session
`sesb982e5d3a8744e4594002bdbbcc18486` exposed all three exact join values in
metadata: `resourceAttributes.router.task_id`,
`resourceAttributes.router.host_id`, and
`resourceAttributes.router.child_session_id`. The direct child-session query and
Router's local `correlation --json` record therefore agree without using prompt
metadata or credentials. This closes the Router task/host/session correlation gap
for the local OpenCode route; remote children remain out of scope.

#### Router compatibility and verification handoff

Router currently invokes `run --standalone --auto --session` with a preallocated
session ID. The pinned public OpenCode 1.18.29
[CLI parser](https://github.com/anomalyco/opencode/blob/16747470f976aca3d362ad730bcd3fe82ecc2c9a/packages/opencode/src/index.ts#L116)
is strict; its [run command](https://github.com/anomalyco/opencode/blob/16747470f976aca3d362ad730bcd3fe82ecc2c9a/packages/opencode/src/cli/cmd/run.ts#L125)
does not declare `--standalone`, and
[`--session` retrieves an existing session](https://github.com/anomalyco/opencode/blob/16747470f976aca3d362ad730bcd3fe82ecc2c9a/packages/opencode/src/cli/cmd/run.ts#L456).
This public-source mismatch must be resolved before claiming Router child
coverage. The operator-selected executable may differ; its version and behavior
were **NOT CHECKED**. Do not remove flags or retry an uncertain start as a fallback.

OpenCode receives Router's permission-only `OPENCODE_CONFIG_CONTENT` override.
Upstream [configuration loading](https://github.com/anomalyco/opencode/blob/16747470f976aca3d362ad730bcd3fe82ecc2c9a/packages/opencode/src/config/config.ts#L482)
merges that override with other configuration, so it does not prove plugins are
disabled. Router's Codex `--ignore-user-config` behavior does not describe OpenCode.

Ask a teammate with an approved synthetic tracing destination to perform these
checks after separate data-handling approval:

1. Record the exact Router head, OpenCode executable/version and pinned plugin
   version. Check command flags and session creation/resume semantics before any
   model call. Stop if the invocation is unsupported.
2. In a disposable `example-project`, enable only the pinned official plugin with
   the correct version-specific key. Preserve authentication and permissions;
   keep credentials out of the tool environment and public artifacts.
3. Run one synthetic child and resume that same native session. Compare native
   session/message IDs, every model call, token/cache/reasoning buckets and cost
   with the resulting observations. Detect duplicated prior generations.
4. Check normal flush, a harmless tool failure, effective worktree configuration,
   captured synthetic content and credential access boundaries. Report every
   omitted lifecycle, outage, SSH or independent reboot scenario as **NOT CHECKED**.

Missing joins or unreconciled calls remain unknown. No trace was verified by this
audit, and no custom Router tracing adapter is justified by this source evidence.

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

## Follow-up Codex live check

On 2026-10-19, a Linux host over SSH ran Codex 0.163.0 with the pinned
Langfuse plugin 0.4.2 and a synthetic Router metadata payload. A native
session-flags Stop hook was supplied without `--dangerously-bypass-hook-trust`;
the same hook ran on a fresh invocation and its resumed invocation. Langfuse
v2 observations contained the expected Router task/host/work-item metadata and
the native Codex thread ID. The fresh and resumed traces had distinct trace IDs,
non-zero generation usage/cost, and retained the same native session ID.

A Router-launched Codex child using the explicit `codex_tracing` route then ran
on the same host, exited 0 and went through accept, integrate and close. Its
Langfuse trace carried `router_task_id`, `router_host_id` and `work_item_id`
with usage and cost. Remote-child execution remains NOT CHECKED (see #54).
