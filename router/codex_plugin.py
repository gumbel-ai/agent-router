"""Read-only check of whether Codex can deliver lifecycle hooks to Router.

Codex runs a plugin hook only when the plugin is enabled and the hook is trusted:
`[hooks.state."<plugin>@<marketplace>:<file>:<event>:<group>:<handler>"]` must hold
the SHA-256 of the hook's normalized declaration, as installed in Codex's plugin cache.
Router never writes Codex config;
this only reports which prerequisite is missing, so an untrusted or disabled plugin
is never mistaken for an idle Codex. See docs/codex-lifecycle.md.
"""

import hashlib
import json
import os
import shutil
import tomllib
from pathlib import Path

PLUGIN_ID = "agent-router@agent-router"
PLUGIN_CACHE = ("plugins", "cache", "agent-router", "agent-router")
HOOKS_FILE = "router/plugin/codex/hooks.json"
# Codex clamps these events' timeouts to 1-3 s; others default to 600 s.
SHORT_TIMEOUT_EVENTS = {"SessionEnd", "Interrupt"}


def _snake(event: str) -> str:
    return "".join("_" + character.lower() if character.isupper() else character for character in event).lstrip("_")


def _normalized_timeout(event: str, handler: dict) -> int:
    timeout = handler.get("timeout")
    if event in SHORT_TIMEOUT_EVENTS:
        return min(3, max(1, timeout if isinstance(timeout, int) else 1))
    return timeout if isinstance(timeout, int) else 600


def hook_hash(event: str, group: dict) -> str:
    """The trust hash Codex stores for one matcher group of a hooks file."""
    declaration = {"event_name": _snake(event), "hooks": []}
    if "matcher" in group:
        declaration["matcher"] = group["matcher"]
    for handler in group["hooks"]:
        normalized = {"type": handler["type"], "command": handler["command"],
                      "timeout": _normalized_timeout(event, handler), "async": bool(handler.get("async", False))}
        if "statusMessage" in handler:
            normalized["statusMessage"] = handler["statusMessage"]
        declaration["hooks"].append(normalized)
    encoded = json.dumps(declaration, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _version_key(version: str) -> tuple:
    parts = version.split(".")
    if len(parts) == 3 and all(part.isdecimal() for part in parts):
        return (1, tuple(int(part) for part in parts), version)
    return (0, (), version)


def active_plugin_root(home: Path) -> Path | None:
    """The cached plugin version Codex loads: `local` if present, else the highest version.

    Mirrors `active_plugin_version` in codex-rs/core-plugin-common/src/installed.rs
    (0.162.1). Codex runs and trusts this copy, which can differ from Router's package.
    """
    base = home.joinpath(*PLUGIN_CACHE)
    try:
        versions = [entry.name for entry in base.iterdir() if entry.is_dir()]
    except FileNotFoundError:
        return None
    if not versions:
        return None
    if "local" in versions:
        return base / "local"
    return base / max(versions, key=_version_key)


def expected_trust(declarations: dict, plugin_id: str = PLUGIN_ID, hooks_file: str = HOOKS_FILE) -> dict[str, str]:
    """Trust-state keys Codex expects for these hook declarations, mapped to their hashes."""
    return {
        f"{plugin_id}:{hooks_file}:{_snake(event)}:{index}:0": hook_hash(event, group)
        for event, groups in declarations["hooks"].items()
        for index, group in enumerate(groups)
    }


def availability(codex_home: Path | None = None) -> dict:
    """Return {"state": ..., "detail": ...} describing Codex observation readiness."""
    if shutil.which("codex") is None:
        return {"state": "codex_not_installed", "detail": "Codex CLI not found on PATH"}
    home = (codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")).expanduser()
    try:
        config = tomllib.loads((home / "config.toml").read_text())
    except FileNotFoundError:
        config = {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return {"state": "unknown", "detail": f"Codex config unreadable: {error}"}
    plugins, features, hooks = (config.get(name, {}) for name in ("plugins", "features", "hooks"))
    if not all(isinstance(table, dict) for table in (plugins, features, hooks)):
        return {"state": "unknown", "detail": "Codex config has an unexpected shape"}
    plugin = plugins.get(PLUGIN_ID)
    try:
        root = active_plugin_root(home)
    except OSError as error:
        return {"state": "unknown", "detail": f"Codex plugin cache unreadable: {error}"}
    if not isinstance(plugin, dict) or root is None:
        return {"state": "plugin_not_installed", "detail": "Run router setup, or install the Codex plugin"}
    if plugin.get("enabled") is False:
        return {"state": "plugin_disabled", "detail": f"Enable {PLUGIN_ID} in Codex"}
    # Codex applies features in key order, so `hooks` overrides its legacy alias `codex_hooks`.
    if features.get("hooks", features.get("codex_hooks")) is False:
        return {"state": "hooks_disabled", "detail": "Codex hooks are turned off in [features]"}
    try:
        declarations = json.loads((root / HOOKS_FILE).read_text())
        trust = expected_trust(declarations)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return {"state": "unknown", "detail": f"Installed Codex plugin hooks unreadable: {error}"}
    state = hooks.get("state", {})
    untrusted, modified, disabled = [], [], []
    for key, expected in trust.items():
        entry = state.get(key) if isinstance(state, dict) else None
        if not isinstance(entry, dict) or "trusted_hash" not in entry:
            untrusted.append(key)
        elif entry.get("enabled") is False:
            disabled.append(key)
        elif entry["trusted_hash"] != expected:
            modified.append(key)
    if untrusted or modified:
        count = len(untrusted) + len(modified)
        return {"state": "hooks_untrusted",
                "detail": f"{count} Router hook(s) not trusted for the current version; open Codex and run /hooks"}
    if disabled:
        return {"state": "hooks_disabled", "detail": f"{len(disabled)} Router hook(s) disabled in Codex /hooks"}
    return {"state": "ready", "detail": "Codex hooks are installed and trusted"}
