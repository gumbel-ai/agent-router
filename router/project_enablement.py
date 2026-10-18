"""Explicit project configuration and confirmed migration of Router's global entries."""

import json
import os
import stat
import sys
from collections.abc import MutableMapping
from contextlib import chdir
from pathlib import Path

import tomlkit
from tomlkit.exceptions import ParseError

from router.paths import PROJECT_MARKER, PROJECT_MARKER_CONTENT
from router.snapshot import git

PLUGIN_ID = "agent-router@agent-router"


def read_configuration(path: Path) -> str | None:
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError(f"Refusing linked configuration directory: {parent}")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise ValueError(f"Configuration must be a regular file, not a link: {path}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Configuration must be a regular file: {path}")
        return stream.read()


def json_configuration(path: Path) -> dict:
    raw = read_configuration(path)

    def unique_object(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("Duplicate JSON object keys")
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique_object) if raw is not None else {}
    except ValueError as error:
        raise ValueError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Configuration must be a JSON object: {path}")  # noqa: TRY004 - public configuration error
    return value


def toml_configuration(path: Path):
    raw = read_configuration(path)
    try:
        return tomlkit.parse(raw if raw is not None else "")
    except ParseError as error:
        raise ValueError(f"Invalid TOML in {path}: {error}") from error


def plugin_table(config, path: Path):
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, MutableMapping):
        raise ValueError(f"plugins must be a table in {path}")  # noqa: TRY004 - public configuration error
    entry = plugins.setdefault(PLUGIN_ID, {})
    if not isinstance(entry, MutableMapping):
        raise ValueError(f"Router's plugin entry must be a table in {path}")  # noqa: TRY004 - public configuration error
    if "enabled" in entry and not isinstance(entry["enabled"], bool):
        raise ValueError(f"Router's plugin enabled value must be a boolean in {path}")
    return entry


def claude_home() -> Path:
    selected = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    if not selected:
        raise ValueError("CLAUDE_CONFIG_DIR must not be empty")
    return Path(selected).expanduser().resolve()


def claude_user_install(home: Path) -> bool:
    installed = json_configuration(home / "plugins/installed_plugins.json")
    plugins = installed.get("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("Claude installed_plugins.json plugins must be an object")  # noqa: TRY004 - public configuration error
    entries = plugins.get(PLUGIN_ID, [])
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Claude installed_plugins.json Router entries must be objects in a list")
    return any(entry.get("scope") == "user" for entry in entries)


def remove_claude_global(home: Path, user_install: bool) -> None:
    from router.setup import private_write, run

    if user_install:
        run(["claude", "plugin", "uninstall", PLUGIN_ID, "--scope", "user", "--keep-data"])
    settings = home / "settings.json"
    current = json_configuration(settings)
    enabled = current.get("enabledPlugins", {})
    if not isinstance(enabled, dict):
        raise ValueError(f"enabledPlugins must be an object in {settings}")  # noqa: TRY004 - public configuration error
    if PLUGIN_ID in enabled:
        enabled.pop(PLUGIN_ID)
        private_write(settings, json.dumps(current, indent=2) + "\n")


def enable() -> None:
    from router.setup import private_write, run

    cwd = Path.cwd().resolve()
    root = Path(git(cwd, "rev-parse", "--show-toplevel").strip()).resolve()
    marker, codex = root / PROJECT_MARKER, root / ".codex/config.toml"
    claude_paths = [root / ".claude/settings.json"]
    if cwd != root:
        claude_paths.append(cwd / ".claude/settings.json")
    existing = read_configuration(marker)
    if existing is not None:
        value = json_configuration(marker)
        if set(value) != {"observe"} or value["observe"] is not True:
            raise ValueError(f"Refusing to replace conflicting {marker}; expected exactly {{\"observe\": true}}")
    for claude in claude_paths:
        settings = json_configuration(claude)
        if not isinstance(settings.get("enabledPlugins", {}), dict):
            raise ValueError(f"enabledPlugins must be an object in {claude}")  # noqa: TRY004 - public configuration error
    plugin_table(toml_configuration(codex), codex)
    with chdir(root):
        run(["claude", "plugin", "install", PLUGIN_ID, "--scope", "project"])
    # Re-read after installation: preserve configuration the client wrote as well.
    settings_by_path = {}
    for claude in claude_paths:
        settings = json_configuration(claude)
        enabled = settings.setdefault("enabledPlugins", {})
        if not isinstance(enabled, dict):
            raise ValueError(f"enabledPlugins must be an object in {claude}")  # noqa: TRY004 - public configuration error
        enabled[PLUGIN_ID] = True
        settings_by_path[claude] = json.dumps(settings, indent=2) + "\n"
    config = toml_configuration(codex)
    plugin_table(config, codex)["enabled"] = True
    for claude, content in settings_by_path.items():
        private_write(claude, content)
    private_write(codex, tomlkit.dumps(config))
    private_write(marker, json.dumps(PROJECT_MARKER_CONTENT) + "\n")
    print("Project observation configured. Commit .agent-router.json, .claude/settings.json and .codex/config.toml. "
          "Start fresh sessions; Codex still requires project trust and manual hook trust through /hooks.")
    if cwd != root:
        print(f"Claude also enabled in {cwd.relative_to(root)}/.claude/settings.json; "
              "commit that file to share this launch-directory opt-in.")


def migrate_global() -> None:
    from router.setup import private_write

    home = claude_home()
    user_install = claude_user_install(home)
    claude_settings = home / "settings.json"
    settings = json_configuration(claude_settings)
    enabled = settings.get("enabledPlugins", {})
    if not isinstance(enabled, dict):
        raise ValueError(f"enabledPlugins must be an object in {claude_settings}")  # noqa: TRY004 - public configuration error
    if PLUGIN_ID in enabled and not isinstance(enabled[PLUGIN_ID], bool):
        raise ValueError(f"Router's enabledPlugins entry must be a boolean in {claude_settings}")
    global_claude = enabled.get(PLUGIN_ID) is True
    selected = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    if not selected:
        raise ValueError("CODEX_HOME must not be empty")
    codex = Path(selected).expanduser().resolve() / "config.toml"
    config = toml_configuration(codex)
    configured = config.get("plugins", {})
    if not isinstance(configured, MutableMapping):
        raise ValueError(f"plugins must be a table in {codex}")  # noqa: TRY004 - public configuration error
    entry = plugin_table(config, codex) if PLUGIN_ID in configured else None
    # Codex treats a configured plugin without an enabled field as enabled.
    global_codex = entry is not None and entry.get("enabled", True)
    if not user_install and not global_claude and not global_codex:
        return
    print("Legacy global Router enablement found. Until migrated, Router hooks can still start in other projects.")
    if not sys.stdin.isatty():
        print("Migration requires separate interactive confirmation; run router setup in a terminal. Legacy entries retained.")
        return
    answer = input("Remove only Router's user-scope Claude install/entry and disable its global Codex entry? "
                   "History is retained. [y/N] ")
    if answer.strip().lower() not in {"y", "yes"}:
        print("Legacy Router entries retained.")
        return
    if user_install or global_claude:
        remove_claude_global(home, user_install)
    if global_codex:
        # Preserve unrelated tables and comments, including hook trust, byte for byte.
        current = toml_configuration(codex)
        plugin_table(current, codex)["enabled"] = False
        private_write(codex, tomlkit.dumps(current))
    print("Router's global enablement removed. Existing project enablement and history retained.")
