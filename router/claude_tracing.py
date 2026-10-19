"""Explicit Claude hook selection; native Claude owns all secret option delivery."""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

PLUGIN_ID = "langfuse-observability@inline"
PLUGIN_FILES = (".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/langfuse_hook.py")
# b5b7cc364996467e856dc17575b5674f4799ad87, including relative names and NUL separators.
AUDITED_DIGEST = "776d495fbc91ffc486e04fad0ccf2975084c299ff1034e44f7b183149bf369e5"
CONFIG_KEYS = {"plugin_dir", "public_key", "base_url"}
AUTH_KEYS = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"}


def _configuration(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != CONFIG_KEYS
            or any(not isinstance(item, str) or not item or len(item) > 4096
                   or item != item.strip() or any(not c.isprintable() for c in item) for item in value.values())):
        raise ValueError("Claude tracing requires exactly plugin_dir, public_key and base_url strings; no secret setting")
    if not Path(value["plugin_dir"]).is_absolute() or not value["public_key"].startswith("pk-lf-"):
        raise ValueError("Claude tracing requires an absolute plugin directory and a Langfuse public key")
    try:
        url = urlsplit(value["base_url"])
        valid = url.scheme in {"https", "http"} and url.hostname and not (url.username or url.password or url.query or url.fragment)
        _ = url.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Claude tracing base_url must be an HTTP(S) endpoint without credentials, query or fragment")
    return value


def _inspect(directory: Path) -> str:
    try:
        # The selected release is a classic-hook-only plugin. Extra native
        # components or import shadows must not become another execution route.
        if (any((directory / name).exists() for name in
                ("agents", "skills", "commands", "workflows", "monitors", "bin", ".mcp.json", ".lsp.json", "settings.json"))
                or (directory / ".claude-plugin").is_symlink() or (directory / "hooks").is_symlink()
                or {path.name for path in (directory / "hooks").iterdir()} - {"hooks.json", "langfuse_hook.py"}):
            raise ValueError("Claude tracing plugin must contain only its reviewed classic hook components")
        digest = hashlib.sha256()
        for relative in PLUGIN_FILES:
            path = directory / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError("Claude tracing plugin files must be regular files without symlinks")
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                data = stream.read(1_048_577)
                after = os.fstat(stream.fileno())
            if len(data) > 1_048_576 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Claude tracing plugin file is oversized or changed while inspected")
            digest.update(relative.encode() + b"\0" + data + b"\0")
            if relative == PLUGIN_FILES[0]:
                manifest = json.loads(data)
                if (not isinstance(manifest, dict) or manifest.get("name") != "langfuse-observability"
                        or manifest.get("version") != "1.2.1"
                        or set(manifest) - {"name", "version", "description", "author", "repository", "license", "homepage", "userConfig"}
                        or manifest.get("userConfig", {}).get("LANGFUSE_SECRET_KEY", {}).get("sensitive") is not True):
                    raise ValueError("Claude tracing requires the reviewed langfuse-observability 1.2.1 manifest and native sensitive secret option")
        if digest.hexdigest() != AUDITED_DIGEST:
            raise ValueError("Claude tracing plugin does not match the audited upstream source")
        return digest.hexdigest()
    except (OSError, ValueError, TypeError, AttributeError, RecursionError) as error:
        raise ValueError("Claude tracing plugin is unavailable, invalid or changed; inspect the selected source") from error


def configure(raw: str, task: dict, host_id: str, repository: Path) -> dict:
    try:
        if not isinstance(raw, str) or len(raw) > 16384:
            raise ValueError()
        config = _configuration(json.loads(raw))
        directory = Path(config["plugin_dir"]).resolve(strict=True)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise ValueError("Claude tracing configuration is invalid; expected explicit plugin_dir, public_key and base_url") from error
    repositories = {repository.resolve(), Path(task["invocation"]["parent_repo"]).resolve()}
    if any(directory.is_relative_to(root) for root in repositories):
        raise ValueError("Claude tracing plugin must be outside the delegated repository")
    correlation = {"session_id": task["child_session_id"], "router_task_id": task["task_id"], "host_id": host_id}
    if "work_item_id" in task:
        correlation["work_item_id"] = task["work_item_id"]
    tracing = {"requested": True, "plugin": PLUGIN_ID, "correlation": correlation}
    if "work_item_id" in correlation and len("work_item_id:" + correlation["work_item_id"]) > 200:
        tracing["omitted_tags"] = {"work_item_id": "exceeds_plugin_200_character_limit"}
    return {"_claude_tracing": {**config, "plugin_dir": str(directory), "schema_version": 1,
                                "digest": _inspect(directory)},
            "tracing": tracing}


def selected(task: dict) -> dict | None:
    if "_claude_tracing" not in task:
        if isinstance(task.get("tracing"), dict) and task["tracing"].get("requested"):
            raise ValueError("Claude tracing selection is missing; refusing an untraced replacement")
        return None
    config = task["_claude_tracing"]
    if (not isinstance(config, dict) or set(config) != CONFIG_KEYS | {"schema_version", "digest"}
            or type(config["schema_version"]) is not int or config["schema_version"] != 1):
        raise ValueError("Claude tracing selection is invalid; refusing an untraced replacement")
    _configuration({key: config[key] for key in CONFIG_KEYS})
    if _inspect(Path(config["plugin_dir"])) != config["digest"]:
        raise ValueError("Claude tracing plugin changed after selection; refusing an untraced replacement")
    return config


def options(task: dict, config: dict) -> dict:
    return {"pluginConfigs": {PLUGIN_ID: {"options": {
        "LANGFUSE_PUBLIC_KEY": config["public_key"], "LANGFUSE_BASE_URL": config["base_url"],
        "CC_LANGFUSE_STATE_DIR": str(Path(task["artifact_dir"]) / "claude-tracing" / "state"),
        "CC_LANGFUSE_TRACE_SEED": task["child_session_id"],
    }}}}


def _private_directories(base: Path) -> None:
    descriptor = os.open(base.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in base.parts[1:]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if os.fstat(descriptor).st_uid != os.getuid():
            raise ValueError("Claude tracing private directories must be owned by the Router operator")
        os.fchmod(descriptor, 0o700)
        for component in ("plugins", "state", "runtime-cache", "runtime-tmp"):
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                if os.fstat(child).st_uid != os.getuid():
                    raise ValueError("Claude tracing private directories must be owned by the Router operator")
                os.fchmod(child, 0o700)
            finally:
                os.close(child)
    except OSError as error:
        raise ValueError("Claude tracing private directories must be real directories without symlinks") from error
    finally:
        os.close(descriptor)


def environment(result: dict[str, str], task: dict) -> dict[str, str]:
    selected(task)  # Revalidate before setup as well as the actual native child.
    for key in tuple(result):
        if ((key.startswith("CLAUDE_") and key not in AUTH_KEYS)
                or key.startswith(("LANGFUSE_", "CC_LANGFUSE_", "UV_", "PYTHON", "_PYTHON", "PIP_", "CONDA_",
                                   "LD_", "DYLD_", "BASH_FUNC_"))
                or key in {"TRACE_TO_LANGFUSE", "BASH_ENV", "ENV", "ZDOTDIR", "NODE_OPTIONS", "BUN_OPTIONS",
                           "__PYVENV_LAUNCHER__", "SHELLOPTS", "BASHOPTS"}):
            result.pop(key, None)
    # Hooks resolve uv/python3 on this same PATH. A writable worktree interpreter
    # must not receive native hook-only credentials. Tools can name .venv/bin explicitly.
    roots = [Path(task["worktree"]).resolve()]
    roots += [Path(task["invocation"][name]).resolve() for name in ("parent_repo", "parent_worktree")]
    roots.append(Path(task["artifact_dir"]).resolve())
    paths = []
    for entry in result.get("PATH", os.defpath).split(os.pathsep):
        path = Path(entry)
        if path.is_absolute() and not any(path.resolve().is_relative_to(root) for root in roots):
            paths.append(entry)
    if not paths:
        raise ValueError("Claude tracing requires an operator runtime PATH outside delegated repositories")
    result["PATH"] = os.pathsep.join(paths)
    uv = shutil.which("uv", path=result["PATH"])
    if uv is None:
        raise ValueError("Claude tracing requires an existing operator-provisioned uv; no Python fallback or installation")
    python = Path(sys.executable).resolve(strict=True)
    if any(path.is_relative_to(root) for path in (Path(uv).resolve(), python) for root in roots):
        raise ValueError("Claude tracing hook interpreter must be outside delegated repositories")
    # The pinned shell hook selects uv whenever it exists on PATH. Bind its Python
    # to Router's supported running runtime, never the OS python3 fallback (3.9 on macOS).
    result.update(UV_PYTHON=str(python), UV_PYTHON_DOWNLOADS="never", UV_NO_CONFIG="1", UV_NO_ENV_FILE="1",
                  PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1")
    # Do not follow a child-created link when creating or securing private roots.
    base = Path(task["artifact_dir"]) / "claude-tracing"
    if any(base.resolve().is_relative_to(root) for root in roots[:-1]):
        raise ValueError("Claude tracing runtime/cache/tmp must be outside delegated repositories")
    _private_directories(base)
    plugins = base / "plugins"
    result["CLAUDE_CODE_PLUGIN_CACHE_DIR"] = str(plugins)
    result["UV_CACHE_DIR"] = str(base / "runtime-cache")
    result.update(dict.fromkeys(("TMPDIR", "TEMP", "TMP"), str(base / "runtime-tmp")))
    result["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    result["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    correlation = task["tracing"]["correlation"]
    tags = ["router_task_id:" + correlation["router_task_id"], "router_host_id:" + correlation["host_id"]]
    if "work_item_id" in correlation and len("work_item_id:" + correlation["work_item_id"]) <= 200:
        tags.append("work_item_id:" + correlation["work_item_id"])
    result["CC_LANGFUSE_TRACE_TAGS"] = json.dumps(tags)
    return result
