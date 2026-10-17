# Agent Instructions

All implementation and review work in this repository must follow
[CODE_REVIEW_PROCESS.md](CODE_REVIEW_PROCESS.md).

In particular, do not declare substantial or high-risk work complete based only on a
passing test suite. Trace changed contracts end to end, exercise the applicable
adversarial boundary matrix, and audit the whole violated invariant rather than patching
review comments one at a time. Independent reviews must cover the full current diff;
later fixes invalidate overlapping earlier verdicts. Complete the final verification
and review-loop gates before committing or pushing.
