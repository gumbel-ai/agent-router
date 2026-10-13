"""Concrete executor invocations and a release gate for owned phases."""

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from router.config import Settings
from router.process import creation_identity


def launch(command: list[str], cwd: Path, environment: dict[str, str], directory: Path,
           register: Callable[[dict], None]) -> tuple[subprocess.Popen, dict]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    gate_read, gate_write = os.pipe()
    token = uuid.uuid4().hex
    completion = directory / f"{token}.completion.json"
    stdout = directory / f"{token}.stdout"
    stderr = directory / f"{token}.stderr"
    # Use absolute script paths; a child worktree need not contain this package.
    wrapper = str(Path(__file__).with_name("process_gate.py"))
    process = None
    try:
        with stdout.open("wb") as out, stderr.open("wb") as err:
            process = subprocess.Popen([sys.executable, wrapper, str(gate_read), str(completion), *command],
                                       cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=err, pass_fds=(gate_read,), start_new_session=True)
        record = {"pid": process.pid, "pgid": process.pid,
                  "creation_identity": creation_identity(process.pid), "completion": str(completion),
                  "stdout": str(stdout), "stderr": str(stderr)}
        register(record)  # This must commit before the gate is released.
        os.write(gate_write, b"1")
        return process, record
    except BaseException:
        os.close(gate_write)
        gate_write = -1
        if process is not None:
            # EOF at the gate prevents the actual command from starting.
            process.wait(timeout=5)
        raise
    finally:
        os.close(gate_read)
        if gate_write != -1:
            os.close(gate_write)


def executor_command(settings: Settings, task: dict) -> list[str]:
    invocation = task["invocation"]
    prompt = invocation["task"]
    context = invocation.get("context", {})
    if context:
        prompt += "\n\nParent-provided context:\n" + json.dumps(context)
    if task.get("feedback"):
        prompt = "Continue this task in the same session. Parent feedback:\n" + task["feedback"]
    if task["executor"] == "opencode":
        return [*settings.opencode_command, "run", "--standalone", "--auto", "--session", task["child_session_id"],
                "--model", task["resolved_model"], "--format", "json", "--", prompt]
    session = task.get("child_session_id")
    if task["attempts"] and not session:
        raise ValueError("Codex session ID unavailable; cannot resume or silently replace this child")
    return [*settings.codex_command, "exec", *(["resume", session] if session else []),
            "--json", "--ignore-user-config", "--ignore-rules", "--disable", "multi_agent", "--disable", "multi_agent_v2",
            "-c", 'approval_policy="never"', "-c", f'sandbox_mode="{task["permission_profile"]["sandbox"]}"',
            "-c", 'model_provider="openai"', "-c", 'service_tier="default"',
            "--model", task["resolved_model"].removeprefix("openai/"), "--", prompt]


def child_environment(environment: dict[str, str], task: dict) -> dict[str, str]:
    result = environment.copy()
    worktree = Path(task["worktree"])
    result.pop("VIRTUAL_ENV", None)
    # Never inherit the parent's writable Python/Node module lookup paths.
    result.pop("PYTHONPATH", None)
    result.pop("NODE_PATH", None)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        result.pop(key, None)
    result["PATH"] = str(worktree / ".venv" / "bin") + os.pathsep + environment.get("PATH", os.defpath)
    if task["executor"] == "opencode":
        result["OPENCODE_CONFIG_CONTENT"] = json.dumps({"permission": task["permission_profile"]})
    else:
        for key in ("OPENCODE_CONFIG_CONTENT", "OPENAI_BASE_URL", "CODEX_API_KEY", "OPENAI_API_KEY"):
            result.pop(key, None)
    return result
