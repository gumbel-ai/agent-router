# Child tracing

Child tracing is off unless the target repository was initialized with
`router init --tracing`. Router never uploads traces itself. When tracing is
requested, the OpenCode 2 route adds the pinned official
`@langfuse/opencode-observability-plugin@0.5.0` to the child configuration;
the plugin performs any export using its own documented OpenTelemetry route.
The route has been live-verified against native OpenCode records and a
Router-launched child trace.

```sh
router init --executor opencode --model worker=provider/exact \
  --setup-command 'true' --permissions '{"*":"allow"}' --tracing --yes
```

Running `router init` without `--tracing` removes the repository's tracing choice.
The permission object is always copied unchanged into the child
`OPENCODE_CONFIG_CONTENT`; tracing does not broaden or replace it.

## OpenCode 2 route

For a requested OpenCode child, Router sets:

```json
{
  "permission": {"...": "the configured allow/deny profile"},
  "plugins": ["@langfuse/opencode-observability-plugin@0.5.0"]
}
```

The plugin's documented credential inputs are passed only to a requested
OpenCode child: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`
(or its documented `LANGFUSE_BASEURL` alias), `LANGFUSE_ENVIRONMENT`, and
`LANGFUSE_USER_ID`. Router forwards these existing environment inputs only for
that child; it does not read credential files or persist credential values.
All Git validation, snapshot, integration and cleanup subprocesses remove
`LANGFUSE_*` variables, including Git hooks. Without tracing, these variables are
also removed from the child, and Router does not add the plugin. An
operator-installed plugin or credential file
in the user's own OpenCode configuration remains outside Router's control.

The plugin emits the native OpenCode `session.id` on observations. Router records
the requested session value as `child_session_id` and stores the join tuple
`(host_id, router_task_id, child_session_id)` locally. For a requested OpenCode
child, Router also sets the standard `OTEL_RESOURCE_ATTRIBUTES` environment
variable to the three non-secret `router.*` identifiers. The official plugin
preserves those resource attributes in Langfuse observation metadata as
`resourceAttributes.router.host_id`, `resourceAttributes.router.task_id`, and
`resourceAttributes.router.child_session_id`. This provides a direct trace join
without putting credentials or metadata into the prompt. A resumed attempt reuses
the same requested session ID and resource attributes.

Router's usage and cost counters remain the accounting source for Router tasks;
Langfuse observations must not be added to them. Missing credentials cause the
upstream plugin to disable its exporter; Router does not treat the absence of
credentials as permission to expose or persist them.

The task's `tracing.available` flag is conservative and is true only when the
startup environment contains the plugin's required public and secret keys. Router
does not read the optional user credential file, so file-only configuration remains
an operator-controlled export path and is not claimed as Router-verified readiness.

## Other executors

Codex and Claude remain explicitly unavailable for this route:

| Executor | Reason recorded when tracing is requested |
| --- | --- |
| Codex | Children run with `--ignore-user-config`; tracing plugin hooks need trust from that ignored user layer. |
| Claude | Children run with `--safe-mode` and no setting sources; installed plugins and hooks do not load. |

Their tracing request is retained in task and attempt history with `available:
false`; their child environments also strip Langfuse variables. No executor
fallback is attempted.

## Verification boundary

The route and environment/configuration boundary are covered by unit and runtime
tests. A live Router-launched child reconciled the native OpenCode
session/message/model/token records with its Langfuse generation and confirmed the
three `resourceAttributes.router.*` values. Requested OpenCode tracing is recorded
as `available: true` only when the required Langfuse public and secret keys are
present in the startup environment; otherwise the route remains visible with
`available: false` and an explicit credential diagnostic. Remote
children remain out of scope.
