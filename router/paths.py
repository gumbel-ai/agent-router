"""Lightweight shared paths and raw operator configuration for host hooks."""

import json
import os
import stat
from pathlib import Path

from dotenv import dotenv_values


def env_file() -> Path:
    return Path(os.environ.get("AGENT_ROUTER_ENV_FILE", "~/.agent-router/.env")).expanduser()


def path_setting(name: str, fallback: str) -> Path:
    value = os.environ.get(name)
    if value is None:
        value = dotenv_values(env_file()).get(name)
    return Path(value or fallback).expanduser()


def state_dir() -> Path:
    return path_setting("AGENT_ROUTER_STATE_DIR", "~/.agent-router")


def socket_path() -> Path:
    return path_setting("AGENT_ROUTER_SOCKET", str(state_dir() / "router.sock"))


def runtime_pointer() -> Path:
    # Bootstrap location matches the shell hook even when .env moves data state.
    return Path(os.environ.get("AGENT_ROUTER_STATE_DIR", "~/.agent-router")).expanduser() / "python-path"


def registry() -> dict:
    path = state_dir() / "config.json"
    values = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(values, dict):
        raise ValueError("Router config.json must be an object")  # noqa: TRY004 - public configuration error
    return values


def configuration(values: dict | None = None) -> tuple[dict, dict[str, str]]:
    values = dict(registry() if values is None else values)
    environment = {key: value for key, value in dotenv_values(env_file()).items() if value is not None}
    environment.update(os.environ)
    maps = {"AGENT_ROUTER_MODELS", "AGENT_ROUTER_REPOSITORIES", "AGENT_ROUTER_MODEL_PRICES"}
    for key, value in environment.items():
        if key.startswith("AGENT_ROUTER_"):
            value = json.loads(value) if key in maps or key in {"AGENT_ROUTER_OPENCODE_COMMAND", "AGENT_ROUTER_CODEX_COMMAND", "AGENT_ROUTER_CLAUDE_COMMAND"} else value
            if key in maps:
                if not isinstance(value, dict):
                    raise ValueError(f"{key} must be a JSON object")
                value = {**values.get(key, {}), **value}
            values[key] = value
    return values, environment


def delegation_target(cwd: str | None, repositories: dict) -> bool:
    return isinstance(cwd, str) and any(Path(cwd).resolve().is_relative_to(Path(root).expanduser().resolve()) for root in repositories)


PROJECT_MARKER = ".agent-router.json"
PROJECT_MARKER_CONTENT = {"observe": True}
MAX_PROJECT_MARKER_BYTES = 64


def observed_project(cwd: str | None) -> Path | None:
    """The opted-in project containing cwd: its nearest ancestor holding a project marker."""
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return None
    path = Path(cwd).resolve()  # `..` and symlinks must not place cwd inside a project it is not in.
    for directory in (path, *path.parents):
        marker = directory / PROJECT_MARKER
        if os.path.lexists(marker):
            # Check the file actually opened: a FIFO swapped in after a separate check would
            # block a plain open forever. O_NONBLOCK never waits; regular files ignore it.
            try:
                descriptor = os.open(marker, os.O_RDONLY | os.O_NONBLOCK)
            except FileNotFoundError as error:
                raise ValueError(f"{marker} must be a regular file") from error
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError(f"{marker} must be a regular file")
                raw = os.read(descriptor, MAX_PROJECT_MARKER_BYTES + 1)
            finally:
                os.close(descriptor)
            if len(raw) > MAX_PROJECT_MARKER_BYTES:
                raise ValueError(f'{marker} must contain exactly {{"observe": true}}')
            try:
                content = json.loads(raw)
            except ValueError as error:
                raise ValueError(f'{marker} must contain exactly {{"observe": true}}: {error}') from error
            # `is True`: JSON 1 compares equal to True, and only an explicit true opts in.
            if not isinstance(content, dict) or set(content) != {"observe"} or content["observe"] is not True:
                raise ValueError(f'{marker} must contain exactly {{"observe": true}}')
            return directory
    return None
