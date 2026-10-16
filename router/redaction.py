"""Shared bounded redaction for operator-visible metadata and errors."""

import re


def redact_secrets(value: str, limit: int = 1000) -> str:
    value = re.sub(
        r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?(?:-----END [^-\r\n]*PRIVATE KEY-----|$)",
        "[REDACTED PRIVATE KEY]",
        value,
    )
    value = re.sub(
        r"(?im)\b(proxy-authorization|authorization)\s*[:=]\s*[^\r\n]+",
        r"\1: [REDACTED]",
        value,
    )
    value = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@", r"\1[REDACTED]@", value)
    value = re.sub(
        r"(?i)\b([a-z_][a-z0-9_]*(?:token|password|passwd|secret|api_key|apikey|access_key|private_key|credential)"
        r"[a-z0-9_]*)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1=[REDACTED]",
        value,
    )
    value = re.sub(
        r"(?i)\b(token|password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
        r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1=[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)bearer\s+[^\s]+", "Bearer [REDACTED]", value)
    value = re.sub(r"\b(?:sk|ghp|github_pat)-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
    return value.replace("\r", " ").replace("\n", " ")[:limit]
