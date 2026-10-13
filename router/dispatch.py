"""The deliberately narrow shell dispatch contract."""

import json
import shlex

MUTATIONS = {"start", "review", "cancel", "close"}


def literal_payload(command: str, operation: str = "start") -> dict:
    argv = shlex.split(command)
    if (len(argv) != 4 or argv[:3] != ["router", operation, "--json"]
            or shlex.join(argv) != command):
        raise ValueError(f"Use router {operation} --json with canonical literal quoting")
    payload = json.loads(argv[3])
    if not isinstance(payload, dict):
        raise ValueError("Invocation must be a JSON object")  # noqa: TRY004 - public parser contract
    return payload


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
