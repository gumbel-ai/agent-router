"""Explicit Claude CLI permissions and cumulative structured result accounting."""

import math
import uuid
from pathlib import Path

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


def validate_command(command: list[str]) -> None:
    if len(command) != 1:
        raise ValueError("Claude requires one executable path without configured arguments; use an operator forwarding wrapper")


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
        raw = event.get("modelUsage")
        complete = isinstance(raw, dict) and bool(raw) and len(raw) <= 32 and event.get("subtype") != "error_during_execution"
        if complete and model not in raw and event.get("subtype") == "success":
            task["execution_error"] = True
        totals = dict.fromkeys(TOKEN_FIELDS.values(), 0)
        if complete:
            for usage in raw.values():
                for source, target in TOKEN_FIELDS.items():
                    value = usage.get(source) if isinstance(usage, dict) else None
                    if type(value) is not int or not 0 <= value <= 2**63 - 1:
                        complete = False
                    else:
                        totals[target] += value
        baseline = task.get("claude_baseline", {})
        previous = baseline.get("usage", {})
        deltas = {key: value - previous.get(key, 0) for key, value in totals.items()}
        complete = complete and all(value >= 0 for value in deltas.values()) and baseline.get("complete", True)
        task["usage_complete"] = complete
        task["usage"] = deltas if complete else {}
        cost = event.get("total_cost_usd")
        valid_cost = type(cost) in {int, float} and 0 <= cost <= 1_000_000_000_000 and math.isfinite(cost) and cost >= baseline.get("cost", 0)
        valid_cost = valid_cost and baseline.get("cost_complete", True) and event.get("subtype") != "error_during_execution"
        task["claude_total"] = {"usage": totals, "complete": complete, "cost_complete": valid_cost}
        task["cost_note"] = "Claude CLI session-total estimate minus prior attempt; not billed spend"
        if valid_cost:
            task["claude_total"]["cost"] = cost
            task["cost"] = cost - baseline.get("cost", 0)
            task["cost_source"] = "executor_reported"
        else:
            task.pop("cost", None)
            task["cost_note"] = "Claude CLI cost missing, invalid, decreased, or unavailable after an incomplete attempt"
