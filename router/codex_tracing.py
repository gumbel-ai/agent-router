"""Explicit Langfuse tracing selection for Router-launched Codex children.

Codex children intentionally ignore their user configuration.  The selected
Langfuse hook is therefore supplied as a per-process session configuration,
with the exact native trust entry required by Codex.  Credentials remain
operator environment inputs; Router never reads or copies a credential file.
"""

import hashlib
import json
import os
import shutil
from pathlib import Path

PLUGIN_ID = "langfuse-codex-observability@inline"
PLUGIN_VERSION = "0.4.2"
PLUGIN_FILES = (".codex-plugin/plugin.json", "hooks/hooks.json", "dist/index.mjs", "package.json")
# Digest of the reviewed 0.4.2 release at commit
# 586910c81eeaf21d8f54c1c86c89a29a5466c1ef.
AUDITED_DIGEST = "ec89e4f2be9ee013b0195d8697cb8d1478617d00a765eea6d9481193df0cffb3"
CONFIG_KEYS = {"plugin_dir"}
HOOK_COMMAND = 'node "${PLUGIN_ROOT}/dist/index.mjs"'
HOOK_STATUS = "Uploading Codex trace to Langfuse"
HOOK_TRUST_KEY = "/<session-flags>/config.toml:stop:0:0"
# Pinned plugin default; set explicitly so a worktree .codex/langfuse.json cannot redirect.
DEFAULT_BASE_URL = "https://cloud.langfuse.com"
HOOK_TRUST_HASH = "sha256:69a05cbfa6984ec5f1433343b45480d5239c119e7332ae863f9865edc2efec74"


def _configuration(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != CONFIG_KEYS
            or not isinstance(value["plugin_dir"], str) or not value["plugin_dir"]
            or len(value["plugin_dir"]) > 4096 or value["plugin_dir"] != value["plugin_dir"].strip()
            or any(not c.isprintable() for c in value["plugin_dir"])
            or not Path(value["plugin_dir"]).is_absolute()):
        raise ValueError("Codex tracing requires exactly one absolute plugin_dir")
    return value


def _inspect(directory: Path) -> str:
    try:
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Codex tracing plugin directory must be a real directory")
        digest = hashlib.sha256()
        for relative in PLUGIN_FILES:
            path = directory / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError("Codex tracing plugin files must be regular files without symlinks")
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                data = stream.read(16_777_217)
                after = os.fstat(stream.fileno())
            if len(data) > 16_777_216 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Codex tracing plugin file is oversized or changed while inspected")
            digest.update(relative.encode() + b"\0" + data + b"\0")
            if relative == ".codex-plugin/plugin.json":
                manifest = json.loads(data)
                if (not isinstance(manifest, dict) or manifest.get("name") != "tracing"
                        or manifest.get("version") != PLUGIN_VERSION
                        or manifest.get("hooks") != "./hooks/hooks.json"):
                    raise ValueError("Codex tracing requires the reviewed Langfuse 0.4.2 manifest")
            elif relative == "hooks/hooks.json":
                hooks = json.loads(data)
                expected = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": HOOK_COMMAND,
                                                                  "timeout": 30, "statusMessage": HOOK_STATUS}]}]}}
                if hooks != expected:
                    raise ValueError("Codex tracing hook declaration does not match the audited release")
        if digest.hexdigest() != AUDITED_DIGEST:
            raise ValueError("Codex tracing plugin does not match the audited upstream source")
        return digest.hexdigest()
    except (OSError, ValueError, TypeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Codex tracing plugin is unavailable, invalid or changed; inspect the selected source") from error


def availability(credentials_available: bool) -> dict:
    return {"available": credentials_available,
            "reason": ("Codex plugin export and Router correlation verified via the trusted session hook"
                       if credentials_available else
                       "Codex tracing plugin selected, but required Langfuse credentials are unavailable")}


def configure(raw: str, task: dict, host_id: str, repository: Path, credentials_available: bool) -> dict:
    try:
        if not isinstance(raw, str) or len(raw) > 16384:
            raise ValueError()
        config = _configuration(json.loads(raw))
        directory = Path(config["plugin_dir"]).resolve(strict=True)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Codex tracing configuration is invalid; expected exactly one explicit plugin_dir") from error
    repositories = {repository.resolve(), Path(task["invocation"]["parent_repo"]).resolve()}
    if any(directory.is_relative_to(root) for root in repositories):
        raise ValueError("Codex tracing plugin must be outside the delegated repository")
    correlation = {"router_task_id": task["task_id"], "host_id": host_id}
    if isinstance(task.get("child_session_id"), str):
        correlation["session_id"] = task["child_session_id"]
    if "work_item_id" in task:
        correlation["work_item_id"] = task["work_item_id"]
    tracing = {"requested": True, **availability(credentials_available), "plugin": PLUGIN_ID,
               "correlation": correlation}
    return {"_codex_tracing": {**config, "plugin_dir": str(directory), "schema_version": 1,
                                "digest": _inspect(directory)}, "tracing": tracing}


def selected(task: dict) -> dict | None:
    if "_codex_tracing" not in task:
        tracing = task.get("tracing")
        if isinstance(tracing, dict) and tracing.get("requested") and tracing.get("plugin") == PLUGIN_ID:
            raise ValueError("Codex tracing selection is missing; refusing an untraced replacement")
        return None
    config = task["_codex_tracing"]
    if (not isinstance(config, dict) or set(config) != CONFIG_KEYS | {"schema_version", "digest"}
            or type(config["schema_version"]) is not int or config["schema_version"] != 1):
        raise ValueError("Codex tracing selection is invalid; refusing an untraced replacement")
    tracing = task.get("tracing")
    if not (isinstance(tracing, dict) and tracing.get("requested") is True and tracing.get("plugin") == PLUGIN_ID):
        raise ValueError("Codex tracing was revoked after selection; refusing an untraced replacement")
    _configuration({key: config[key] for key in CONFIG_KEYS})
    if _inspect(Path(config["plugin_dir"])) != config["digest"]:
        raise ValueError("Codex tracing plugin changed after selection; refusing an untraced replacement")
    return config


def hook_configuration() -> str:
    """Inline TOML accepted by Codex's ``-c`` option for a trusted Stop hook."""
    # Use TOML basic strings for values containing shell syntax.  TOML literal
    # strings preserve backslashes, which would change the command Codex runs.
    return ("hooks={Stop=[{hooks=[{type='command',"
            "command=" + json.dumps(HOOK_COMMAND) + ","
            "timeout=30,statusMessage=" + json.dumps(HOOK_STATUS)
            + "}]}],state={" + json.dumps(HOOK_TRUST_KEY)
            + "={enabled=true,trusted_hash=" + json.dumps(HOOK_TRUST_HASH) + "}}}")


def environment(result: dict[str, str], task: dict, *, credentials: dict[str, str]) -> dict[str, str]:
    selected(task)
    # The trusted hook runs `node` outside the Codex sandbox. Mirror the Claude
    # route: drop startup-injection inputs and delegated-repository PATH entries.
    for key in tuple(result):
        if key.startswith(("LD_", "DYLD_", "BASH_FUNC_")) or key in {"NODE_OPTIONS", "BASH_ENV", "ENV", "ZDOTDIR"}:
            result.pop(key, None)
    roots = [Path(task["worktree"]).resolve(), Path(task["artifact_dir"]).resolve()]
    roots += [Path(task["invocation"][name]).resolve() for name in ("parent_repo", "parent_worktree")]
    result["PATH"] = os.pathsep.join(
        entry for entry in result.get("PATH", os.defpath).split(os.pathsep)
        if Path(entry).is_absolute() and not any(Path(entry).resolve().is_relative_to(root) for root in roots))
    node = shutil.which("node", path=result["PATH"])
    if node is None or any(Path(node).resolve().is_relative_to(root) for root in roots):
        raise ValueError("Codex tracing requires an operator-provisioned node outside delegated repositories")
    result["PLUGIN_ROOT"] = task["_codex_tracing"]["plugin_dir"]
    available = all(isinstance(credentials.get(key), str) and credentials[key].strip()
                    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"))
    if available:
        result["LANGFUSE_PUBLIC_KEY"] = credentials["LANGFUSE_PUBLIC_KEY"]
        result["LANGFUSE_SECRET_KEY"] = credentials["LANGFUSE_SECRET_KEY"]
    # The pinned plugin reads LANGFUSE_BASE_URL and LANGFUSE_TRACING_ENVIRONMENT only.
    result["LANGFUSE_BASE_URL"] = (credentials.get("LANGFUSE_BASE_URL") or credentials.get("LANGFUSE_BASEURL")
                                   or DEFAULT_BASE_URL)
    if credentials.get("LANGFUSE_ENVIRONMENT"):
        result["LANGFUSE_TRACING_ENVIRONMENT"] = credentials["LANGFUSE_ENVIRONMENT"]
    correlation = task["tracing"]["correlation"]
    metadata = {"router_task_id": correlation["router_task_id"], "router_host_id": correlation["host_id"]}
    session = correlation.get("session_id")
    if isinstance(session, str):
        metadata["router_child_session_id"] = session
    if "work_item_id" in correlation:
        metadata["work_item_id"] = correlation["work_item_id"]
    result.update(TRACE_TO_LANGFUSE="1" if available else "0", LANGFUSE_CODEX_USER_ID="agent-router",
                  LANGFUSE_CODEX_METADATA=json.dumps(metadata, separators=(",", ":")),
                  LANGFUSE_CODEX_TRACE_SEED=session or correlation["router_task_id"])
    return result
