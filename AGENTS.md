# Agent Instructions

All implementation and review work in this repository must follow
[CODE_REVIEW_PROCESS.md](CODE_REVIEW_PROCESS.md).

In particular, do not declare substantial or high-risk work complete based only on a
passing test suite. Trace changed contracts end to end, exercise the applicable
adversarial boundary matrix, and audit the whole violated invariant rather than patching
review comments one at a time. Independent reviews must cover the full current diff;
later fixes invalidate overlapping earlier verdicts. Complete the final verification
and review-loop gates before committing or pushing.

Before requesting any review, check every applicable item in
[Known Defect Patterns](CODE_REVIEW_PROCESS.md#known-defect-patterns). Re-review your own
fixes with the same rigor as the original change (section 9 of the process), and keep
tests proportionate (section 5a) and deterministic (section 5b). A flaky test is a
defect, not noise, and a test that cannot fail without its fix is clutter.

This file is the single source of agent instructions for this repository.
`CLAUDE.md` imports it; update this file, not `CLAUDE.md`.
