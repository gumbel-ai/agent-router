# Receiving a remote child snapshot

`router.remote_receiver.RemoteReceiver` admits a selected snapshot into an existing
Router database and scheduler. This internal receiving-side API is a prerequisite
for remote children; it is not a remote `router start` command yet.

The caller must already hold the receiving machine's existing privileged local or
SSH account. Origin identifiers bind ownership and replay; they do not authenticate
a caller. The future origin transport must validate the native parent's authority
before forwarding a request. Receiving-side authentication, permissions, setup and
model aliases come from the receiving operator's configuration.

## Admission and replay

A schema-version 1 start identifies the receiving host, origin host, native origin
session and request ID. It supplies an absolute configured repository, executor,
model alias, task and the exact [selected snapshot receipt](remote-snapshots.md).
Optional context, work-item and parent-agent identifiers must be supplied explicitly.
Requests are bounded to 128 KiB and task text to 64 KiB.
The optional `result_paths` list declares exact additional output paths before
launch. Together with the snapshot's selected paths it may name at most 128 files.
Changing this selection under an existing request ID fails like any other payload
change; it never triggers an automatic replacement command.

The receiver validates configuration and imports the verified bundle without holding
a database write transaction. It then atomically binds the origin host, origin
session and request ID to one task. Concurrent duplicates return that same task;
changing the payload under the same identity fails. A maintenance fence is checked
again after import, before admission.

`lookup()` reads this binding after a lost response or daemon restart. A missing
binding does not prove a concurrent start cannot still complete. Never use a missing
or disconnected response to launch a replacement command or fall back to local
execution.

The existing scheduler prepares the worktree from the verified imported commit,
including its exact tree and parentless snapshot identity. It does not capture the
receiving checkout as the task's initial content. Native session selection remains
the scheduler's responsibility, and replay does not create another attempt.
Preparation materializes the verified blobs directly in a worktree without a Git
checkout. Git clean/smudge filters, text and line-ending conversion, identifier
expansion, working-tree encoding and hooks do not transform those bytes.

Final capture reads raw regular-file bytes from the initial selection and declared
additional result paths, with the snapshot's file and byte bounds. It preserves
deletions, binary contents and executable modes. Ignored files are excluded from
discovery and refused if explicitly selected. An undeclared nonignored file makes
capture fail with the worktree retained; Router does not silently return a complete
patch that omits visible output. Include necessary output paths before starting.
The default result selection is only the original snapshot files.
Binary patches have a separate 16 MiB bound for original and final content plus
encoding and path metadata; snapshot bundles retain their 6 MiB bound.
Result discovery and admission honor the receiving repository's effective Git
excludes file, including global and explicit configuration-environment settings.

## Ownership and correlation

An internal hashed request scope separates remote requests from native local
sessions. This scope is never exported as a native parent session. Ordinary local
parent authorization and mutation routes refuse these remote tasks; remote controls
must validate the original ownership binding through a dedicated route.

Status exposes only validated `remote_origin` identifiers. Legacy fleet and
correlation parent fields remain unknown for these tasks because those schemas do
not yet express a parent on another host. A child native session may still be known.
Time proximity and a matching label never establish parentage.

## Verification scope

Synthetic tests cover concurrent admission, changed-payload refusal, wrong-host
refusal, maintenance during import, durable lookup, native-control rejection and
the existing scheduler executing imported file contents. They use separate
repositories and state directories with a fixture child.

**NOT CHECKED:** physical SSH, native provider execution, independent reboot, remote
review/accept/discard, result transport and durable application on the origin host.
Cross-host parent relations are not exported yet. This API does not complete #54 or
the remote-child correlation acceptance checks in #48. The eventual remote-command
PR must provide an exact-commit checklist for an approved-access machine.
