"""Read-only check of whether Codex can deliver lifecycle hooks to Router.

Codex runs a plugin hook only when the plugin is enabled and the hook is trusted:
`[hooks.state."<plugin>@<marketplace>:<file>:<event>:<group>:<handler>"]` must hold
the SHA-256 of the hook's normalized declaration. Router never writes Codex config;
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


def expected_trust(plugin_id: str = PLUGIN_ID, hooks_file: str = HOOKS_FILE,
                   declarations: dict | None = None) -> dict[str, str]:
    """Trust-state keys Codex expects for this plugin, mapped to their hashes."""
    if declarations is None:
        declarations = json.loads((Path(__file__).parent / "plugin" / "codex" / "hooks.json").read_text())
    return {
        f"{plugin_id}:{hooks_file}:{_snake(event)}:{index}:0": hook_hash(event, group)
        for event, groups in declarations["hooks"].items()
        for index, group in enumerate(groups)
    }


def availability(codex_home: Path | None = None) -> dict:
    """Return {"state": ..., "detail": ...} describing Codex observation readiness."""
    if shutil.which("codex") is None:
        return {"state": "codex_not_installed", "detail": "Codex CLI not found on PATH"}
    home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        config = tomllib.loads((home / "config.toml").read_text())
    except FileNotFoundError:
        config = {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        return {"state": "unknown", "detail": f"Codex config unreadable: {error}"}
    plugin = config.get("plugins", {}).get(PLUGIN_ID)
    if not isinstance(plugin, dict):
        return {"state": "plugin_not_installed", "detail": "Run router setup, or install the Codex plugin"}
    if plugin.get("enabled") is False:
        return {"state": "plugin_disabled", "detail": f"Enable {PLUGIN_ID} in Codex"}
    state = config.get("hooks", {}).get("state", {})
    untrusted, modified, disabled = [], [], []
    for key, expected in expected_trust().items():
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
