# Code Review Process

This process is mandatory for changes that span multiple layers, alter persisted state,
handle untrusted input, or affect lifecycle, capacity, concurrency, or operator-visible
status. A passing test suite alone is not evidence that such a change is complete.

## Why This Exists

Milestone A was declared ready before it had been reviewed end to end. Later reviews
found real defects in redaction boundaries, fleet-level capacity reclassification,
out-of-order lifecycle events, active totals, partial-state rendering, and database
snapshot consistency. A later review cycle then found additional defects in shutdown
ownership, transport deadlines, remote-schema projection, privacy boundaries, numeric
validation, duplicate identity handling, and restart semantics.

The failure was not simply a lack of tests. The initial review validated components in
isolation and trusted existing tests even when they encoded incorrect behavior. It did
not consistently follow data through every production layer or challenge timing,
truncation, restart, malformed-input, and concurrency boundaries.

Not every subsequent review comment represented a distinct production defect, but the
number of valid findings showed that the initial completion claim was unjustified.
Repeated findings in one subsystem are evidence of a missed invariant, not merely a
queue of isolated lines to patch.

A third cycle repeated the pattern, and two of its findings were regressions introduced
by fixes from the previous round. The recurring defect classes are listed in
[Known defect patterns](#known-defect-patterns); check every applicable item before
asking for external review.

## Preventing Review Churn

PR #4 drew more than 25 external review threads across five rounds. Three of those
findings were regressions introduced by earlier fixes. Most were variants of a bug
already fixed once. These rules address the causes, not the symptoms.

1. **Keep changes reviewable.** Split work into PRs of a few hundred production lines,
   one subsystem each, and merge each before starting the next. A large multi-subsystem
   diff guarantees that every review pass samples a different edge case.
2. **Remove whole defect classes by design.** Where an approach has unbounded corner
   cases, change the structure rather than patching instances:
   - forward only an allowlist of known-safe fields instead of scrubbing free text for
     secrets;
   - express lifecycle rules as one explicit transition table (state and event to next
     state) instead of scattered watermarks and flags;
   - compute a total or a classification in one place, and derive every alert from it.
3. **Review internally before the first push.** Run the independent, adversarial
   reviews in section 6 on the full diff before opening or updating the PR. External
   review (Copilot) is the final check, not the discovery mechanism.
4. **Test families, not examples.** When inputs vary along a dimension (nesting depth,
   event order, token prefix, timing), test across that dimension. Prefer a
   generated or exhaustive table over a hand-picked case.
5. **Set a stopping rule per PR.** Fix P1 and P2 findings in the PR. Record narrow P3
   findings as follow-up issues with a reproduction, unless the fix is trivial and
   local. A reviewer can always find a narrower case; looping on them indefinitely
   adds risk through more fixes.

## Required Review Sequence

### 1. Define verifiable completion criteria

Before implementation, translate the request into observable checks. Include:

- expected behavior and explicit non-goals;
- affected inputs, persisted records, APIs, and operator-visible outputs;
- failure behavior for unavailable, malformed, stale, or partial data;
- commands or tests that will prove completion.

Do not add speculative behavior merely to make the design appear comprehensive.

### 2. Trace every contract end to end

For each new or changed field, state, route, or relation, inspect all applicable stages:

1. source or definition;
2. input normalization and validation;
3. construction and state transitions;
4. persistence and migration behavior;
5. serialization and transport;
6. aggregation or reclassification;
7. rendering or downstream consumption;
8. tests at both component and production-path levels.

A lower layer being correct is insufficient if an earlier layer transforms its input or
a later layer overwrites its result. When a field is removed or renamed, search every consumer across the whole
repository, including `scripts/` live smoke checks, not only `router/` and `tests/`.

### 3. Build an adversarial boundary matrix

Review every applicable dimension, not only the happy path:

| Dimension | Required questions |
| --- | --- |
| Missing or malformed data | Is it rejected or explicitly reported as unknown/partial? Can a fallback hide the failure? |
| Time and freshness | What happens exactly at, before, and after each threshold? Does collection latency change the answer? |
| Ordering | Can delayed, duplicated, or out-of-order events reopen terminal state or overwrite newer evidence? |
| Restart | What remains visible, what becomes uncertain, and what evidence resolves that uncertainty? |
| Bounds and truncation | Is sensitive data transformed safely before truncation? Are totals independent of display limits? |
| Concurrency | Can related reads observe different database snapshots? Are writes idempotent and monotonic? |
| Partial failure | Does one failed host or section remain isolated? Is degraded output visibly marked? |
| Security | Does any text leave its host or reach a terminal without passing the output-safety boundary? Is that intentional and documented? |
| Output safety | Are terminal controls, oversized values, and hostile remote payloads bounded and sanitized? |

If a dimension does not apply, note why rather than silently skipping it.

### 4. Review invariants, not individual comments

For every valid finding:

1. name the violated invariant in plain language;
2. find every producer, transformation, consumer, state transition, and sibling input
   governed by that invariant;
3. build a variant matrix covering those paths and failure modes;
4. fix the invariant at the correct abstraction boundary;
5. verify the original reproduction and the sibling variants.

Do not close a finding with a one-branch condition, regex, or narrow regression test
until the surrounding variants have been checked. After a second valid finding in the
same subsystem, stop patching comments sequentially and perform an invariant-wide
audit of that subsystem.

Examples of invariants that require system-wide checks:

- unknown, partial, malformed, or contradictory evidence must never become healthy or
  available through a later reclassification;
- a terminal or newer state must not be reopened by delayed or same-time events;
- accepted work must have durable or bounded ownership through shutdown;
- timeout and cancellation paths must remain bounded, including cleanup after a kill;
- remote data must be projected onto an explicit local schema; unknown fields must not
  be forwarded merely because they arrived inside a known object;
- values used in arithmetic or JSON output must be finite and bounded;
- truncation must remain visibly partial and must not change aggregate truth;
- identifiers, errors, reservations, summaries, and nested fields must cross the same
  privacy and output-safety boundary as primary payloads.

### 5. Test semantics, not the current implementation

- Treat a surprising existing assertion as a hypothesis, not authority.
- A test count is not quality evidence. Challenge tests that preserve current behavior
  when that behavior conflicts with the contract.
- Add regression tests for every behavior-changing fix.
- Exercise the real production entry point when preprocessing or integration layers exist.
- Test both sides of thresholds and length boundaries.
- Include concurrency tests when consistency depends on transactions or snapshots.
- Verify negative behavior: stale or unknown records must not be counted as active, and
  malformed data must not be reported as available or healthy.
- Add composition tests where independently reasonable behaviors interact. Applicable
  combinations include reordered events plus restart, partial sections plus
  reclassification, slow transport plus freshness, contention plus shutdown, hostile
  nested wire fields plus serialization, and concurrent file or process state.

### 5a. Keep tests proportionate

Each test must protect a distinct contract that a plausible change could break. More
tests are not better tests: every test costs suite time and maintenance, and may pin
the current implementation.

- Before adding a test, look for an existing table-driven or boundary test for the same
  contract, and add a row or a case to it. Escape-sequence formats, threshold boundaries, and
  event orderings belong in tables, not separate methods.
- Write one regression per violated invariant, not one per comment. Sibling variants of
  the same invariant share one parameterized test.
- A test must fail without the fix. If it cannot, it adds no protection; delete it or
  say why it remains. Prove it by reverting only the fix's lines; `git stash` of a whole
  file can pull in unrelated changes and fail for the wrong reason.
- When one test covers several fixes (subtests, multiple observers), revert each fix on
  its own and confirm its subtest fails. Shared fixtures can mask a missing fix: one
  part's uncertainty made capacity unknown for the other part's case too.
- Do not test constants, trivial plumbing, logging text, or private helpers already
  covered through the production entry point.
- Assert observable behavior (persisted state, API output, process outcome), not
  internal call sequences, unless the call sequence is the contract.
- Prefer real components over deep mocks. Mock only process, clock, network, or
  operating-system boundaries that cannot run safely in the test environment.
- When a change makes an existing test redundant, remove or merge it in the same change.
- If a race cannot be reproduced deterministically, do not add a sleep-based "stress"
  test that passes either way. Document the reasoning in the review reply instead.
- Compute fixture boundaries from the data (for example
  `text[:text.index(secret) + 4]`), never from hand-counted offsets. A miscounted
  slice can silently test a different case, such as a terminated value instead of a
  truncated one.
- Make the marker you assert against unique. `"swor"` also matches `"password"`.
- Set the value under test explicitly in every event that carries it. A fixture default
  (for example a model name in a payload helper) silently overrides it and can make a
  correct fix look broken or a broken one look correct.

### 5b. Keep tests deterministic

A flaky test is a defect in the change that introduced it, not background noise.

- Do not assert outcomes that depend on sub-second wall-clock races, such as a 40 ms
  acknowledgement window or a 50 ms subprocess timeout. Patch the timing constant when
  the semantics, not the latency, are under test.
- When a hook or another process acknowledges before persisting, poll with a bounded
  deadline instead of reading once.
- Keep synthetic timestamps in the past. A timestamp later than the current clock is
  treated as clock rollback.
- Give subprocess fixtures enough time to start before a timeout fires, then bound the
  total duration separately. Interpreter startup alone can exceed a second while the
  suite runs other processes.
- Use generous timeouts on success paths (for example 15 s). They return as soon as
  the work finishes, so a large limit costs nothing when the machine is fast and
  prevents failures when it is loaded. Keep short timeouts only where the timeout
  itself is under test.
- Run new or timing-sensitive tests repeatedly (at least 10 times) and run the full suite
  at least twice before pushing. Check whether a failure also occurs on the base commit.

### 6. Make the review scope match the claim

A targeted review supports only a targeted claim. Before claiming that a change or PR
is ready, reviewers must inspect the full current diff, including all uncommitted fixes,
against the actual merge base. A verdict on an older commit or smaller diff does not
cover later overlapping changes.

For substantial or high-risk changes, run independent correctness, security, testing,
reliability, API-contract, and project-standards reviews after implementation. Include
an adversarial review for code involving concurrency, lifecycle state, persistence,
untrusted wire data, or process control. Reviewers must inspect the current filesystem
and cite concrete code or runtime evidence. Treat their advice as hypotheses until it
is reproduced or otherwise verified.

After addressing findings, ask the relevant reviewers to re-review the updated code.
Any new actionable finding invalidates the affected clean verdict. Rerun the relevant
reviewers on the updated full diff rather than reviewing only the latest patch.

### 7. Verify the final state

Before committing or pushing:

- run focused tests for changed behavior;
- run the full test suite;
- run the repository lint and formatting checks;
- run `git diff --check`;
- inspect the final diff for unrelated changes and temporary scaffolding;
- confirm every accepted review finding has a test or other reproducible verification;
- confirm the working tree contains only intended changes.

For concurrency, timeouts, ordering, or race fixes, repeat the focused regression enough
times to exercise scheduling variance. A single successful run is insufficient evidence
for a nondeterministic boundary.

If a check has a known pre-existing failure, identify it precisely and prove the current
change did not introduce it. Do not describe a partially skipped check as fully passing.

### 8. Close the review loop

For each external review finding:

1. classify it as accepted, rejected, or already addressed;
2. record the evidence for that decision;
3. implement accepted fixes and add regression coverage;
4. rerun the semantic review and verification gates;
5. perform a fresh full-diff review after all accepted fixes;
6. push only after the local review has no unresolved actionable findings;
7. reply to and resolve addressed review threads;
8. request another external review when the workflow or user requires it.

Read every unresolved thread in full before replying, and write each reply for that
thread alone. Never reply to or resolve threads in bulk with one message: new threads can
arrive between polls, and a generic reply resolves findings nobody read. If a thread was
resolved with the wrong reply, reopen the work, fix it, and post a correction on that
thread.

When a fix strategy keeps drawing new findings of the same kind (for example, parsing
shell syntax to find executed words), stop patching cases. Step back to a design whose
correctness does not depend on enumerating them, and record why in the PR.

### 9. Re-review your own fixes

Fixes are new code and receive the same review as the original change.

- Review the fix diff adversarially before pushing. Ask what the fix could break, not
  only whether it resolves the comment.
- When a fix changes a persisted format, a time base, or a counting rule, re-run the
  boundary matrix for that dimension.
- Never drop a "suspected but not reproduced" finding silently. Fix it, or record why
  it cannot occur.
- When a finding names one instance (one token prefix, one event kind, one field, one
  nesting depth), search for and fix the whole family before replying. Write the family
  down (for example "escaping at depth N") and test across it.
- A finding in a fix you just pushed means the previous re-review missed it. Add the
  missed check to [Known Defect Patterns](#known-defect-patterns) in the same change.

## Known Defect Patterns

These classes recurred across review cycles in this repository. Check each applicable
item before requesting review.

**Text handling and output safety**

- Do not pattern-match free text to find or remove secrets. Scrubbing cannot be complete
  (prose, encodings, nesting, unknown token formats), it mangles useful text, and each
  extension added new defects, including a regex denial of service. Pass text through
  as written, keep the stores it lands in private, and let the producing host decide
  whether text leaves the machine (`AGENT_ROUTER_FLEET_SHARE_TEXT`).
- Make text safe to store and display without changing its words: remove whole terminal
  escape sequences with a character scanner (removing only ESC leaves `[0m` fragments),
  drop control and format characters, turn line breaks into spaces, then bound length.
- Cover every ECMA-48 escape form, not just CSI and OSC: nF (`ESC ( B`, emitted by
  `tput sgr0`), Fp/Fe/Fs (`ESC 7`, `ESC =`), and C0 controls embedded in a CSI body.
  An unterminated control string must end at the next ESC so it cannot swallow later
  text. Treat 8-bit C1 characters as single controls; a stray `U+0090` must never
  consume the rest of a field.
- Validate identifiers structurally (no controls, no surrounding whitespace, bounded
  length). Do not reject them for looking like secrets.
- Validate execution identity independently of usage and cost. A nonempty reported
  model map that omits the selected model must fail even when malformed counters,
  invalid entries, or accounting bounds make usage unavailable.
- A value that fails a safety check should degrade only that value. For example, an
  unusable transcript path must not reject the whole lifecycle event.

**Ordering and terminal state**

- Every terminal kind (`stop` and `failed`) needs the same handling for delayed,
  same-timestamp, result-watermark, and session-end cases.
- Watermarks must advance on metadata-only paths as well as state-changing paths.
- Same-timestamp ties must not reopen a terminal or ended state.
- Derive lifecycle status from retained entity evidence on delayed, tied, and replayed
  paths. Use each entity's own watermark for terminal precedence and each reset's
  watermark for monotonic clearing; an unrelated latest event is not an ordering fence.
- Replaying a reset must preserve newer evidence, including a same-timestamp waiter
  delivered after the original reset. Test both completion orders with live siblings.
- When a history map is bounded (for example the last 128 events), ordering checks
  must use every retained timestamp for that entity, not only the bounded map.
  Otherwise eviction lets a delayed event clear state that is still live.
- When eviction makes ordering unknowable, mark the record uncertain and report its
  totals as unknown until the next reset. Do not add a floor that rejects older
  evidence: it silently hides real state (a waiting agent) and blocks legitimate
  updates for entries that are still retained.

**Clocks and freshness**

- Never compare one host's wall clock with another's. Advance the source clock by
  monotonic elapsed time.
- Choose the conservative boundary: measure from request start when aging a sample,
  and from receipt when expiring a reservation.
- Treat a timestamp later than the current clock as uncertainty in every observer, and
  report the exact total as unavailable.
- Restart uncertainty must reach every derived classification, not only the status
  label. A record reclassified `unknown` after a restart must keep capacity `unknown`
  until evidence arrives or the window in which it would have counted as active
  expires. A warning shown next to `available` is a contradiction, not a caveat.
- Include already-unknown lifecycle records in restart admission tests. Preserve an
  existing uncertainty deadline across repeated restarts and delayed or tied waiter
  changes; derive new deadlines from retained evidence, never from restart time.
- Resolve each uncertainty cause when its own accepted evidence arrives. Check delayed
  root evidence after missing-root warnings and fresh child evidence after restart;
  stale, tied, replayed, or rejected evidence must not retire a restart fence, and a
  resolved cause must not clear unrelated uncertainty or fence a later unknown state.
- Use one threshold for a classification and for every alert or `partial` flag derived
  from it.
- A cached state derived from another entity must expire with that entity's evidence,
  not a newer unrelated event. Check silent expiry without another hook, replay
  equivalence, and the same read-time predicate in rendering and complete totals,
  including empty and truncated display pages.
- Fresh missing-root uncertainty must invalidate exact active totals and capacity,
  then expire at the same freshness boundary. During clock rollback, retain future
  live evidence in durable status; check the state again after the clock catches up
  without delivering another event.

**Ownership of nested data**

- A viewer must not write viewer-derived values into a producer's nested record. Put
  them on the viewer's wrapper. Mixing clocks or sources inside one record breaks
  that record's own invariants (for example, alerts later than its `generated_at`).

**Counting and truncation**

- Compute an active total as one predicate. Disjoint sub-counts drop records that
  satisfy neither half, for example a fresh idle session with a live tool.
- Never derive alerts or totals from a page truncated for display. Sort records that
  need attention first, and mark derived sections partial when the source was truncated.
- One invalid row must not fail a whole collection. Skip it, log it, and report the
  result as partial.
- Health errors must decay. One lost write must not keep snapshots partial until
  restart.

**Persistence and upgrades**

- Validate cumulative usage per model and token bucket before aggregation. Growth
  in one bucket must not hide another bucket's disappearance or decrease; legacy
  aggregate-only baselines cannot establish that continuity. Bound the cumulative
  sums in current and persisted maps as well as each contributor and attempt delta.
  Revalidate retained flat accounting frames and final public totals on read; a
  collector guard alone cannot protect completed records written by older versions.
- Version any change to a persisted digest or format. Verify legacy values once, then
  upgrade them, and test the upgrade from the previously pushed format. This applies
  even before merge, because a pushed branch may already be running.

**Files read while they change**

- A `stat` taken after reading can describe newer content than the bytes read. Cache the
  metadata observed before verification and reading. If size or mtime changed across
  the read, report the result as incomplete rather than caching it as complete.

**Native interfaces**

- When enabling a platform, trace installation, reconfiguration, and uninstall together;
  platform-specific service management must not leak into manual-daemon paths.
- Bind native version inspection and execution to the same resolved executable for
  each new attempt. Recovery consumes its persisted binding; changed configuration
  or a worktree PATH must not select an uninspected executable.
- Validate every native inventory diagnostic channel and compare required entries
  before classifying trust. Missing entries are incomplete evidence; warnings must
  retain their cause without invalidating complete, unrelated evidence.
- Size every ctypes output parameter from the C header, not by assumption. For example,
  `vm_size_t` is pointer-sized (`c_size_t`), not `c_uint32`. A mismatch writes past the
  buffer silently. Verify FFI calls against the operating system's own tool output
  (such as `sysctl`).
- Verify the meaning of each field, not only its width. macOS `free_count` already
  includes speculative pages (`vm_stat` subtracts them), so adding `speculative_count`
  double-counted available memory. Reproduce the OS tool's number before deriving one.

**Concurrency and processes**

- Exercise SQLite lock contention through the daemon wire protocol for both new
  subscriptions and acknowledgments. Classify busy/locked errors explicitly, preserve
  useful diagnostics, and keep delivery retries within the caller's overall deadline.
  Cover empty and partial replies, and a healthy poll followed by a deadline-truncated poll.
- Never `clear()` and then refill a shared object that another thread may read. Publish
  the new state in one swap.
- A process-group leader exiting does not prove the group exited. After a failure,
  check the group and SIGKILL any members that remain.
- A disappeared process can raise `ProcessLookupError` when signaled or
  `FileNotFoundError` when reading Linux `/proc`. Cleanup must handle both while
  preserving ownership checks and surfacing permission or malformed-state errors.
- Publish fixture PID payloads atomically, after signal handlers and foreground
  groups are initialized. Output/session files are not readiness. Register cleanup
  before publication, PID parsing, identity probes and group assertions; cleanup
  must also account for foreground groups outside the launcher group.
- Keep failure state independent of diagnostic text. An exception or sanitized stderr
  can have an empty message; it must still trigger failed delivery and owned-process
  cleanup, with a nonempty diagnostic fallback.

**Parsing and cost**

- Avoid regular expressions on untrusted free text. Where one is unavoidable, bound
  every quantifier and time it on 1 MB of its worst-case character; an unbounded
  `\\*` before an optional quote once made matching quadratic (13 s for 20k
  backslashes).
- Parsers of untrusted JSON must catch `RecursionError` alongside `ValueError`.
- Verification on hot paths (for example, per-status transcript refresh) must
  short-circuit when inputs are unchanged. Measure the cost on large inputs.

## Completion Standard

Do not say a change is complete, safe, clean, or ready merely because tests pass.
A completion claim requires all of the following:

- the requested behavior is implemented without expanding scope;
- relevant contracts have been traced end to end;
- applicable adversarial boundaries have been exercised;
- focused and full verification pass, with exceptions stated accurately;
- the required independent reviews have no unresolved actionable findings on the
  current full diff;
- the final diff has been inspected and contains no unrelated work.

Do not use a local commit as evidence of readiness. If a high-risk change must be saved
before these gates pass, label it explicitly as a checkpoint rather than a completed
implementation.

Do not say `clean`, `ready`, or `code complete` while a specification-mandated live or
integration check remains pending. Use precise language such as: “Automated checks pass;
the two-host live verification is still pending.” When any evidence is incomplete, state
exactly what remains unverified.
