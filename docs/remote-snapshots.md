# Selected snapshots for remote children

The `router.remote_snapshot` helpers prepare and verify a bounded bundle of
explicitly selected code files. The remote-child control path carries the existing
session/request identity with this receipt. A remote Router imports and materializes
the immutable ref before the scheduler can launch a child; repeated `remote_start`
with the same identity and receipt returns the same task, while a changed replay is
refused. Remote status, review, cancel and close are explicit SSH requests. A
transport failure leaves the parent outcome unknown and never starts locally.

The caller must supply 1–128 exact, unique repository-relative file paths. No
directory, glob, label or filename implies permission. Git-visible content is not
necessarily safe to transfer: the operator must approve the selected code contents.
Ignored paths are refused, including tracked files that now match ignore rules.
Admission honors Git's effective `core.excludesFile`, including global and explicit
configuration-environment overrides. Only that path is carried into the isolated
Git operations; unrelated user/system settings remain disabled. Git selects its
normal XDG ignore-file default when no explicit path is configured.
Missing/deleted files must be absent from the selection. Indexed symlinks,
submodules, unresolved merges and sparse checkouts are unsupported and refused.
Repository admission streams index modes with fixed-size parser state, so unrelated
tracked-file counts and path lengths do not consume the selected snapshot's budget.

Selected files are read as raw bytes with bounded, no-follow reads and change
detection. Git clean filters, hooks and fsmonitor do not run. A temporary index
captures current staged, unstaged and nonignored untracked file contents without
changing the parent's real index. The snapshot contains a parentless commit, so
its bundle carries the selected tree only, with no project history.
All Git subprocesses strip inherited `LANGFUSE_*` environment variables.

## Identity and limits

The snapshot uses `refs/router/snap/TASK_ID`. A task ID binds one immutable commit;
a repeat with the same contents can return that snapshot, while changed contents
under the same task ID fail. No branch is pushed or modified. The commit author
and timestamp are fixed so the same task and tree have stable commit identity.

The receipt contains version 1, task/commit/tree IDs, object format, sorted selected
paths, file/byte counts and a SHA-256 digest of the bundle. The digest detects
changed bytes; SSH authentication and the remote Router's durable request binding
provide the transport boundary.

| Bound | Limit |
| --- | --- |
| Selected files | 128 |
| File size | 1 MiB |
| Total selected bytes | 4 MiB |
| Bundle size | 6 MiB |
| Path depth / encoded length | 16 components / 512 bytes |
| Git output collection | 30 seconds |
| Owned Git group cleanup | At most one additional second in the caller |
| Recursive tree listing | At most 128 × 16 entries, with bounded path and Git metadata fields |

The transport format is a canonical v2 SHA-1 or v3 SHA-256 Git bundle containing
full objects. Export disables delta encoding and fixes full-object compression. Import refuses deltas and
thin packs and checks pack count, checksum and declared/actual streamed expansion
before Git imports any object. Metadata and expanded objects have separate bounds;
a small compressed bundle cannot bypass the file or aggregate limits.
The recursive listing has a separate bound because it repeats full paths for every
ancestor directory. This does not increase the packed-object or expansion bounds.
If SIGKILL cannot be followed by confirmed child termination within the cleanup
budget, the operation reports termination as unconfirmed and retains a background
reaper. It never blocks the caller indefinitely or reports that state as success.

## Import and failure behavior

`export_snapshot(root, task_id, directory, paths)` writes a private
`snapshot.bundle` and returns its receipt. `import_snapshot(root, receipt,
bundle_path)` verifies it in an isolated quarantine repository, then imports only
the validated objects and binds the immutable task ref in the receiving repository.
Both repositories must use the same Git object format.

Import verifies the exact parentless commit, tree, selected regular-file modes,
paths, byte counts and object closure. Extra objects, symlinks, unsafe paths,
changed bundles, malformed receipts and conflicting refs fail explicitly. Git
transport is restricted to local files even if repository URL-rewrite settings
try to select another protocol. Automatic maintenance and credential prompts are
disabled. Validation does not check out files or integrate anything.

Failed export may leave unreachable Git objects or an immutable snapshot ref;
failed import may leave validated objects before a conflicting ref is refused.
An uncertain Git timeout or cleanup remains an error even if its ref was persisted;
only a confirmed Git status failure can recover from a matching concurrent ref.
Neither outcome authorizes a command launch. Retry only the same immutable transfer
identity when appropriate; never use a transfer error to rerun a command or fall
back to local execution. The parent applies a returned diff only through its
existing `git apply --check`/`git apply` path.

## Verification scope

Automated tests use independent repositories with synthetic dirty/binary files,
concurrent duplicate exports, hostile bundles and synthetic URL-rewrite/alternate-ref helpers.
They cover unchanged parent index, selected-only/no-history transfer, immutable
refs, compressed expansion bounds, refused external transports and tracing-environment
isolation for an allowed Git helper.

**NOT CHECKED:** physical SSH, native child execution, independent reboot and the
complete start/review/accept/discard/integration lifecycle. Synthetic tests cover
wire validation and isolated materialization only. A live run must use the exact
commit on both hosts and record the remote host ID, request binding, reboot boundary,
and final diff digest before claiming remote-child completion.
