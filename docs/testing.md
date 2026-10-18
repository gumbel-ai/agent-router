# Test verification and preparation diagnostics

Run the suite and lint from the repository root:

```sh
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests
uvx ruff@0.17.1 check router hooks scripts tests
```

When a runtime test fails to reach its expected task state, its assertion includes
the captured Git preparation stderr for each preparation attempt. Large files show
their last 64 KiB with an explicit truncation marker. Missing, empty, or unreadable
artifacts are reported alongside the original task-state failure.

A deterministic branch-namespace conflict checks that a real Git error reaches the
assertion. This verifies the diagnostic path; it does not establish the cause of the
load-sensitive macOS failures tracked in issue #16. That cause remains NOT CHECKED.
Existing expected states and timeouts remain in force; diagnostics add no retry.
