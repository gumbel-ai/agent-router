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
Each child launch, including a rejected or resumed attempt, records the flag and
reason for the environment that child receives; the correlation tuple and earlier
attempt records are unchanged. A launched child's tracing record is kept through
final capture and restart recovery even if the repository opts out later; a
later launch from an opted-out repository records no tracing.

## Codex route

Codex children normally run with `--ignore-user-config`, so a repository-wide
`--tracing` request remains unavailable unless the operator explicitly selects
the audited Codex plugin. Add one private repository setting (outside the
delegated tree):

```json
{"codex_tracing":"{\"plugin_dir\":\"/approved/codex-langfuse-plugin\"}"}
```

Router verifies the Langfuse Codex Observability Plugin 0.4.2 source digest,
then supplies its Stop hook and exact native session-flags trust entry through
the child command's inline Codex configuration. It forwards only the selected
child's documented Langfuse environment inputs and bounded metadata containing
the Router task and host. Codex emits its native session/thread ID after launch;
Router reconciles that ID into the local correlation record, while the plugin
emits it on the Langfuse observations. `LANGFUSE_CODEX_USER_ID=agent-router` prevents
fallback to a personal Codex-auth email. Router usage and cost accounting remain
unchanged.

Codex's plugin destination is still operator-managed; Router does not read or
copy `~/.codex/langfuse.json`. The task's `tracing.available` flag is true only
when the required startup credentials are present. Changed or missing plugin
source fails closed rather than launching an untraced replacement. A repository
without the explicit selection retains the visible unavailable reason below.

A live check on a Linux host over SSH ran a Router-launched Codex 0.163.0 child
through accept, integrate and close; Langfuse received its trace with Router task,
host and work-item metadata plus usage and cost. Remote-child tracing depends on
#54, where the remote host's Router launches the child through this same path.

## Other executors

Claude remains explicitly unavailable for the default route:

| Executor | Reason recorded when tracing is requested |
| --- | --- |
| Claude | Default children run with `--safe-mode`; the explicit candidate below uses `--restricted` and a selected plugin, but real tracer exports remain unverified. |

Their tracing request is retained in task and attempt history with `available:
false`; their child environments also strip Langfuse variables. The explicit
Claude candidate below is the only exception. No executor fallback is attempted.

## Verification boundary

The route and environment/configuration boundary are covered by unit and runtime
tests. A live Router-launched child reconciled the native OpenCode
session/message/model/token records with its Langfuse generation and confirmed the
three `resourceAttributes.router.*` values. Requested OpenCode tracing is recorded
as `available: true` only when the required Langfuse public and secret keys are
present in the startup environment; otherwise the route remains visible with
`available: false` and an explicit credential diagnostic. Remote
children remain out of scope.

## Explicit Claude candidate (live verification required)

An operator repository entry may contain one `claude_tracing` JSON string:

```json
{"claude_tracing":"{\"plugin_dir\":\"/approved/langfuse-plugin\"}"}
```

The directory must be outside the delegated repository and contain the exact
audited Langfuse Observability Plugin 1.2.1 source. Router verifies the pinned
manifest, hooks and digest, freezes the private selection per task, and refuses
launch if the source changes. The candidate uses `--restricted`, `--plugin-dir`
and empty setting sources.

Claude loads a `--plugin-dir` plugin under the `langfuse-observability@inline`
identity, and native plugin options configured for an installed identity are not
delivered to it. Credentials therefore follow the OpenCode route: set
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and optionally `LANGFUSE_BASE_URL`
and `LANGFUSE_USER_ID` in Router's startup environment (not a committed file).
Router forwards the same documented `LANGFUSE_*` inputs as the OpenCode route
only to the opted-in Claude child phase; the pinned plugin reads plain
environment values before plugin options. Because it reads only
`LANGFUSE_BASE_URL`, Router maps `LANGFUSE_BASEURL` to it when the canonical name
is absent. Setup, default and non-selected Codex children and
all Git subprocesses still remove `LANGFUSE_*`. As with OpenCode, the secret is
present in the child process environment, so child tools can read it. Router
does not read credential files or persist credential values. `tracing.available`
is true only when the startup environment has the public and secret keys. Like
OpenCode, each child launch, including a rejected or resumed attempt, refreshes
the flag and reason for the environment that child receives.

Selected children strip ambient plugins, hooks, plugin options, `CC_LANGFUSE_*`
variables and attached trace context, while preserving native authentication.
Router sets `CC_LANGFUSE_STATE_DIR` to private task storage,
`CC_LANGFUSE_TRACE_SEED` to the child session ID, and `CC_LANGFUSE_TRACE_TAGS`
to the Router task and host identifiers plus a bounded work-item tag when
supported. Router usage and cost accounting remain unchanged, and
`traced: "unknown"` remains until an exporter is independently verified.

Running `router init` without `--tracing` also removes `claude_tracing`. A task
selected before such a revocation refuses its next launch instead of continuing
untraced, and selections recorded by earlier builds of this candidate (schema 1,
native plugin options) refuse to resume; start a new task.

The required live checks—a fresh Router Claude export, a rejected/resumed
export, environment isolation for non-selected processes, and changed-plugin
refusal—remain pending on an approved Claude environment. Synthetic hook tests
do not count as tracer-export evidence.
