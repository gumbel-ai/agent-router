"""Stable, non-secret host and reboot identity."""

import fcntl
import hashlib
import os
import platform
import socket
import subprocess
import sys
import uuid
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from router.redaction import redact_secrets
from router.validation import HOST_STRING_LIMITS


def _digest(value: bytes) -> str:
    return hashlib.sha256(b"agent-router-host-v1\0" + value).hexdigest()


def _private_uuid(path: Path) -> str:
    def existing() -> str | None:
        try:
            with path.open() as stream:
                identity = os.fstat(stream.fileno())
                value = stream.read().strip()
        except FileNotFoundError:
            return None
        if not value:
            try:
                current = path.stat()
            except FileNotFoundError:
                return None
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                path.unlink(missing_ok=True)
                return None
            return existing()
        try:
            uuid.UUID(value)
        except ValueError as error:
            raise ValueError(f"Invalid generated host identity in {path}") from error
        path.chmod(0o600)
        return value
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(f".{path.name}.lock")
    lock_flags = os.O_RDWR | os.O_CREAT
    lock_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    lock_descriptor = os.open(lock_path, lock_flags, 0o600)
    try:
        os.fchmod(lock_descriptor, 0o600)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        while True:
            winner = existing()
            if winner is not None:
                return winner

            value = str(uuid.uuid4())
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(value + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    continue
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                return value
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        os.close(lock_descriptor)


def stable_host_id(directory: Path, system: str | None = None) -> str:
    system = system or sys.platform
    if system.startswith("linux"):
        for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                value = candidate.read_bytes().strip()
            except OSError:
                continue
            if value:
                return _digest(value)
    return _private_uuid(directory / "host-id")


def current_boot_id(system: str | None = None) -> str:
    system = system or sys.platform
    if system.startswith("linux"):
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            value = ""
        if value:
            return value
    if system == "darwin":
        try:
            # Hash the structured kernel boot-time value rather than exposing it.
            value = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.boottime"],
                check=True,
                capture_output=True,
                timeout=0.25,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            value = b""
        if value:
            return _digest(value)
    # Unsupported platforms cannot establish a durable reboot boundary.
    return "unknown"


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


@lru_cache(maxsize=1)
def _claude_version() -> str:
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=0.25, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    output = (result.stdout or result.stderr).strip().split()
    return output[0] if result.returncode == 0 and output else "unknown"


class HostIdentity:
    def __init__(self, directory: Path):
        self.host_id = stable_host_id(directory)
        self._static = {
            "host_id": self.host_id,
            "hostname": socket.gethostname(),
            "boot_id": current_boot_id(),
            "platform": "macos" if sys.platform == "darwin" else platform.system().lower(),
            "architecture": platform.machine(),
            "router_version": _package_version("agent-router"),
            "claude_version": _claude_version(),
        }

    def view(self, observed_at: int) -> dict:
        if (self._static.get("host_id") != self.host_id
                or len(self.host_id) > HOST_STRING_LIMITS["host_id"]
                or redact_secrets(self.host_id, len(self.host_id) + 16) != self.host_id):
            raise ValueError("Generated host identity is unsafe to serialize")
        result = {"host_id": self.host_id}
        for key, limit in HOST_STRING_LIMITS.items():
            if key == "host_id":
                continue
            value = self._static.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Host identity has invalid {key}")
            projected = redact_secrets(value, len(value) + 16)
            if not projected.strip() or len(projected) > limit:
                raise ValueError(f"Host identity has invalid {key}")
            result[key] = projected
        result["observed_at"] = observed_at
        return result
