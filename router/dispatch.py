"""The deliberately narrow shell dispatch contract."""

import itertools
import json
import shlex

MUTATIONS = {"start", "review", "cancel", "close"}


def mutation_operation(command: str) -> str | None:
    """Return the Router mutation a command's text could invoke, else None.

    Deliberately conservative: any `router` word directly followed by a mutation word,
    whatever shell syntax surrounds them (quotes, substitutions, eval, heredocs, expansions),
    is reported so the hook sends the command through the canonical check. Parsing shell
    to find only executed words cannot be done safely without a shell.
    """
    if not isinstance(command, str):
        raise TypeError("Shell command must be text")
    words = "".join(character if character.isalnum() or character == "_" else " "
                    for character in command).split()
    for word, following in itertools.pairwise(words):
        if word == "router" and (following in MUTATIONS or following == "remote"):
            return following
    return None


def literal_payload(command: str, operation: str = "start") -> dict:
    argv = shlex.split(command)
    if (len(argv) != 4 or argv[:3] != ["router", operation, "--json"]
            or shlex.join(argv) != command):
        raise ValueError(f"Use a standalone router {operation} --json command with canonical literal quoting; "
                         "no pipelines or substitutions")
    payload = json.loads(argv[3])
    if not isinstance(payload, dict):
        raise ValueError("Invocation must be a JSON object")  # noqa: TRY004 - public parser contract
    return payload


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
