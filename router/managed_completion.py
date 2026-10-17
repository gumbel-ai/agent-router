"""Shared schema for durable managed-command completion evidence."""

import math

MAX_COMPLETION_BYTES = 16 * 1024
_FIELDS = frozenset({
    "exit_code", "exit_status", "signal", "finished_at", "peak_rss_bytes", "cpu_seconds",
})


def completion_payload(
    exit_code: int, *, finished_at: int, peak_rss_bytes: int, cpu_seconds: float,
) -> dict:
    """Build the single completion document accepted by all consumers."""
    return validate_completion({
        "exit_code": exit_code,
        "exit_status": exit_code if exit_code >= 0 else None,
        "signal": -exit_code if exit_code < 0 else None,
        "finished_at": finished_at,
        "peak_rss_bytes": peak_rss_bytes,
        "cpu_seconds": cpu_seconds,
    })


def validate_completion(
    payload: object, *, started_at: int | None = None, current_time: int | None = None,
) -> dict:
    """Validate and normalize evidence written by ``managed_gate``."""
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("Invalid managed process completion fields")
    exit_code = payload["exit_code"]
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ValueError(  # noqa: TRY004 - persisted document validation
            "Invalid managed process exit code"
        )
    expected_status = exit_code if exit_code >= 0 else None
    expected_signal = -exit_code if exit_code < 0 else None
    if payload["exit_status"] != expected_status or payload["signal"] != expected_signal:
        raise ValueError("Managed process completion status is inconsistent")
    finished_at = payload["finished_at"]
    if not isinstance(finished_at, int) or isinstance(finished_at, bool) or finished_at < 0:
        raise ValueError("Invalid managed process completion timestamp")
    if started_at is not None and finished_at < started_at:
        raise ValueError("Completion predates managed process start")
    if current_time is not None and finished_at > current_time + 60_000:
        raise ValueError("Completion timestamp is implausibly far in the future")
    peak_rss = payload["peak_rss_bytes"]
    if not isinstance(peak_rss, int) or isinstance(peak_rss, bool) or peak_rss < 0:
        raise ValueError("Invalid managed process peak RSS")
    cpu_seconds = payload["cpu_seconds"]
    if (not isinstance(cpu_seconds, (int, float)) or isinstance(cpu_seconds, bool)
            or not math.isfinite(cpu_seconds) or cpu_seconds < 0):
        raise ValueError("Invalid managed process CPU time")
    return {
        "exit_code": exit_code,
        "exit_status": expected_status,
        "signal": expected_signal,
        "finished_at": finished_at,
        "peak_rss_bytes": peak_rss,
        "cpu_seconds": float(cpu_seconds),
    }
