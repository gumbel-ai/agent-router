# Child tracing

Child tracing is off unless the target repository was initialized with
`router init --tracing`. Router never uploads traces itself. When tracing is
requested, the OpenCode 2 route adds the pinned official
`@langfuse/opencode-observability-plugin@0.5.0` to the child configuration;
the plugin performs any export using its own documented OpenTelemetry route.
The route loads on the installed OpenCode executable, but live export and
Router-to-Langfuse correlation remain explicitly unverified until an approved
project produces a reconciled trace.

```sh
router init --executor opencode --model provider/exact \
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
`LANGFUSE_USER_ID`. Router does not read, copy, or persist credential values.
Without tracing, all `LANGFUSE_*` variables are removed from the child; the
plugin is not added by Router. An operator-installed plugin or credential file
in the user's own OpenCode configuration remains outside Router's control.

The plugin emits the native OpenCode `session.id` on observations. Router records
the requested session value as `child_session_id` and stores the candidate join
pair `(host_id, router_task_id, child_session_id)` locally. The plugin's supported
metadata inputs do not yet provide a verified Router-task tag channel, so this is
not evidence that a Langfuse trace can be joined. Router does not invent a prompt
metadata field or put credentials in the trace. A resumed attempt reuses the same
requested session ID.

Router's usage and cost counters remain the accounting source for Router tasks;
Langfuse observations must not be added to them. Missing credentials cause the
upstream plugin to disable its exporter; Router does not treat the absence of
credentials as permission to expose or persist them.

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
tests. A live model session still requires an approved Langfuse project and must
reconcile native OpenCode session/message/model/token records with the resulting
trace, including an approved correlation-tag strategy, before claiming
end-to-end export coverage. Until then, requested OpenCode tracing is recorded as
`available: false` with the unverified reason. Remote children remain out of
scope.
