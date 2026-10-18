"""Read-only check of whether Codex can deliver lifecycle hooks to Router.

Codex runs a plugin hook only when the plugin is enabled and the hook is trusted:
`[hooks.state."<plugin>@<marketplace>:<file>:<event>:<group>:<handler>"]` must hold
the SHA-256 of the hook's normalized declaration, as installed in Codex's plugin cache.
These readiness checks never write Codex config or hook trust;
they only report which prerequisite is missing, so an untrusted or disabled plugin
is never mistaken for an idle Codex. See docs/codex-lifecycle.md.
"""

import asyncio
import functools
import hashlib
import json
import os
import shutil
from pathlib import Path

from websockets.asyncio.client import unix_connect
from websockets.exceptions import WebSocketException

from router.observer_hook import CODEX_EVENTS
from router.paths import observed_project

PLUGIN_ID = "agent-router@agent-router"
PLUGIN_CACHE = ("plugins", "cache", "agent-router", "agent-router")
HOOKS_FILE = "router/plugin/codex/hooks.json"
# Codex clamps these events' timeouts to 1-3 s; others default to 600 s.
SHORT_TIMEOUT_EVENTS = {"SessionEnd", "Interrupt"}
READINESS_TIMEOUT = 5
READINESS_OUTPUT_LIMIT = 1024 * 1024


async def _readiness_evidence(home: Path, cwd: Path) -> tuple[dict, dict]:
    """Query the existing native daemon; never start one or refresh plugins."""
    # Native Codex can advertise long paths through short rendezvous aliases.
    endpoint = (home / "app-server-control" / "app-server-control.sock").resolve(strict=True)
    size = 0
    async with asyncio.timeout(READINESS_TIMEOUT):
        async with unix_connect(endpoint, open_timeout=READINESS_TIMEOUT, close_timeout=1,
                                max_size=READINESS_OUTPUT_LIMIT, max_queue=1,
                                compression=None, ping_interval=None, proxy=None) as connection:
            async def receive(identity):
                nonlocal size
                while True:
                    raw = await connection.recv()
                    size += len(raw.encode() if isinstance(raw, str) else raw)
                    if size > READINESS_OUTPUT_LIMIT:
                        raise ValueError("Codex readiness output exceeded the one MiB bound")
                    try:
                        message = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as failure:
                        raise ValueError(f"Codex readiness returned invalid JSON: {failure}") from failure
                    if not isinstance(message, dict):
                        raise TypeError("Codex readiness returned a non-object JSON message")
                    if type(message.get("id")) is not int or message["id"] != identity:
                        continue
                    if "error" in message:
                        failure = message["error"]
                        if not isinstance(failure, dict) or not isinstance(failure.get("message"), str):
                            raise ValueError("Codex readiness returned a malformed RPC error")
                        raise ValueError(f"Codex readiness RPC error: {failure['message']}")
                    if not isinstance(message.get("result"), dict):
                        raise TypeError("Codex readiness returned a malformed RPC result")
                    return message["result"]

            await connection.send(json.dumps({"id": 0, "method": "initialize", "params": {
                # Native daemon probes use this non-originating identity so initialize
                # leaves daemon-wide auth policy and client metadata unchanged (0.163.0).
                "clientInfo": {"name": "codex_app_server_daemon", "title": "Agent Router readiness probe", "version": "1"},
                "capabilities": {"experimentalApi": True}}}))
            daemon_home = (await receive(0)).get("codexHome")
            if not isinstance(daemon_home, str) or not Path(daemon_home).is_absolute():
                raise ValueError("Codex readiness initialization lacks an absolute daemon home")
            if Path(daemon_home).resolve() != home:
                raise ValueError("Codex readiness daemon serves a different Codex home")
            await connection.send(json.dumps({"method": "initialized", "params": {}}))
            await connection.send(json.dumps({"id": 1, "method": "config/read",
                                               "params": {"cwd": str(cwd), "includeLayers": False}}))
            result = (await receive(1)).get("config")
            if not isinstance(result, dict):
                raise TypeError("Codex readiness config/read result lacks a configuration object")
            await connection.send(json.dumps({"id": 2, "method": "hooks/list", "params": {"cwds": [str(cwd)]}}))
            data = (await receive(2)).get("data")
            if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict)
                    or data[0].get("cwd") != str(cwd) or not isinstance(data[0].get("hooks"), list)
                    or not isinstance(data[0].get("errors"), list)):
                raise ValueError("Codex readiness hook inventory is missing or malformed")
            if data[0]["errors"]:
                raise ValueError("Codex readiness hook inventory errors: " + json.dumps(data[0]["errors"])[:4096])
            # Native discovery excludes project layers from user hook trust.
            state = {}
            for hook in data[0]["hooks"]:
                if not isinstance(hook, dict):
                    raise TypeError("Codex readiness hook inventory contains a non-object")
                if hook.get("pluginId") != PLUGIN_ID:
                    continue
                key, status = hook.get("key"), hook.get("trustStatus")
                if (not isinstance(key, str) or key in state or type(hook.get("enabled")) is not bool
                        or status not in {"trusted", "untrusted", "modified"}):
                    raise ValueError("Codex readiness Router hook state is malformed or unsupported")
                state[key] = {"enabled": hook["enabled"]}
                if status == "trusted":
                    if not isinstance(hook.get("currentHash"), str):
                        raise ValueError("Codex readiness trusted Router hook has no current hash")
                    state[key]["trusted_hash"] = hook["currentHash"]
            return result, state


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


def _identifiers(text: str, numeric_leading_zero_ok: bool) -> list | None:
    parts = text.split(".")
    for part in parts:
        if not part or not all(c.isascii() and (c.isalnum() or c == "-") for c in part):
            return None
        if part.isdigit() and len(part) > 1 and part[0] == "0" and not numeric_leading_zero_ok:
            return None
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in parts]


def _semver(version: str) -> tuple | None:
    """SemVer 2.0 precedence key (Rust `semver::Version` order), or None if invalid."""
    core, _, build = version.partition("+")
    core, has_pre, pre = core.partition("-")
    numbers = core.split(".")
    if len(numbers) != 3 or not all(n.isascii() and n.isdigit() and (n == "0" or n[0] != "0") for n in numbers):
        return None
    pre_ids = _identifiers(pre, False) if has_pre else []
    build_ids = _identifiers(build, True) if "+" in version else []
    if pre_ids is None or build_ids is None:
        return None
    # A release sorts after its pre-releases; build metadata breaks remaining ties.
    return tuple(int(n) for n in numbers), (not pre_ids, pre_ids), build_ids


def _compare_versions(left: str, right: str) -> int:
    """Mirror of `compare_plugin_versions`: SemVer when both parse, else lexical."""
    keys = (_semver(left), _semver(right))
    a, b = keys if None not in keys else (left, right)
    return (a > b) - (a < b)


def _valid_version_segment(version: str) -> bool:
    return (version not in {"", ".", ".."}
            and all(c.isascii() and (c.isalnum() or c in "-_.+") for c in version))


def active_plugin_root(home: Path) -> Path | None:
    """The cached plugin version Codex loads: `local` if present, else the highest version.

    Mirrors `active_plugin_version` in codex-rs/core-plugin-common/src/installed.rs
    (0.162.1). Codex runs and trusts this copy, which can differ from Router's package.
    """
    base = home.joinpath(*PLUGIN_CACHE)
    try:
        versions = [entry.name for entry in base.iterdir()
                    if entry.is_dir() and _valid_version_segment(entry.name)]
    except FileNotFoundError:
        return None
    if not versions:
        return None
    if "local" in versions:
        return base / "local"
    return base / max(versions, key=functools.cmp_to_key(_compare_versions))


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
    selected = codex_home if codex_home is not None else os.environ.get("CODEX_HOME")
    if selected is None:
        try:
            selected = Path.home() / ".codex"
        except RuntimeError as error:
            return {"state": "unknown", "detail": f"Default Codex home unavailable: {error}"}
    if not selected:
        return {"state": "unknown", "detail": "CODEX_HOME must not be empty"}
    try:
        home, cwd = Path(selected).expanduser().resolve(), Path.cwd().resolve()
    except (OSError, ValueError, RuntimeError) as error:
        return {"state": "unknown", "detail": f"Readiness paths unavailable: {error}"}
    try:
        if observed_project(str(cwd)) is None:
            return {"state": "project_not_enabled", "detail": "Run router enable in this project and commit its configuration"}
    except (OSError, ValueError) as error:
        return {"state": "unknown", "detail": f"Project admission unavailable: {error}"}
    try:
        root = active_plugin_root(home)
        if root is None:
            return {"state": "plugin_not_installed", "detail": "Refresh Router's plugin cache in the selected Codex home"}
    except OSError as error:
        return {"state": "unknown", "detail": f"Codex plugin cache unreadable: {error}"}
    try:
        config, state = asyncio.run(_readiness_evidence(home, cwd))
    except TimeoutError:
        return {"state": "unknown", "detail": "Codex readiness deadline exceeded; existing daemon did not reply"}
    except (OSError, ValueError, TypeError, WebSocketException) as error:
        return {"state": "unknown", "detail": f"Codex readiness unavailable; start Codex normally in the selected home: {error}"}
    # config/read represents absent optional TOML tables as JSON null.
    plugins, features, hooks = ({} if config.get(name) is None else config[name]
                               for name in ("plugins", "features", "hooks"))
    if not all(isinstance(table, dict) for table in (plugins, features, hooks)):
        return {"state": "unknown", "detail": "Codex config has an unexpected shape"}
    plugin = plugins.get(PLUGIN_ID)
    if not isinstance(plugin, dict):
        return {"state": "plugin_not_installed", "detail": "Run router setup and router enable; refresh plugins in Codex"}
    if ("enabled" in plugin and type(plugin["enabled"]) is not bool
            or any(name in features and type(features[name]) is not bool for name in ("plugins", "hooks", "codex_hooks"))):
        return {"state": "unknown", "detail": "Codex plugin and hook feature flags must be booleans"}
    if plugin.get("enabled") is False:
        return {"state": "plugin_disabled", "detail": f"Enable {PLUGIN_ID} in Codex"}
    if features.get("plugins") is False:
        return {"state": "plugin_disabled", "detail": "Codex plugins are turned off in [features]"}
    # Codex applies features in key order, so `hooks` overrides its legacy alias `codex_hooks`.
    if features.get("hooks", features.get("codex_hooks")) is False:
        return {"state": "hooks_disabled", "detail": "Codex hooks are turned off in [features]"}
    try:
        declarations = json.loads((root / HOOKS_FILE).read_text())
        trust = expected_trust(declarations)
        missing = sorted(event for event in CODEX_EVENTS
                         if not any(group.get("hooks") for group in declarations["hooks"].get(event) or []))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return {"state": "unknown", "detail": f"Installed Codex plugin hooks unreadable: {error}"}
    if missing:
        return {"state": "unknown", "detail": "Installed Codex plugin lacks hooks for " + ", ".join(missing)}
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
