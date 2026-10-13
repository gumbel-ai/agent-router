"""One-time global setup and explicit, non-executing project opt-in."""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import distribution
from pathlib import Path
from urllib.parse import unquote, urlparse

from router.config import load_settings, state_dir
from router.paths import registry, runtime_pointer
from router.service import (
    identity,
    refresh_service,
    require_idle,
    require_platform,
    run,
    start,
    stop,
)
from router.snapshot import git, validate_repository

PLUGIN_ID = "agent-router@agent-router"
MARKETPLACE = "gumbel-ai/agent-router"


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".router-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def confirm(message: str, yes: bool) -> None:
    if not yes and input(message + " [y/N] ").strip().lower() not in {"y", "yes"}:
        raise ValueError("No changes made")


def source() -> str:
    direct = json.loads(distribution("agent-router").read_text("direct_url.json") or "{}")
    url = direct.get("url", "")
    if direct.get("vcs_info"):
        return f"git+{url}@{direct['vcs_info']['commit_id']}"
    if url.startswith("file:"):
        return unquote(urlparse(url).path)
    return "agent-router==" + distribution("agent-router").version


def setup(yes: bool = False, marketplace: str = MARKETPLACE) -> None:
    # Probe prerequisites before changing either the CLI or host integration.
    require_platform()
    if not shutil.which("claude") or not shutil.which("uv"):
        raise ValueError("Install Claude Code and uv first")
    load_settings()
    current = require_idle()
    confirm("Install Agent Router's user-scoped Claude plugin and a background service? Local observation history stays on this machine.", yes)
    tool_dir = Path(run(["uv", "tool", "dir"])) / "agent-router"
    if Path(sys.prefix).resolve() != tool_dir.resolve():
        if current:
            label, _ = identity()
            from router.service import domain, loaded
            if not loaded(f"{domain()}/{label}"):
                raise ValueError("Stop the manually started daemon before installing the managed service")
            stop()
        # uvx's temporary environment is not a durable service executable.
        run(["uv", "tool", "install", "--force", "--python", "3.14", "--from", source(), "agent-router"])
        command = tool_dir / "bin" / "router"
        result = subprocess.run([str(command), "setup", "--yes", "--marketplace", marketplace], check=False)
        if result.returncode:
            raise ValueError("Persistent Router setup failed; retry router setup after resolving the reported error")
        return
    run(["claude", "plugin", "install", "agent-router", "--marketplace", marketplace, "--scope", "user"])
    private_write(runtime_pointer(), str(Path(sys.executable).absolute()) + "\n")
    result = start(restart=bool(current))
    print(f"Agent Router ready ({result['version']}). Run ordinary claude in any project, and router status --watch in a terminal.")
    print("OpenCode delegation is opt-in: run router init in each target project. Restart existing Claude sessions to load the plugin.")


def permissions(value: object) -> bool:
    return (bool(value) and all(permissions(item) for item in value.values())) if isinstance(value, dict) else isinstance(value, str) and value in {"allow", "deny"}


def initialize(repo: Path, model: str, setup_command: str, permission: dict | None,
               concurrency: int, timeout: float, attempts: int) -> None:
    repo = Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    validate_repository(repo)
    if not setup_command.strip() or "=" not in model:
        raise ValueError("Provide a setup command and model alias=provider/exact-model")
    alias, exact = model.split("=", 1)
    if not alias.strip() or "/" not in exact or not exact.strip():
        raise ValueError("Model must be alias=provider/exact-model, without substitution")
    executable = shutil.which("opencode")
    if not executable:
        raise ValueError("Install OpenCode and configure its provider access before enabling delegation")
    existing = repo / "opencode.json"
    if existing.exists():
        current = json.loads(existing.read_text()).get("permission")
        if permission is not None and permission != current:
            raise ValueError("Refusing to overwrite existing opencode.json permissions; edit and approve them explicitly")
        permission = current
    if not isinstance(permission, dict) or not permissions(permission):
        raise ValueError("Supply an explicit nonempty allow/deny permission object; ask is unsupported")
    current_daemon = require_idle()
    if current_daemon:
        if sys.platform != "darwin":
            raise ValueError("Stop the manual daemon, run router init, then restart it with router daemon start")
        label, _ = identity()
        from router.service import domain, loaded
        if not loaded(f"{domain()}/{label}"):
            raise ValueError("Stop the manually started daemon before router init; managed services reload automatically")
    directory = state_dir()
    if directory.resolve().is_relative_to(repo):
        raise ValueError("Router state must be outside the project")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config, _ = load_settings()
        if alias in config.models and config.models[alias] != exact:
            raise ValueError("Model alias already identifies another exact model; choose a new alias")
        values = registry()
        if alias in values.get("AGENT_ROUTER_MODELS", {}) and values["AGENT_ROUTER_MODELS"][alias] != exact:
            raise ValueError("Model alias already identifies another exact model in config.json; choose a new alias")
        values.update(AGENT_ROUTER_CONCURRENCY_CAP=concurrency, AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS=timeout,
                      AGENT_ROUTER_MAX_ATTEMPTS=attempts)
        values.setdefault("AGENT_ROUTER_OPENCODE_COMMAND", [executable])
        values.setdefault("AGENT_ROUTER_MODELS", {})[alias] = exact
        values.setdefault("AGENT_ROUTER_REPOSITORIES", {})[str(repo)] = {"setup_command": setup_command}
        effective, _ = load_settings(values)
        for key, actual in (("AGENT_ROUTER_CONCURRENCY_CAP", effective.concurrency_cap),
                            ("AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS", effective.attempt_timeout_seconds),
                            ("AGENT_ROUTER_MAX_ATTEMPTS", effective.max_attempts)):
            if actual != values[key]:
                raise ValueError(f"{key} overrides this choice; update or unset it before router init")
        if effective.models.get(alias) != exact or effective.repositories.get(str(repo)) != {"setup_command": setup_command}:
            raise ValueError("Environment configuration overrides this model or repository setup; update or unset it before router init")
        expected, actual = values["AGENT_ROUTER_OPENCODE_COMMAND"], effective.opencode_command
        if expected[1:] != actual[1:] or (shutil.which(expected[0]) or expected[0]) != (shutil.which(actual[0]) or actual[0]):
            raise ValueError("Environment configuration overrides the executor command; update or unset it before router init")
        if not existing.exists():
            private_write(existing, json.dumps({"permission": permission}, indent=2) + "\n")
        private_write(directory / "config.json", json.dumps(values, indent=2) + "\n")
    try:
        refresh_service()
    except ValueError as error:
        raise ValueError(f"Configuration saved, but service reload failed: {error}. Run router service restart when idle.") from error
    print(f"Delegation enabled for {repo}. Setup command recorded, not executed. Model: {alias}={exact}.")


def init(args) -> None:
    config, _ = load_settings()
    if args.yes and (not args.model or not args.setup_command):
        raise ValueError("Noninteractive init requires --model and --setup-command")
    model = args.model or input("Model alias=provider/exact-model: ").strip()
    setup_command = args.setup_command or input("Child dependency setup command (recorded, not run now): ").strip()
    repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    permission = json.loads(args.permissions) if args.permissions else None
    if not (repo / "opencode.json").exists() and permission is None:
        if args.yes:
            raise ValueError("Noninteractive init requires --permissions or an existing explicit opencode.json")
        permission = json.loads(input('OpenCode permission object (explicit allow/deny JSON): '))
    def choice(value, previous, fallback, label, cast):
        if value is not None:
            return value
        default = previous or fallback
        return cast(input(f"{label} [{default}]: ").strip() or default) if not args.yes else default
    cap = choice(args.concurrency, config.concurrency_cap, 3, "Global concurrency cap", int)
    timeout = choice(args.timeout, config.attempt_timeout_seconds, 600, "Per-attempt timeout seconds", float)
    attempts = choice(args.attempts, config.max_attempts, 3, "Maximum launches per task", int)
    confirm(f"Enable OpenCode delegation for {repo} with {model}, setup {setup_command!r}, and limits {cap}/{timeout}s/{attempts}?", args.yes)
    initialize(repo, model, setup_command, permission, cap, timeout, attempts)


def uninstall(yes: bool = False) -> None:
    require_idle()
    confirm("Remove Agent Router's user-scoped Claude plugin, service, and CLI? Keep configuration and history.", yes)
    stop(uninstall=True)
    run(["claude", "plugin", "uninstall", PLUGIN_ID, "--scope", "user", "--keep-data"])
    runtime_pointer().unlink(missing_ok=True)
    run(["uv", "tool", "uninstall", "agent-router"])
    print("Agent Router uninstalled. Configuration, execution artifacts, and history retained.")
