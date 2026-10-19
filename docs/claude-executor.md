# Claude Code children

Router can dispatch `executor: "claude"` using the same task authorization, owned
process gate, isolated Git worktree, concurrency cap, per-attempt deadline, explicit
parent review, and launch ceiling as its other executors. A failed launch never
selects another executor or model. Parent rejection is the only retry path.

Configure an exact versioned model and explicit permissions in the target project:

```sh
router init --executor claude --model worker=anthropic/claude-sonnet-4-6 \
  --setup-command 'true' \
  --permissions '{"tools":["Read","Edit","Write"],"allow":["Read","Edit","Write"]}' \
  --concurrency 2 --timeout 120 --attempts 2 --yes
```

The CLI must be installed and authenticated by the operator. Router checks
`claude --version` in a persisted, release-gated owned phase before preparing a
child and requires 2.1.303 or newer. Inspection has a five-second deadline within
the attempt deadline; its disposable process group is stopped before version
output is accepted, including descendants left by a configured launcher.
`AGENT_ROUTER_CLAUDE_COMMAND` must contain exactly one executable path and no
configured arguments. For example, use `["claude"]` or one operator-owned wrapper
executable; interpreter/script prefixes and native option prefixes are refused
before any phase. This prevents configured arguments from consuming or hiding
Router's version and permission options. A wrapper must forward Router's arguments
unchanged, preserve normal native authentication, and never independently start
provider work. Each new attempt resolves the operator-selected executable using
Router's PATH and the parent working directory, follows symlinks, and persists that
absolute path before inspection. Inspection and child execution use that path;
worktree PATH entries and setup-created shadows cannot substitute another CLI.
Every new attempt repeats inspection, including after restart or configuration
changes. Recovery of an already-running phase uses that attempt's persisted path
and version rather than the current command setting. A legacy in-progress attempt
without a binding is refused before launching another provider child; it is never guessed. Router cannot validate an operator-owned wrapper's implementation.
Cancellation and restart use that same recorded ownership. Uncertain ownership
retains the task reservation and requires operator inspection. CLI help
was inspected on 2.1.303; authenticated execution remains **NOT CHECKED** in the
source implementation. Versions before 2.1.277 used different resumed cost
semantics and are refused. An unsupported CLI or unavailable model fails explicitly.
The model grammar requires `anthropic/claude-family-version` with numeric version
components, optionally followed by a numeric revision; floating aliases such as
`sonnet`, `opus`, and `default` are refused. This validates a pinned identifier,
not whether the account can access that model.

The permission object has exactly `tools` and `allow` lists. Supported built-in
tools are `Read`, `Edit`, `Write`, `Bash`, `Glob`, `Grep`, and `NotebookEdit`.
For shell access, explicitly add `Bash` to `tools` and approved Claude permission
rules to `allow`. Router passes `dontAsk`: any action requiring additional approval
is denied. The operator owns these rules; permitting arbitrary Bash permits shell
access under the operator's account. The worktree isolates changes, and is not an
operating system sandbox. Dependency setup runs under that account too.

Router starts a print-mode stream with the configured model and a fresh, persisted
UUID. Only explicit parent feedback resumes that UUID in the same worktree. Missing
or changed native identity, a different reported main model, a successful result reporting only other models,
a failed result, or a
missing init/result fails the task even if the process exits zero. No replacement
session is launched. Session state remains in the operator-selected Claude config
home; Router does not select or discover another home.

The child explicitly disables availability fallback chains and automatic model
switching on flagged requests; noninteractive refusals fail the task. It uses safe
mode, no user/project/local settings sources, an empty strict
MCP configuration, the explicit tools list, and disabled slash commands. Custom
hooks, skills, and plugins therefore do not load. Managed policy still applies. If policy or the native CLI remaps the requested
model, Router reports the observed mismatch as failure; it never bypasses policy.
Router retains the normal Claude login or configured API key and selected
`CLAUDE_CONFIG_DIR`, and removes the parent's nested-session markers, including
`CLAUDE_CODE_CHILD_SESSION`, and alternate model/provider/endpoint overrides.
Configured option prefixes such as `--no-session-persistence` are refused, and a
nonempty `CLAUDE_CODE_SKIP_PROMPT_HISTORY` is refused before version inspection: resumable children cannot honor that
privacy policy, and Router does not silently override it. The
[tracing opt-in](tracing.md) is separate and currently records Claude tracing as unavailable.

Claude may launch ordinary foreground tools in separate process groups. Router
first interrupts the harness so it can close them. A negative CLI exit, missing
completion evidence, or forced termination leaves `interrupted`,
`execution_stopped: false`, and `shutdown_unconfirmed: true`, including after a
restart. The task keeps its capacity reservation and cannot be closed or
automatically relaunched. Inspect and stop remaining foreground commands, stop
the daemon, then use
`router recover TASK_ID --reason 'Inspected and stopped foreground commands'`
with the required interactive confirmation. Router never claims an explicitly detaching launcher
represents its detached job.

## Accounting

Router reads only structured `system/init`, `assistant`, and `result` events for
identity and accounting. It takes tokens from `result.modelUsage`, including cache
read/write buckets, and cost from `result.total_cost_usd`. It does not add assistant
message usage or per-model costs to that aggregate. Repeated results replace the
same attempt's values. Resume results carry session totals on the supported CLI;
Router stores version-1 internal baselines containing at most 32 models, bounded
model identifiers, and four integer token buckets per model. Each bucket's
cumulative sum across models, including persisted baselines, must fit the shared
wire integer bound (`2**63 - 1`); checking only an attempt delta is insufficient.
It validates every retained model and bucket before summing their deltas: another model's growth
cannot hide a missing or decreased bucket. A newly observed model starts at zero.
The existing task aggregation then adds attempt deltas. Previously published
aggregate-only baselines are upgraded to the versioned representation with token
continuity unknown; Router never invents their per-model history. Independently
valid native aggregate cost deltas remain usable and are never added to per-model
costs. Missing, nonfinite, negative, decreased, or incomplete values remain
unavailable; a crash result is not reported as zero cost. These internal maps and
executable bindings are omitted from public task output and attempt history.

Retained completed attempts are validated again when read, including records from
older versions: each frame must contain the four bounded integer token buckets,
and their combined total must fit the same bound. Invalid frames contribute no
usage; valid prior partial usage can remain visible with `usage_complete: false`.
An overflowing combined total is unavailable. This temporary public projection
also covers attempt history and never rewrites stored evidence or aggregate cost.

These costs are Claude CLI estimates, not billed spend. No prompts, credentials,
or process environments enter accounting metadata. Existing private stdout/stderr
artifacts retain the child's output under the established task retention policy.

The schema and resume behavior follow the official
[headless CLI](https://code.claude.com/docs/en/headless),
[CLI reference](https://code.claude.com/docs/en/cli-reference),
[model fallback controls](https://code.claude.com/docs/en/model-config), and
[cost accounting](https://code.claude.com/docs/en/agent-sdk/cost-tracking) contracts.

## Provider verification: NOT CHECKED

A teammate with approved Claude access must run this against the PR's exact head.
Use a disposable enabled `example-project`, synthetic instructions, and an
operator-approved exact model. Do not post transcripts, credentials, actual host
inventories, or personal paths to the public PR.

1. Record the Router commit, operating system, `claude --version`, and exact model
   `anthropic/claude-sonnet-4-6`. If the account cannot access that model, report the
   explicit failure; agree another exact model before testing, without fallback.
2. Run the configuration example above. In a normally authorized Claude parent,
   ask Router's delegation skill to start a Claude child with model alias `worker`
   and a synthetic task to create `result.txt` containing `first`. Use a unique
   request ID. Inspect `router status --json`: the task reaches `awaiting_review`,
   has the expected resolved model and native child session UUID, and the parent
   checkout has no `result.txt`. Inspect the private init event locally to confirm
   the exact model and configured tools, no extra MCP servers/plugins, and the
   recorded task worktree as the child's working directory.
3. Reject attempt 1 through the normal parent review operation with feedback to
   change the file to `second`. Confirm attempt 2 keeps the worktree and native UUID,
   reaches `awaiting_review`, and records two attempts. Check private result events:
   the public task tokens/cost equal the latest cumulative session totals, not the
   sum of both session totals. Cache buckets remain separate. No extra launch occurs.
4. Reject once more and confirm the launch ceiling refuses attempt 3. Accept or
   discard the existing result using the normal review/close flow.
5. Before the shutdown checks, initialize a second disposable enabled project with
   `Bash` included in `tools` and an operator-approved allow rule for the specific
   foreground shell command. Confirm its private init event includes `Bash`.
   Keep the original Read/Edit/Write-only project for the tool-denial check.
   In the second project, use a synthetic task with a short approved deadline and
   request foreground shell work that will exceed it. Confirm graceful harness interruption, owned
   process shutdown, retained worktree, and no relaunch. On a disposable task,
   abruptly stop the native CLI while its foreground command runs: confirm
   `interrupted`, retained capacity, refused close, and the operator recovery
   procedure above. Repeat after restarting Router; forced escalation must remain
   unconfirmed even if the launcher returns zero. In the original Read/Edit/Write-only
   project, request `Bash` and confirm it is denied rather than prompting or bypassing permissions.
6. Repeat fresh/resumed coverage with the operator's actual approved authentication
   method and selected Claude config directory. Restart Router between completed
   attempts and confirm baseline/session persistence. Report sanitized PASS/FAIL
   for each step and the exact tested commit to the PR.

Physical Linux/macOS Claude launches, actual authentication, native permission
behavior, provider failures, and real fresh/resumed accounting remain **NOT CHECKED**
until that evidence is attached. CI exercises these contracts with separate local
fixture processes and worktrees; it makes no model requests.
