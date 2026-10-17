"""The deliberately narrow shell dispatch contract."""

import json
import shlex
from pathlib import PurePath

MUTATIONS = {"start", "review", "cancel", "close"}
# Shell text nested inside words (bash -c, eval, quoted substitutions) is re-parsed this deep.
MAX_NESTED_SHELL_DEPTH = 4


def _shell_words(command: str) -> list[str]:
    """POSIX shell words, with control operators and substitution delimiters split out."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&`")
    lexer.whitespace_split = True
    return list(lexer)


def mutation_operation(command: str, depth: int = 0) -> str | None:
    """Return the Router mutation a shell command would invoke anywhere, else None.

    Words are re-parsed as shell text so commands hidden in `bash -c '...'`, `eval`, or a
    quoted `$(...)` are still found. Text that does not parse cannot run as written.
    """
    try:
        words = _shell_words(command)
    except ValueError:
        return None
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else None
        if PurePath(word).name == "router" and following in MUTATIONS:
            return following
        if depth < MAX_NESTED_SHELL_DEPTH and word != command:
            nested = mutation_operation(word, depth + 1)
            if nested is not None:
                return nested
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
