# Waiting for Router tasks

`router wait-tasks` blocks until started Router tasks need the coordinator, in a single
command. It replaces coordinator-written polling loops of `router status`.

```sh
router wait-tasks TASK_ID [TASK_ID ...] (--any | --all) --timeout SECONDS
```

- `--any` returns as soon as one task is ready; `--all` waits until every task is ready.
- `--timeout` is required: greater than 0, at most 3600 seconds.
- A task is **ready** when it has left the active states (`created`, `validating`,
  `preparing`, `running`, `retrying`, `cancelling`) or has a `cleanup_error`. That covers
  `awaiting_review`, `blocked`, `failed`, `interrupted`, `cancelled`, `accepted`,
  `integrated` and `closed`.
- The command is read-only. It polls `router status TASK_ID` once a second and never
  accepts, reviews, retries, cancels, closes or otherwise changes a task.

## Result

One line of JSON, exit code 0 for all three outcomes:

```json
{"mode":"any","condition_met":true,"timed_out":false,"daemon_unavailable":false,
 "tasks":[{"task_id":"task-a","status":"running","attempts":1,"ready":false},
          {"task_id":"task-b","status":"awaiting_review","attempts":2,"ready":true}]}
```

| Field | Meaning |
|---|---|
| `condition_met` | `--any`/`--all` was satisfied |
| `timed_out` | the deadline passed first; `tasks` holds the latest state of every task |
| `daemon_unavailable` | with `timed_out`: the last poll could not reach Router (stopped or restarting). States are the last ones read, or `unknown` if none was |
| `tasks[].status` | current status, or `missing` for an ID Router does not know (counts as ready) |
| `tasks[].ready` | this task needs the coordinator or has finished |
| `failure_reason`, `cleanup_error` | present only when set |

Every requested task is always listed, so `--any` also shows which tasks are still
running. A daemon restart during the wait is tolerated: polling resumes when Router is
back. Usage errors (bad IDs, missing mode, out-of-range timeout) exit 2.

## Coordinator usage

After `router start` returns task IDs, make **one** blocking call instead of checking
status from model turns:

```sh
router wait-tasks task-a task-b --any --timeout 540
```

On **every** return, including `timed_out`, check each task's `ready` flag and handle the
ready ones first (`router task-diff`, `router review`, `router close`, …): with `--all`
a timeout can still list ready tasks, and during an outage the states are only the last
ones read. Then call `router wait-tasks` again with the tasks still running.

- **Claude Code:** the Bash tool allows up to 10 minutes per command; give the call a
  tool timeout above `--timeout` (for example `--timeout 540` with a 600000 ms tool
  timeout). Alternatively run it in the background and continue when it exits.
- **Codex:** keep `--timeout` below the shell tool's command timeout, or raise that
  timeout for this call.

Each call costs one tool call and one compact result, however long it waits.
