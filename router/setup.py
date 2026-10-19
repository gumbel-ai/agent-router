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
    if sys.platform not in {"darwin", "linux"}:
        raise ValueError("Router setup supports macOS and Linux")
    if not shutil.which("claude") or not shutil.which("uv"):
        raise ValueError("Install Claude Code and uv first")
    load_settings()
    current = require_idle()
    installation = "CLI and background service" if sys.platform == "darwin" else "CLI"
    confirm(f"Install Agent Router's {installation} and register plugin marketplaces? "
            "Plugins are enabled separately per project. Local observation history stays on this machine.", yes)
    tool_dir = Path(run(["uv", "tool", "dir"])) / "agent-router"
    if current:
        if sys.platform == "linux":
            raise ValueError("Stop the manually started daemon with router daemon stop before router setup; "
                             "restart it afterward with router daemon start")
        label, _ = identity()
        from router.service import domain, loaded
        if not loaded(f"{domain()}/{label}"):
            raise ValueError("Stop the manually started daemon before installing the managed service")
    if Path(sys.prefix).resolve() != tool_dir.resolve():
        if current:
            stop()
        # uvx's temporary environment is not a durable service executable.
        run(["uv", "tool", "install", "--force", "--python", "3.14", "--from", source(), "agent-router"])
        command = tool_dir / "bin" / "router"
        result = subprocess.run([str(command), "setup", "--yes", "--marketplace", marketplace], check=False)
        if result.returncode:
            raise ValueError("Persistent Router setup failed; retry router setup after resolving the reported error")
        return
    from router.project_enablement import migrate_global
    migrate_global()
    run(["claude", "plugin", "marketplace", "add", marketplace])
    register_codex_marketplace(marketplace)
    private_write(runtime_pointer(), str(Path(sys.executable).absolute()) + "\n")
    if sys.platform == "darwin":
        result = start(restart=bool(current))
        print(f"Agent Router service ready ({result['version']}).")
    else:
        print("Agent Router CLI installed. No background service was installed. "
              "Run router daemon start in a separate terminal and keep it running.")
    print("Run router enable in each project to observe, then use ordinary claude or codex there "
          "and router status --watch in a terminal.")
    print("External delegation is opt-in: run router init in each target project. Restart existing Claude sessions to load the plugin.")


def permissions(value: object) -> bool:
    return (bool(value) and all(permissions(item) for item in value.values())) if isinstance(value, dict) else isinstance(value, str) and value in {"allow", "deny"}


def initialize(repo: Path, model: str, setup_command: str, permission: dict | None,
               concurrency: int, timeout: float, attempts: int, *, executor: str = "opencode", sandbox: str | None = None) -> None:
    repo = Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
    validate_repository(repo)
    if not setup_command.strip() or "=" not in model:
        raise ValueError("Provide a setup command and model alias=provider/exact-model")
    alias, exact = model.split("=", 1)
    if not alias.strip() or "/" not in exact or not exact.strip():
        raise ValueError("Model must be alias=provider/exact-model, without substitution")
    if executor not in {"opencode", "codex", "claude"}:
        raise ValueError("Unsupported executor; no fallback")
    if executor == "codex" and (not exact.startswith("openai/") or not exact.removeprefix("openai/").strip()):
        raise ValueError("Codex requires an exact openai/model ID; custom providers are unsupported")
    if executor == "codex" and (sandbox not in {"read-only", "workspace-write"} or permission is not None):
        raise ValueError("Codex requires an explicit read-only/workspace-write sandbox, not OpenCode permissions")
    if executor != "codex" and sandbox is not None:
        raise ValueError("--sandbox is for Codex; other executors use explicit permissions")
    if executor == "claude":
        from router.claude_executor import validate
        validate(exact, permission)
    executable = shutil.which(executor)
    if not executable:
        raise ValueError(f"Install {executor} and configure its provider access before enabling delegation")
    existing = repo / "opencode.json"
    if executor == "opencode" and existing.exists():
        current = json.loads(existing.read_text()).get("permission")
        if permission is not None and permission != current:
            raise ValueError("Refusing to overwrite existing opencode.json permissions; edit and approve them explicitly")
        permission = current
    if executor == "opencode" and (not isinstance(permission, dict) or not permissions(permission)):
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
        command_key = "AGENT_ROUTER_" + executor.upper() + "_COMMAND"
        values.setdefault(command_key, [executable])
        values.setdefault("AGENT_ROUTER_MODELS", {})[alias] = exact
        target = {**values.get("AGENT_ROUTER_REPOSITORIES", {}).get(str(repo), {}), "setup_command": setup_command}
        if executor == "codex":
            target["codex_sandbox"] = sandbox
        if executor == "claude":
            target["claude_permissions"] = json.dumps(permission)
        values.setdefault("AGENT_ROUTER_REPOSITORIES", {})[str(repo)] = target
        effective, _ = load_settings(values)
        for key, actual in (("AGENT_ROUTER_CONCURRENCY_CAP", effective.concurrency_cap),
                            ("AGENT_ROUTER_ATTEMPT_TIMEOUT_SECONDS", effective.attempt_timeout_seconds),
                            ("AGENT_ROUTER_MAX_ATTEMPTS", effective.max_attempts)):
            if actual != values[key]:
                raise ValueError(f"{key} overrides this choice; update or unset it before router init")
        if effective.models.get(alias) != exact or effective.repositories.get(str(repo)) != target:
            raise ValueError("Environment configuration overrides this model or repository setup; update or unset it before router init")
        expected, actual = values[command_key], getattr(effective, executor + "_command")
        if expected[1:] != actual[1:] or (shutil.which(expected[0]) or expected[0]) != (shutil.which(actual[0]) or actual[0]):
            raise ValueError("Environment configuration overrides the executor command; update or unset it before router init")
        if executor == "opencode" and not existing.exists():
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
    sandbox = args.sandbox
    if args.executor == "codex" and sandbox is None:
        if args.yes:
            raise ValueError("Noninteractive Codex init requires --sandbox read-only or workspace-write")
        sandbox = input("Codex sandbox (read-only or workspace-write): ").strip()
    if args.executor == "opencode" and not (repo / "opencode.json").exists() and permission is None:
        if args.yes:
            raise ValueError("Noninteractive init requires --permissions or an existing explicit opencode.json")
        permission = json.loads(input('OpenCode permission object (explicit allow/deny JSON): '))
    if args.executor == "claude" and permission is None:
        if args.yes:
            raise ValueError("Noninteractive Claude init requires --permissions with tools and allow lists")
        permission = json.loads(input('Claude permission object (tools and allow JSON lists): '))
    def choice(value, previous, fallback, label, cast):
        if value is not None:
            return value
        default = previous or fallback
        return cast(input(f"{label} [{default}]: ").strip() or default) if not args.yes else default
    cap = choice(args.concurrency, config.concurrency_cap, 3, "Global concurrency cap", int)
    timeout = choice(args.timeout, config.attempt_timeout_seconds, 600, "Per-attempt timeout seconds", float)
    attempts = choice(args.attempts, config.max_attempts, 3, "Maximum launches per task", int)
    profile = sandbox if args.executor == "codex" else permission if permission is not None else "existing opencode.json permissions"
    confirm(f"Enable {args.executor} delegation for {repo} with {model}, permissions {profile}, setup {setup_command!r}, and limits {cap}/{timeout}s/{attempts}?", args.yes)
    initialize(repo, model, setup_command, permission, cap, timeout, attempts, executor=args.executor, sandbox=sandbox)


def register_codex_marketplace(marketplace: str) -> None:
    """Register only: plugin enablement belongs to each project's configuration."""
    if not shutil.which("codex"):
        return
    command = ["codex", "plugin", "marketplace", "add", marketplace]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=120, check=False)
    except (subprocess.TimeoutExpired, OSError) as error:
        detail = str(error)
    else:
        if result.returncode == 0:
            print("Codex marketplace registered. Run router enable in each project; refresh plugins in Codex "
                  "and trust Router's hooks manually through /hooks.")
            return
        detail = result.stderr or result.stdout or f"exited with status {result.returncode}"
    print(f"Codex marketplace registration failed: {detail}. "
          f"Register it later with: codex plugin marketplace add {marketplace}")


def uninstall(yes: bool = False) -> None:
    from router.project_enablement import (
        claude_home,
        claude_user_install,
        remove_claude_global,
    )

    current = require_idle()
    if sys.platform == "linux" and current:
        raise ValueError("Stop the manually started daemon with router daemon stop before router uninstall")
    home = claude_home()
    user_install = claude_user_install(home)
    confirm("Remove Agent Router's user-scoped Claude plugin, CLI, and any managed service? Keep configuration and history.", yes)
    if sys.platform != "linux":
        stop(uninstall=True)
    remove_claude_global(home, user_install)
    if shutil.which("codex"):
        try:
            removed = subprocess.run(["codex", "plugin", "remove", PLUGIN_ID], stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=120, check=False).returncode == 0
        except (subprocess.TimeoutExpired, OSError):  # Codex optional: finish uninstalling.
            removed = False
        if not removed:
            print(f"Codex plugin not removed; remove it with: codex plugin remove {PLUGIN_ID}")
    runtime_pointer().unlink(missing_ok=True)
    run(["uv", "tool", "uninstall", "agent-router"])
    print("Agent Router uninstalled. Configuration, execution artifacts, and history retained.")
