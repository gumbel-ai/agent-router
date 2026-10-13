"""Explicit user-service installation; ordinary clients and hooks never spawn it."""

import hashlib
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

from router.client import request
from router.paths import runtime_pointer, socket_path, state_dir


def run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"{command[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def health() -> dict | None:
    try:
        return request("health", {}, timeout=1)
    except ValueError as error:
        if socket_path().exists() and not isinstance(error.__cause__, ConnectionRefusedError):
            raise ValueError("Router socket exists but health is unavailable; resolve or stop the old daemon before setup") from None
        return None


def require_idle() -> dict | None:
    result = health()
    if result and result["active_tasks"]:
        raise ValueError("Finish or explicitly cancel active Router tasks before changing installation or configuration")
    database = state_dir() / "router.sqlite3"
    if result is None and database.exists():
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            tasks = [json.loads(row[0]) for row in db.execute("SELECT data FROM tasks")]
        from router.daemon import ACTIVE
        if any(task["status"] in ACTIVE or not task["execution_stopped"] or task.get("cleanup_pending") for task in tasks):
            raise ValueError("Recover and stop active Router tasks before changing installation or configuration")
    return result


def require_platform() -> None:
    if sys.platform != "darwin":
        raise ValueError("Automatic service setup currently supports macOS; on Linux use router daemon start")


def identity() -> tuple[str, Path]:
    require_platform()
    directory = state_dir().resolve()
    suffix = hashlib.sha256(str(directory).encode()).hexdigest()[:12]
    label = f"ai.gumbel.agent-router.{suffix}"
    return label, Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def definition() -> tuple[str, Path, bytes]:
    label, path = identity()
    try:
        interpreter = Path(runtime_pointer().read_text().strip())
    except OSError as error:
        raise ValueError("Managed runtime is not installed; run router setup") from error
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise ValueError("Managed runtime is unavailable; run router setup")
    directory = state_dir().resolve()
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "AGENT_ROUTER_STATE_DIR": str(directory),
                   "AGENT_ROUTER_SOCKET": str(socket_path().absolute()),
                   "AGENT_ROUTER_ENV_FILE": str(Path(os.environ.get("AGENT_ROUTER_ENV_FILE", "~/.agent-router/.env")).expanduser().absolute())}
    job = {"Label": label, "ProgramArguments": [str(interpreter), "-I", "-m", "router.cli", "daemon", "start"],
           "EnvironmentVariables": environment, "WorkingDirectory": str(Path.home()), "RunAtLoad": True,
           "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10,
           "StandardOutPath": str(directory / "service.log"), "StandardErrorPath": str(directory / "service.log")}
    return label, path, plistlib.dumps(job)


def domain() -> str:
    gui = f"gui/{os.getuid()}"
    probe = subprocess.run(["launchctl", "print", gui], capture_output=True, check=False)
    return gui if probe.returncode == 0 else f"user/{os.getuid()}"


def loaded(target: str) -> bool:
    return subprocess.run(["launchctl", "print", target], capture_output=True, check=False).returncode == 0


def shutdown_idle() -> None:
    if health():
        request("daemon_stop", {"only_if_idle": True})
        deadline = time.monotonic() + 15
        while socket_path().exists() and time.monotonic() < deadline:
            time.sleep(.1)
        if socket_path().exists():
            raise ValueError("Idle daemon did not stop; refusing to terminate it forcibly")


def start(*, restart: bool = False) -> dict:
    label, path, content = definition()
    target_domain = domain()
    target = f"{target_domain}/{label}"
    current = health()
    if current and not restart:
        return current
    require_idle()
    present = loaded(target)
    if current and not present:
        raise ValueError("A manually started daemon is running; stop it before switching to the managed service")
    if present:
        shutdown_idle()
        run(["launchctl", "bootout", target])
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)
    run(["launchctl", "bootstrap", target_domain, str(path)])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            result = health()
        except ValueError:
            result = None
        if result:
            return result
        time.sleep(.1)
    raise ValueError(f"Service did not become ready; inspect {directory / 'service.log'}")


def stop(*, uninstall: bool = False) -> None:
    label, path = identity()
    target = f"{domain()}/{label}"
    require_idle()
    if loaded(target):
        shutdown_idle()
        run(["launchctl", "bootout", target])
    if uninstall:
        path.unlink(missing_ok=True)


def refresh_service() -> None:
    """Only reload a service we manage; never seize a manually started daemon."""
    if health():
        start(restart=True)
