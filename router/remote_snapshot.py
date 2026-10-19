"""Explicitly selected, current-file snapshots; no history, execution or integration.

The caller owns the code-only allowlist: Git-visible does not mean secret-free.
Selected regular files are copied as raw bytes, without clean filters. Bundles contain
one parentless commit and its selected tree. Receipts must accompany transport bytes.
"""

import hashlib
import os
import selectors
import signal
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
import zlib
from pathlib import Path

MAX_FILES = 128
MAX_DEPTH = 16
MAX_PATH_BYTES = 512
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_BUNDLE_BYTES = 6 * 1024 * 1024
# Binary deltas carry both trees plus encoding and path metadata.
MAX_PATCH_BYTES = 4 * MAX_TOTAL_BYTES
MAX_METADATA_BYTES = 512 * 1024
MAX_COMMIT_BYTES = 1024
MAX_PACK_OBJECTS = MAX_FILES * (MAX_DEPTH + 1) + 2
MAX_EXPANDED_BYTES = MAX_TOTAL_BYTES + MAX_METADATA_BYTES + MAX_COMMIT_BYTES
# ls-tree -t emits every ancestor and file, unlike packed tree-object bytes.
MAX_TREE_LIST_BYTES = MAX_FILES * MAX_DEPTH * (MAX_PATH_BYTES + 6 + 1 + 4 + 1 + 64 + 1 + 20 + 1 + 1)
GIT_TIMEOUT = 30
GIT_CLEANUP_TIMEOUT = 1
_IDENTITY = "Agent Router <router@localhost> 946684800 +0000"


class _GitStatusError(ValueError):
    """Git exited with a status the operation did not accept."""


def _git(root, *arguments, data=None, environment=None, statuses=(0,), limit=MAX_METADATA_BYTES,
         read_configuration=False, stdout_consumer=None):
    """Bound captured output and process-group lifetime; optionally stream stdout."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "LANGFUSE_"))}
    if read_configuration:
        if arguments != ("config", "--null", "--path", "--get", "core.excludesFile"):
            raise ValueError("Normal Git configuration is allowed only for the excludes-file query")
        env.update((key, value) for key, value in os.environ.items() if key.startswith("GIT_CONFIG"))
    else:
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file")
    env.update(environment or {})
    command = ["git", "--no-replace-objects", "-c", f"core.hooksPath={os.devnull}",
               "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false",
               "-c", "gc.auto=0", "-c", "gc.autoPackLimit=0", "-C", str(root), *arguments]
    # Inputs are bounded blobs or metadata. A temporary regular file avoids a pipe
    # deadlock while output is drained and never exposes content in arguments.
    with tempfile.TemporaryFile() as source:
        if data is not None:
            source.write(data)
            source.seek(0)
        process = subprocess.Popen(command, env=env, stdin=source, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        output, diagnostic = bytearray(), bytearray()
        deadline = time.monotonic() + GIT_TIMEOUT
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, output)
                selector.register(process.stderr, selectors.EVENT_READ, diagnostic)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("Snapshot Git operation exceeded its deadline")
                    for key, _events in selector.select(remaining):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif key.data is output and stdout_consumer is not None:
                            stdout_consumer(chunk)
                        else:
                            key.data.extend(chunk)
                            if len(key.data) > (limit if key.data is output else 65536):
                                raise ValueError("Snapshot Git output exceeds its bound")
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if process.returncode not in statuses:
                # Git diagnostics can quote caller-selected paths or bundle text.
                # Keep them on this local exception boundary; receipts carry none.
                message = diagnostic.decode(errors="replace").strip()
                raise _GitStatusError(f"Snapshot Git {arguments[0]} failed: {message or process.returncode}")
            return bytes(output)
        except subprocess.TimeoutExpired as error:
            raise ValueError("Snapshot Git operation exceeded its deadline") from error
        finally:
            for stream in (process.stdout, process.stderr):
                stream.close()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                try:
                    process.wait(timeout=GIT_CLEANUP_TIMEOUT)
                except subprocess.TimeoutExpired as error:
                    # Retain ownership without blocking the caller on an uninterruptible child.
                    threading.Thread(target=process.wait, name="router-snapshot-reaper", daemon=True).start()
                    raise ValueError("Snapshot Git termination is unconfirmed after SIGKILL; background cleanup continues") from error


def _git_with_excludes(root, *arguments, **options):
    """Retain only the normal effective excludes-file setting, not other config."""
    raw = _git(root, "config", "--null", "--path", "--get", "core.excludesFile",
               statuses=(0, 1), read_configuration=True)
    if not raw:
        return _git(root, *arguments, **options)  # Git's native XDG default still applies.
    if not raw.endswith(b"\0") or b"\0" in raw[:-1]:
        raise ValueError("Git excludes-file configuration returned an invalid path")
    return _git(root, "-c", "core.excludesFile=" + os.fsdecode(raw[:-1]), *arguments, **options)


def _path(value):
    if not isinstance(value, str) or not value or len(value.encode()) > MAX_PATH_BYTES:
        raise ValueError("Snapshot paths must be bounded nonempty relative file paths")
    pieces = value.split("/")
    if (len(pieces) > MAX_DEPTH or any(part in {"", ".", ".."} or part.casefold() == ".git"
                                    or part.endswith((".", " ")) for part in pieces)
            or any(character in "\\:" or unicodedata.category(character).startswith("C") for character in value)):
        raise ValueError("Snapshot path is unsafe or unsupported")
    return value


def _paths(values):
    if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= MAX_FILES:
        raise ValueError(f"Snapshot requires 1 to {MAX_FILES} explicitly selected file paths")
    result = sorted(_path(value) for value in values)
    if len(set(result)) != len(result):
        raise ValueError("Snapshot paths must be unique")
    return result


def _task(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in value)):
        raise ValueError("Snapshot task_id must contain 1 to 128 letters, digits, underscores or hyphens")
    return value


def _oid(value, length):
    return (isinstance(value, str) and len(value) == length
            and all(character in "0123456789abcdef" for character in value) and set(value) != {"0"})


def _repository(root, require_head=True):
    root = Path(root).resolve(strict=True)
    actual = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    if actual != root:
        raise ValueError("Snapshot repository must name the Git worktree root")
    if require_head:
        _git(root, "rev-parse", "--verify", "HEAD")
    if _git(root, "ls-files", "-u", "-z"):
        raise ValueError("Snapshot transfer refuses unresolved merges")
    if _git(root, "config", "--bool", "--get", "core.sparseCheckout", statuses=(0, 1)).strip() == b"true":
        raise ValueError("Snapshot transfer refuses sparse checkouts")
    prefix, invalid_record, invalid_mode = bytearray(), False, False

    def inspect_modes(chunk):
        nonlocal invalid_record, invalid_mode
        for value in chunk:
            if value == 0:
                if len(prefix) != 7:
                    invalid_record = True
                prefix.clear()
            elif len(prefix) < 7:
                prefix.append(value)
                if len(prefix) == 7 and prefix not in (b"100644 ", b"100755 "):
                    invalid_mode = True

    # Finish bounded Git cleanup before reporting a mode refusal; transport failures still propagate.
    _git(root, "ls-files", "--stage", "-z", stdout_consumer=inspect_modes)
    if prefix or invalid_record:
        raise ValueError("Git index returned malformed or unterminated mode metadata")
    if invalid_mode:
        raise ValueError("Snapshot transfer requires indexed regular files; submodules and symlinks are refused")
    return root


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_file(root, path, limit):
    descriptors = []
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        components = path.split("/")
        for component in components[:-1]:
            descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(descriptor)
        parent = descriptor
        descriptor = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("Snapshot file must be regular and within its byte bound")
        content = bytearray()
        while len(content) <= limit:
            part = os.read(descriptor, min(65536, limit + 1 - len(content)))
            if not part:
                break
            content.extend(part)
        after = os.fstat(descriptor)
        current = os.stat(components[-1], dir_fd=parent, follow_symlinks=False)
        if len(content) > limit or len(content) != before.st_size or _signature(before) != _signature(after) or _signature(after) != _signature(current):
            raise ValueError("Snapshot file changed while being read or exceeds its byte bound")
        return bytes(content), before.st_mode
    except OSError as error:
        raise ValueError(f"Snapshot file is unavailable or unsafe: {error.strerror}") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _bind(root, task_id, commit):
    reference = f"refs/router/snap/{task_id}"
    _git(root, "check-ref-format", reference)
    if _git(root, "symbolic-ref", "--quiet", reference, statuses=(0, 1)).strip():
        raise ValueError("Snapshot task is already bound through an unsupported symbolic ref")
    current = _git(root, "rev-parse", "--verify", "--quiet", reference, statuses=(0, 1)).strip()
    if current:
        if current.decode() != commit:
            raise ValueError("Snapshot task is already bound to another snapshot")
        return
    try:
        _git(root, "update-ref", "--no-deref", reference, commit, "0" * len(commit))
    except _GitStatusError:
        if _git(root, "rev-parse", "--verify", "--quiet", reference, statuses=(0, 1)).strip() != commit.encode():
            raise ValueError("Snapshot task is already bound or its ref could not be persisted") from None


def _commit_bytes(tree, task_id):
    return f"tree {tree}\nauthor {_IDENTITY}\ncommitter {_IDENTITY}\n\nAgent Router remote snapshot {task_id}\n".encode()


def _bundle_header(task_id, commit, object_format):
    header = b"# v2 git bundle\n" if object_format == "sha1" else b"# v3 git bundle\n@object-format=sha256\n"
    return header + f"{commit} refs/router/snap/{task_id}\n\n".encode()


def _verify_pack(content, receipt):
    """Bound packed-object expansion before Git sees any transport-supplied object.

    Only full commit/tree/blob objects are supported. Delta chains and thin packs
    require resolving bases before their expansion is known, so are refused.
    """
    header = _bundle_header(receipt["task_id"], receipt["snapshot_commit"], receipt["object_format"])
    if not content.startswith(header):
        raise ValueError("Snapshot bundle header does not match its sole bound snapshot")
    packed = memoryview(content)[len(header):]
    digest_length = len(receipt["snapshot_commit"]) // 2
    end = len(packed) - digest_length
    if end < 12 or packed[:4] != b"PACK":
        raise ValueError("Snapshot bundle has an invalid pack header")
    version, count = struct.unpack(">II", packed[4:12])
    if version not in (2, 3) or not 3 <= count <= MAX_PACK_OBJECTS:
        raise ValueError("Snapshot pack version or object count exceeds its bound")
    if hashlib.new(receipt["object_format"], packed[:end]).digest() != packed[end:]:
        raise ValueError("Snapshot pack checksum does not match")
    position, total = 12, 0
    for _number in range(count):
        if position >= end:
            raise ValueError("Snapshot pack object header is truncated")
        value = packed[position]
        position += 1
        kind, size, shift = (value >> 4) & 7, value & 15, 4
        while value & 128:
            if position >= end or shift >= 64:
                raise ValueError("Snapshot pack object size is malformed")
            value = packed[position]
            position += 1
            size |= (value & 127) << shift
            shift += 7
        limit = {1: MAX_COMMIT_BYTES, 2: MAX_METADATA_BYTES, 3: MAX_FILE_BYTES}.get(kind)
        if limit is None or size > limit or total + size > MAX_EXPANDED_BYTES:
            raise ValueError("Snapshot pack object type or declared expansion exceeds its bound")
        total += size
        decoder, expanded = zlib.decompressobj(), 0
        try:
            while not decoder.eof:
                if position >= end:
                    raise ValueError("Snapshot pack compressed object is truncated")
                chunk = packed[position:min(position + 65536, end)]
                output = decoder.decompress(chunk, min(65536, size - expanded + 1))
                expanded += len(output)
                if expanded > size:
                    raise ValueError("Snapshot pack actual expansion exceeds its declared size")
                remainder = decoder.unused_data if decoder.eof else decoder.unconsumed_tail
                consumed = len(chunk) - len(remainder)
                position += consumed
                if not consumed and not output:
                    raise ValueError("Snapshot pack compressed object made no progress")
        except zlib.error as error:
            raise ValueError("Snapshot pack contains invalid compressed data") from error
        if expanded != size:
            raise ValueError("Snapshot pack expansion does not match its declared size")
    if position != end:
        raise ValueError("Snapshot pack contains trailing or undeclared objects")


def export_snapshot(root: Path, task_id: str, directory: Path, paths: list | tuple) -> dict:
    """Write private ``directory/snapshot.bundle`` and return its bounded receipt.

    Only selected existing regular files are included. Missing/deleted paths must be
    removed from the explicit selection. No filename or content implies permission.
    """
    task_id, selected = _task(task_id), _paths(paths)
    root = _repository(root)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError("Snapshot artifact directory must be private, owned and not a symlink")
    ignored = _git_with_excludes(root, "check-ignore", "--no-index", "-z", "--stdin",
                                 data=b"\0".join(value.encode() for value in selected) + b"\0", statuses=(0, 1))
    if ignored:
        raise ValueError("Snapshot selection contains an ignored path, including tracked ignored paths")
    total, records = 0, []
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=directory) as temporary:
        environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
        _git(root, "read-tree", "--empty", environment=environment)
        for path in selected:
            content, mode = _read_file(root, path, MAX_FILE_BYTES)
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("Snapshot selected files exceed the total byte bound")
            blob = _git(root, "hash-object", "-w", "--stdin", data=content).strip()
            file_mode = b"100755" if mode & stat.S_IXUSR else b"100644"
            records.append(file_mode + b" " + blob + b"\t" + path.encode() + b"\0")
        _git(root, "update-index", "-z", "--index-info", data=b"".join(records), environment=environment)
        tree = _git(root, "write-tree", environment=environment).decode().strip()
        commit = _git(root, "hash-object", "-t", "commit", "-w", "--stdin", data=_commit_bytes(tree, task_id)).decode().strip()
        _bind(root, task_id, commit)
        object_format = "sha1" if len(commit) == 40 else "sha256"
        packed = _git(root, "pack-objects", "--stdout", "--revs", "--no-reuse-delta", "--no-reuse-object",
                      "--compression=6", "--threads=1", "--window=0", "--depth=0",
                      data=(commit + "\n").encode(), limit=MAX_BUNDLE_BYTES)
        content = _bundle_header(task_id, commit, object_format) + packed
        if len(content) > MAX_BUNDLE_BYTES:
            raise ValueError("Snapshot bundle exceeds its byte bound")
        candidate = Path(temporary) / "snapshot.bundle"
        with candidate.open("xb") as stream:
            os.chmod(candidate, 0o600)
            stream.write(content)
        destination = directory / "snapshot.bundle"
        try:
            os.link(candidate, destination)
        except FileExistsError:
            existing, _mode = _read_file(directory, destination.name, MAX_BUNDLE_BYTES)
            if existing != content:
                raise ValueError("Snapshot artifact directory already contains a different bundle") from None
    return {"schema_version": 1, "task_id": task_id, "snapshot_commit": commit, "snapshot_tree": tree,
            "object_format": "sha1" if len(commit) == 40 else "sha256", "paths": selected,
            "file_count": len(selected), "total_bytes": total, "byte_count": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


def _receipt(receipt):
    fields = {"schema_version", "task_id", "snapshot_commit", "snapshot_tree", "object_format", "paths",
              "file_count", "total_bytes", "byte_count", "sha256"}
    if not isinstance(receipt, dict) or set(receipt) != fields or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        raise ValueError("Invalid snapshot receipt fields or version")
    _task(receipt["task_id"])
    selected = _paths(receipt["paths"])
    length = {"sha1": 40, "sha256": 64}.get(receipt["object_format"]) if isinstance(receipt["object_format"], str) else None
    if length is None or any(not _oid(receipt[field], length) for field in ("snapshot_commit", "snapshot_tree")) or not _oid(receipt["sha256"], 64):
        raise ValueError("Invalid snapshot receipt object or digest identity")
    for field, minimum, maximum in (("file_count", len(selected), len(selected)), ("total_bytes", 0, MAX_TOTAL_BYTES), ("byte_count", 1, MAX_BUNDLE_BYTES)):
        if type(receipt[field]) is not int or not minimum <= receipt[field] <= maximum:
            raise ValueError(f"Snapshot receipt {field} is outside its bound")
    if selected != receipt["paths"]:
        raise ValueError("Snapshot receipt paths must be sorted")


def import_snapshot(root: Path, receipt: dict, bundle_path: Path) -> dict:
    """Verify a bounded bundle in quarantine before importing its immutable ref.

    This imports objects only. It never checks out files, creates branches, starts a
    command or applies a result. The receiving repository must use the same object format.
    """
    _receipt(receipt)
    root = _repository(root, require_head=False)
    if _git(root, "rev-parse", "--show-object-format").decode().strip() != receipt["object_format"]:
        raise ValueError("Snapshot and receiving repository use different object formats")
    bundle_path = Path(bundle_path)
    content, _mode = _read_file(bundle_path.parent.resolve(strict=True), bundle_path.name, MAX_BUNDLE_BYTES)
    if len(content) != receipt["byte_count"] or hashlib.sha256(content).hexdigest() != receipt["sha256"]:
        raise ValueError("Snapshot bundle length or digest does not match its receipt")
    _verify_pack(content, receipt)
    with tempfile.TemporaryDirectory(prefix="router-snapshot-import-") as temporary:
        quarantine = Path(temporary)
        _git(quarantine, "init", "--bare", "--template=", "-q", f"--object-format={receipt['object_format']}")
        bundle = quarantine / "snapshot.bundle"
        with bundle.open("xb") as stream:
            os.chmod(bundle, 0o600)
            stream.write(content)
        reference = f"refs/router/snap/{receipt['task_id']}"
        expected_head = f"{receipt['snapshot_commit']} {reference}\n".encode()
        if _git(quarantine, "bundle", "list-heads", str(bundle)) != expected_head:
            raise ValueError("Snapshot bundle must advertise only its bound task snapshot")
        _git(quarantine, "bundle", "verify", str(bundle))
        _git(quarantine, "-c", "protocol.file.allow=always", "fetch", "--no-auto-maintenance", "--no-tags", "--no-write-fetch-head",
             "--no-recurse-submodules", str(bundle), f"{reference}:{reference}")
        commit, tree = receipt["snapshot_commit"], receipt["snapshot_tree"]
        if _git(quarantine, "cat-file", "commit", commit) != _commit_bytes(tree, receipt["task_id"]):
            raise ValueError("Snapshot commit must be the exact parentless Router snapshot")
        total, files = 0, []
        for entry in _git(quarantine, "ls-tree", "-rztl", "--full-tree", commit, limit=MAX_TREE_LIST_BYTES).split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, object_id, size = metadata.split()
            path = _path(raw_path.decode("utf-8"))
            if mode == b"040000" and kind == b"tree":
                if not any(selected.startswith(path + "/") for selected in receipt["paths"]):
                    raise ValueError("Snapshot contains an unselected directory")
                continue
            if mode not in (b"100644", b"100755") or kind != b"blob" or not size.isdigit() or int(size) > MAX_FILE_BYTES:
                raise ValueError("Snapshot contains unsupported objects or oversized files")
            total += int(size)
            if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                raise ValueError("Snapshot tree exceeds its aggregate bound")
            _git(quarantine, "cat-file", "blob", object_id.decode(), limit=MAX_FILE_BYTES)
            files.append(path)
        if sorted(files) != receipt["paths"] or total != receipt["total_bytes"]:
            raise ValueError("Snapshot tree does not match selected files and byte counts")
        reachable = set(_git(quarantine, "rev-list", "--objects", "--no-object-names", commit).splitlines())
        present = set(_git(quarantine, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").splitlines())
        if present != reachable:
            raise ValueError("Snapshot bundle contains objects outside its selected root tree")
        _git(root, "-c", "protocol.file.allow=always", "fetch", "--no-auto-maintenance", "--no-tags", "--no-write-fetch-head",
             "--no-recurse-submodules", str(quarantine), commit)
        _bind(root, receipt["task_id"], commit)
    return {field: receipt[field] for field in ("task_id", "snapshot_commit", "snapshot_tree")}
