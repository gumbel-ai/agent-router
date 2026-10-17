"""Shared bounded redaction for operator-visible metadata and errors."""

import re
import unicodedata

_ASSIGNMENT_HEADER = re.compile(
    r"(?i)(?<![a-z0-9_])(?P<key_quote>[\"']?)"
    r"(?P<key>[$a-z0-9][$a-z0-9_.-]{0,239})(?P=key_quote)"
    r"\s*(?::|=|\bis\b)\s*"
)
_SPACED_ASSIGNMENT_HEADER = re.compile(
    r"(?i)(?<![a-z0-9_])(?P<key_quote>[\"']?)"
    r"(?P<key>(?:[$a-z0-9_.-]{1,64}\s+){0,8}(?:"
    r"api\s+key|access\s+(?:key|token)|pass\s+(?:word|phrase)|"
    r"secret(?:\s+(?:access\s+key|key|token))?|private\s+key"
    r"))(?P=key_quote)\s*(?::|=|\bis\b)\s*"
)
_OPTION_HEADER = re.compile(
    r"(?i)(?<![a-z0-9_-])--?(?P<key>[a-z][a-z0-9_-]{0,63})(?:\s*=\s*|\s+)"
)
_VALUE_BOUNDARY = re.compile(
    r"(?i)[,;]|\s+(?=(?:"
    r"--?[a-z][a-z0-9_-]{0,63}(?:\s*=|\s+|\Z)|"
    r"[\"']?[$a-z0-9_.-]+[\"']?\s*(?:=|:|\bis\b)"
    r"))"
)
_SENSITIVE_COMPONENTS = frozenset({
    "token", "password", "passwd", "passphrase", "pwd", "credential", "secret",
})
_SENSITIVE_COMPOUNDS = frozenset({
    "apikey", "accesskey", "accesstoken", "secretaccesskey", "secretkey",
    "secrettoken", "privatekey",
})


def _credential_name(value: str) -> bool:
    # Insert word boundaries before normalizing so APIKey, clientSecret, and
    # other common camelCase spellings follow the same rules as API_KEY.
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    components = [item for item in re.sub(r"[^a-z0-9]+", "_", expanded.lower()).split("_") if item]
    if any(item in _SENSITIVE_COMPONENTS for item in components):
        return True
    if any("".join(components[start:end]) in _SENSITIVE_COMPOUNDS
           for start in range(len(components))
           for end in range(start + 1, min(len(components), start + 3) + 1)):
        return True
    compact = "".join(components)
    return any(compact.endswith(name) for name in _SENSITIVE_COMPOUNDS | _SENSITIVE_COMPONENTS)


def _value_end(value: str, start: int) -> int:
    if start >= len(value):
        return start
    quote = value[start]
    if quote in {"\"", "'"}:
        escaped = False
        for index in range(start + 1, len(value)):
            character = value[index]
            if character == quote and not escaped:
                return index + 1
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
        return len(value)
    boundary = _VALUE_BOUNDARY.search(value, start)
    return boundary.start() if boundary else len(value)


def _redact_credential_values(value: str) -> str:
    spans = []
    for pattern in (_ASSIGNMENT_HEADER, _SPACED_ASSIGNMENT_HEADER, _OPTION_HEADER):
        for match in pattern.finditer(value):
            if _credential_name(match.group("key")):
                spans.append((match.end(), _value_end(value, match.end())))
    if not spans:
        return value
    merged = []
    for start, end in sorted(set(spans)):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        value = value[:start] + "[REDACTED]" + value[end:]
    return value


def sanitize_output_text(value: str) -> str:
    """Return single-line, UTF-8-encodable text safe for terminal output."""
    result = []
    for character in value:
        category = unicodedata.category(character)
        if character in "\r\n" or category in {"Zl", "Zp"}:
            result.append(" ")
        elif category not in {"Cc", "Cf", "Cs"}:
            result.append(character)
    return "".join(result)


def redact_secrets(value: str, limit: int = 1000) -> str:
    # Normalize controls first so they cannot split a credential marker until
    # after the final redaction pass (for example, ``pass<ZWSP>word``).
    value = sanitize_output_text(value)
    lowered = value.lower()
    if "private key" in lowered:
        value = re.sub(
            r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?(?:-----END [^-\r\n]*PRIVATE KEY-----|$)",
            "[REDACTED PRIVATE KEY]",
            value,
        )
    if ":" in value or "=" in value:
        value = re.sub(
            r"(?im)\b(proxy-authorization|authorization|set-cookie|cookie)\s*[:=]\s*[^\r\n]+",
            r"\1: [REDACTED]",
            value,
        )
    if "://" in value:
        value = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@", r"\1[REDACTED]@", value)
    if "=" in value or ":" in value or "-" in value or re.search(r"(?i)\bis\b", value):
        value = _redact_credential_values(value)
    if "bearer" in lowered:
        value = re.sub(r"(?i)bearer\s+[^\s]+", "Bearer [REDACTED]", value)
    if any(marker in value for marker in (
            "sk-", "ghp_", "github_pat_", "glpat-", "xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-",
            "AKIA", "ASIA", "A3T", "AGPA", "AIDA", "AIPA", "ANPA", "ANVA", "APKA", "AROA")):
        value = re.sub(
            r"(?:"
            r"sk-[A-Za-z0-9_-]{12,}|(?:ghp|github_pat)_[A-Za-z0-9_]{12,}|"
            r"glpat-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
            r"(?:AKIA|ASIA|A3T[A-Z0-9]|AGPA|AIDA|AIPA|ANPA|ANVA|APKA|AROA)[A-Z0-9]{16}"
            r")(?![A-Za-z0-9])",
            "[REDACTED]",
            value,
        )
    return value[:limit]


def redact_structure(value):
    """Redact every string value and mapping key at an output boundary."""
    if isinstance(value, str):
        return redact_secrets(value, len(value) + 16)
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    if isinstance(value, dict):
        return {
            redact_secrets(key, len(key) + 16) if isinstance(key, str) else key: redact_structure(item)
            for key, item in value.items()
        }
    return value
