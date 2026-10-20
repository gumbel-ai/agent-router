"""Immutable process and installed-source identity captured at daemon startup.

This attests package files at capture time and the paths of already-loaded Router
modules. It cannot reconstruct loaded bytecode after an in-place install, so callers
must serialize installation and daemon startup. Later installation changes cannot
alter the returned record.
"""

import base64
import binascii
import csv
import hashlib
import io
import json
import os
import stat
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path, PurePosixPath

from router.process import creation_identity

DISTRIBUTION = "agent-router"
SCHEME = "python-record-v1"
MAX_DIRECT_URL_BYTES = 64 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024


class _SourceUnavailable(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _SourceUnavailable(
                "direct_url_invalid", f"Duplicate direct_url.json field: {key}"
            )
        result[key] = value
    return result


def _commit(content: bytes | None) -> str | None:
    if content is None:
        return None
    if len(content) > MAX_DIRECT_URL_BYTES:
        raise _SourceUnavailable(
            "direct_url_invalid", "Agent Router direct_url.json exceeds its size limit"
        )
    try:
        direct = json.loads(content, object_pairs_hook=_json_object)
    except _SourceUnavailable:
        raise
    except (TypeError, ValueError, RecursionError) as error:
        raise _SourceUnavailable(
            "direct_url_invalid", "Agent Router direct_url.json is invalid"
        ) from error
    if not isinstance(direct, dict):
        raise _SourceUnavailable(
            "direct_url_invalid", "Agent Router direct_url.json must be an object"
        )
    vcs = direct.get("vcs_info")
    if vcs is None:
        return None
    if not isinstance(vcs, dict) or vcs.get("vcs") != "git":
        raise _SourceUnavailable(
            "direct_url_invalid", "Agent Router VCS provenance must identify git"
        )
    commit = vcs.get("commit_id")
    if (
        not isinstance(commit, str)
        or len(commit) not in {40, 64}
        or any(character not in "0123456789abcdefABCDEF" for character in commit)
    ):
        raise _SourceUnavailable(
            "direct_url_invalid", "Agent Router VCS provenance has no exact commit"
        )
    return commit.lower()


def _record_hash(encoded_hash: str) -> bytes:
    if not encoded_hash.startswith("sha256="):
        raise _SourceUnavailable(
            "record_invalid", "Agent Router RECORD provenance rows require sha256"
        )
    value = encoded_hash.removeprefix("sha256=")
    try:
        expected = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as error:
        raise _SourceUnavailable(
            "record_invalid", "Agent Router RECORD contains an invalid sha256"
        ) from error
    canonical = base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
    if len(expected) != hashlib.sha256().digest_size or canonical != value:
        raise _SourceUnavailable(
            "record_invalid", "Agent Router RECORD contains a noncanonical sha256"
        )
    return expected


def _record_size(raw_size: str) -> int:
    if (
        not raw_size.isascii()
        or not raw_size.isdecimal()
        or (len(raw_size) > 1 and raw_size.startswith("0"))
    ):
        raise _SourceUnavailable(
            "record_invalid", "Agent Router RECORD contains an invalid package size"
        )
    return int(raw_size)


def _verified_file(
    dist, root: Path, path: str, encoded_hash: str, raw_size: str
) -> bytes:
    candidate = Path(dist.locate_file(path))
    try:
        candidate_info = candidate.lstat()
        resolved = candidate.resolve()
        actual = candidate.read_bytes()
    except OSError as error:
        raise _SourceUnavailable(
            "record_unavailable", "Agent Router RECORD file is unavailable"
        ) from error
    if (
        not stat.S_ISREG(candidate_info.st_mode)
        or candidate.is_symlink()
        or not resolved.is_relative_to(root)
    ):
        raise _SourceUnavailable("record_invalid", "Agent Router RECORD file is unsafe")
    if len(actual) != _record_size(raw_size) or hashlib.sha256(
        actual
    ).digest() != _record_hash(encoded_hash):
        raise _SourceUnavailable(
            "record_mismatch", "Agent Router installed bytes do not match RECORD"
        )
    return actual


def _package_paths(package: Path, root: Path) -> set[str]:
    paths = set()
    for candidate in package.rglob("*"):
        relative = candidate.relative_to(root)
        if "__pycache__" in relative.parts:
            continue
        try:
            info = candidate.lstat()
        except OSError as error:
            raise _SourceUnavailable(
                "record_unavailable", "Agent Router package contents are unavailable"
            ) from error
        if stat.S_ISDIR(info.st_mode):
            if candidate.is_symlink():
                raise _SourceUnavailable(
                    "record_invalid",
                    "Agent Router package contains a directory symlink",
                )
            continue
        if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
            raise _SourceUnavailable(
                "record_invalid", "Agent Router package contains an unsafe file"
            )
        paths.add(relative.as_posix())
    return paths


def _record_snapshot(dist) -> tuple[str, bytes | None, Path]:
    try:
        content = dist.read_text("RECORD")
    except OSError as error:
        raise _SourceUnavailable(
            "record_unavailable", "Agent Router RECORD is unavailable"
        ) from error
    if content is None:
        raise _SourceUnavailable(
            "record_unavailable", "Agent Router installation has no RECORD"
        )
    if len(content.encode()) > MAX_RECORD_BYTES:
        raise _SourceUnavailable(
            "record_invalid", "Agent Router RECORD exceeds its size limit"
        )
    root = Path(dist.locate_file("")).resolve()
    package = Path(dist.locate_file("router"))
    try:
        package_info = package.lstat()
    except OSError as error:
        raise _SourceUnavailable(
            "record_unavailable", "Agent Router package directory is unavailable"
        ) from error
    if (
        not stat.S_ISDIR(package_info.st_mode)
        or package.is_symlink()
        or not package.resolve().is_relative_to(root)
    ):
        raise _SourceUnavailable(
            "record_invalid", "Agent Router package directory is unsafe"
        )
    package = package.resolve()

    rows: dict[str, tuple[str, str, int, bytes]] = {}
    direct_url: bytes | None = None
    direct_url_path: str | None = None
    try:
        parsed = csv.reader(io.StringIO(content), strict=True)
        for row in parsed:
            if len(row) != 3:
                raise _SourceUnavailable(
                    "record_invalid", "Agent Router RECORD row must have three fields"
                )
            raw_path, encoded_hash, raw_size = row
            path = PurePosixPath(raw_path)
            package_row = bool(path.parts) and path.parts[0] == "router"
            direct_row = (
                len(path.parts) == 2
                and path.parts[0].endswith(".dist-info")
                and path.parts[1] == "direct_url.json"
            )
            if not package_row and not direct_row:
                continue
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in raw_path
                or raw_path != path.as_posix()
            ):
                raise _SourceUnavailable(
                    "record_invalid", "Agent Router RECORD contains an unsafe path"
                )
            canonical_path = path.as_posix()
            actual = _verified_file(dist, root, canonical_path, encoded_hash, raw_size)
            if direct_row:
                if direct_url_path is not None:
                    raise _SourceUnavailable(
                        "record_invalid",
                        "Agent Router RECORD contains duplicate direct_url provenance",
                    )
                direct_url_path, direct_url = canonical_path, actual
                rows[canonical_path] = (
                    encoded_hash,
                    raw_size,
                    len(actual),
                    hashlib.sha256(actual).digest(),
                )
                continue
            if canonical_path in rows:
                raise _SourceUnavailable(
                    "record_invalid",
                    "Agent Router RECORD contains a duplicate package path",
                )
            rows[canonical_path] = (
                encoded_hash,
                raw_size,
                len(actual),
                hashlib.sha256(actual).digest(),
            )
    except csv.Error as error:
        raise _SourceUnavailable(
            "record_invalid", "Agent Router RECORD is invalid CSV"
        ) from error

    package_rows = {path for path in rows if path.startswith("router/")}
    if "router/__init__.py" not in package_rows:
        raise _SourceUnavailable(
            "record_invalid", "Agent Router RECORD has no package root"
        )
    if _package_paths(package, root) != package_rows:
        raise _SourceUnavailable(
            "record_mismatch", "Agent Router package files do not match RECORD"
        )

    try:
        stable_content = dist.read_text("RECORD")
    except OSError as error:
        raise _SourceUnavailable(
            "installation_changed", "Agent Router RECORD changed during capture"
        ) from error
    if stable_content != content or _package_paths(package, root) != package_rows:
        raise _SourceUnavailable(
            "installation_changed", "Agent Router installation changed during capture"
        )
    for path, (encoded_hash, raw_size, size, file_digest) in rows.items():
        try:
            stable = _verified_file(dist, root, path, encoded_hash, raw_size)
        except _SourceUnavailable as error:
            raise _SourceUnavailable(
                "installation_changed",
                "Agent Router installation changed during capture",
            ) from error
        if len(stable) != size or hashlib.sha256(stable).digest() != file_digest:
            raise _SourceUnavailable(
                "installation_changed",
                "Agent Router installation changed during capture",
            )

    digest = hashlib.sha256()
    for path in sorted(package_rows):
        _, _, size, file_digest = rows[path]
        encoded_path = path.encode()
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(size.to_bytes(8, "big"))
        digest.update(file_digest)
    return digest.hexdigest(), direct_url, package


def _verify_module_origins(package: Path) -> None:
    for name, module in tuple(sys.modules.items()):
        if name != "router" and not name.startswith("router."):
            continue
        origin = getattr(module, "__file__", None)
        if not isinstance(origin, str):
            raise _SourceUnavailable(
                "module_origin_mismatch", "A loaded Router module has no file origin"
            )
        try:
            resolved = Path(origin).resolve(strict=True)
        except OSError as error:
            raise _SourceUnavailable(
                "module_origin_mismatch", "A loaded Router module origin is unavailable"
            ) from error
        if not resolved.is_relative_to(package):
            raise _SourceUnavailable(
                "module_origin_mismatch",
                "Loaded Router modules do not match the installed distribution",
            )


def _source_provenance() -> dict:
    try:
        dist = distribution(DISTRIBUTION)
    except (PackageNotFoundError, OSError) as error:
        raise _SourceUnavailable(
            "distribution_unavailable", "Agent Router distribution is unavailable"
        ) from error
    try:
        version = dist.version
    except (OSError, TypeError, ValueError) as error:
        raise _SourceUnavailable(
            "distribution_unavailable", "Agent Router version is unavailable"
        ) from error
    if (
        not isinstance(version, str)
        or not version
        or len(version) > 128
        or not version.isascii()
        or not version.isprintable()
    ):
        raise _SourceUnavailable(
            "distribution_invalid", "Agent Router distribution version is invalid"
        )
    content_sha256, direct_url, package = _record_snapshot(dist)
    _verify_module_origins(package)
    try:
        stable_version = dist.version
    except (OSError, TypeError, ValueError) as error:
        raise _SourceUnavailable(
            "installation_changed", "Agent Router version changed during capture"
        ) from error
    if stable_version != version:
        raise _SourceUnavailable(
            "installation_changed", "Agent Router version changed during capture"
        )
    return {
        "status": "verified",
        "scheme": SCHEME,
        "distribution": DISTRIBUTION,
        "version": version,
        "commit": _commit(direct_url),
        "content_sha256": content_sha256,
    }


def capture_startup_provenance() -> dict:
    """Capture one process identity and a verified or explicitly unavailable source."""
    pid = os.getpid()
    identity = creation_identity(pid)
    try:
        source = _source_provenance()
    except _SourceUnavailable as error:
        source = {
            "status": "unavailable",
            "scheme": SCHEME,
            "distribution": DISTRIBUTION,
            "reason": error.reason,
        }
    return {
        "schema_version": 1,
        "pid": pid,
        "creation_identity": identity,
        "source": source,
    }
