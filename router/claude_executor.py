"""Explicit Claude CLI permissions and cumulative structured result accounting."""

import math
import os
import shutil
import uuid
from pathlib import Path

from router.native import identifier
from router.validation import MAX_WIRE_INTEGER

TOOLS = frozenset({"Read", "Edit", "Write", "Bash", "Glob", "Grep", "NotebookEdit"})
TOKEN_FIELDS = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
                "cacheReadInputTokens": "cache_read_input_tokens", "cacheCreationInputTokens": "cache_creation_input_tokens"}


def validate(model: str, permission: object) -> dict:
    parts = model.removeprefix("anthropic/").split("-")
    if (not model.startswith("anthropic/") or len(parts) not in {4, 5} or parts[0] != "claude"
            or not parts[1].isascii() or not parts[1].isalpha()
            or any(not part.isascii() or not part.isdecimal() for part in parts[2:])):
        raise ValueError("Claude requires an exact anthropic/claude-family-version model ID; no alias or fallback")
    if (not isinstance(permission, dict) or set(permission) != {"tools", "allow"}
            or not isinstance(permission["tools"], list) or not permission["tools"]
            or any(not isinstance(tool, str) or tool not in TOOLS for tool in permission["tools"])
            or not isinstance(permission["allow"], list)
            or any(not isinstance(rule, str) or not rule.strip() or len(rule) > 1000
                   or any(ord(c) < 32 for c in rule) for rule in permission["allow"])):
        raise ValueError("Claude permissions require explicit tools and allow lists; nested agents and MCP tools are unsupported")
    return permission


def validate_command(command: list[str] | None) -> None:
    if command is None or len(command) != 1:
        raise ValueError("Claude requires one executable path without configured arguments; use an operator forwarding wrapper")


def resolve_executable(command: list[str], environment: dict[str, str], cwd: Path) -> str:
    validate_command(command)
    search = os.pathsep.join(str(cwd / entry) if not Path(entry).is_absolute() else entry
                             for entry in os.get_exec_path(environment))
    selected = str(cwd / command[0]) if os.path.dirname(command[0]) else command[0]
    located = shutil.which(selected, path=search)
    if located is None:
        raise ValueError("Configured Claude executable is unavailable; no fallback")
    try:
        return str(Path(located).resolve(strict=True))
    except (OSError, RuntimeError) as error:
        raise ValueError("Cannot resolve configured Claude executable; no fallback") from error


def model_counters(value: object, fields: dict[str, str]) -> dict | None:
    if not isinstance(value, dict) or len(value) > 32:
        return None
    result = {}
    totals = dict.fromkeys(fields.values(), 0)
    for model, usage in value.items():
        if not isinstance(model, str) or len(model) > 240:
            return None
        try:
            identifier(model, "Claude usage model")
        except (ValueError, TypeError):
            return None
        if not isinstance(usage, dict):
            return None
        counts = {}
        for source, target in fields.items():
            count = usage.get(source)
            if type(count) is not int or not 0 <= count <= MAX_WIRE_INTEGER - totals[target]:
                return None
            totals[target] += count
            counts[target] = count
        result[model] = counts
    return result


def public_usage(frame: dict) -> dict:
    usage = frame.get("usage")
    valid = (isinstance(usage, dict) and set(usage) == set(TOKEN_FIELDS.values())
             and all(type(value) is int and 0 <= value <= MAX_WIRE_INTEGER for value in usage.values()))
    return {"usage": dict(usage) if valid else {},
            "usage_complete": valid and frame.get("usage_complete") is True}


def validate_persistence(environment: dict[str, str]) -> None:
    # Native CLI treats any nonempty value, including "0", as an explicit opt-out.
    if environment.get("CLAUDE_CODE_SKIP_PROMPT_HISTORY"):
        raise ValueError("Claude resumable children require session persistence; CLAUDE_CODE_SKIP_PROMPT_HISTORY is selected")


def version(output: Path, exit_code: int) -> str:
    try:
        with output.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("Claude CLI version output exceeds 4096 bytes; no child launched")
        text = raw.decode("ascii")
    except (OSError, UnicodeError) as error:
        raise ValueError("Cannot inspect Claude CLI version before launch") from error
    if exit_code:
        raise ValueError(f"Claude CLI version command exited {exit_code}; no child launched")
    fields = text.split()
    parts = fields[0].split(".") if fields else []
    if len(parts) != 3 or any(not part.isascii() or not part.isdecimal() for part in parts):
        raise ValueError("Claude CLI returned an invalid version; no child launched")
    if tuple(map(int, parts)) < (2, 1, 303):
        raise ValueError("Claude children require CLI 2.1.303 or newer for the verified cumulative resume contract")
    return fields[0]


def collect(task: dict, event: dict) -> None:
    kind = event.get("type")
    if not isinstance(kind, str):
        task["execution_error"] = True
        return
    if kind not in {"system", "assistant", "result"}:
        return
    if kind == "system" and event.get("subtype") != "init":
        return
    try:
        session = str(uuid.UUID(event.get("session_id", "")))
    except (ValueError, AttributeError, TypeError):
        task["execution_error"] = True
        return
    if session != task.get("child_session_id"):
        task["execution_error"] = True
        return
    model = task["resolved_model"].removeprefix("anthropic/")
    if kind == "system":
        if event.get("model") != model:
            task["execution_error"] = True
        else:
            task["thread_observed"] = True
    elif kind == "assistant":
        message = event.get("message")
        if not isinstance(message, dict) or message.get("model") != model:
            task["execution_error"] = True
    else:
        task["executor_completed"] = True
        if event.get("subtype") != "success" or event.get("is_error") is not False:
            task["execution_error"] = True
        if isinstance(event.get("result"), str):
            task["summary"] = event["result"][-8000:]
        reported_models = event.get("modelUsage")
        if isinstance(reported_models, dict) and reported_models and model not in reported_models and event.get("subtype") == "success":
            task["execution_error"] = True
        models = model_counters(reported_models, TOKEN_FIELDS)
        complete = models is not None and bool(models) and event.get("subtype") != "error_during_execution"
        baseline = task.get("claude_baseline", {"schema_version": 1, "models": {}, "complete": True})
        if not isinstance(baseline, dict):
            baseline = {"complete": False, "cost_complete": False}
        previous = (model_counters(baseline.get("models"), {key: key for key in TOKEN_FIELDS.values()})
                    if type(baseline.get("schema_version")) is int and baseline["schema_version"] == 1 else None)
        complete = complete and previous is not None and baseline.get("complete") is True
        deltas = dict.fromkeys(TOKEN_FIELDS.values(), 0)
        if complete:
            complete = previous.keys() <= models.keys()
            for observed, counts in models.items():
                for key, value in counts.items():
                    change = value - previous.get(observed, {}).get(key, 0)
                    complete = complete and change >= 0
                    deltas[key] += change
        task["usage_complete"] = complete
        task["usage"] = deltas if complete else {}
        cost = event.get("total_cost_usd")
        previous_cost = baseline.get("cost", 0)
        valid_cost = type(previous_cost) in {int, float} and 0 <= previous_cost <= 1_000_000_000_000
        valid_cost = valid_cost and type(cost) in {int, float} and previous_cost <= cost <= 1_000_000_000_000 and math.isfinite(cost)
        valid_cost = valid_cost and baseline.get("cost_complete", True) is True and event.get("subtype") != "error_during_execution"
        task["claude_total"] = {"schema_version": 1, "models": models or {}, "complete": complete, "cost_complete": valid_cost}
        task["cost_note"] = "Claude CLI session-total estimate minus prior attempt; not billed spend"
        if valid_cost:
            task["claude_total"]["cost"] = cost
            task["cost"] = cost - previous_cost
            task["cost_source"] = "executor_reported"
        else:
            task.pop("cost", None)
            task["cost_note"] = "Claude CLI cost missing, invalid, decreased, or unavailable after an incomplete attempt"
