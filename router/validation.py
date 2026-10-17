"""Shared bounds used by monitoring producers and wire validators."""

MAX_WIRE_INTEGER = 2**63 - 1
MAX_HOST_ID_LENGTH = 128
MAX_HOST_DISPLAY_LENGTH = 240
MAX_ROUTER_VERSION_LENGTH = 120

HOST_STRING_LIMITS = {
    "host_id": MAX_HOST_ID_LENGTH,
    "hostname": 255,
    "boot_id": MAX_HOST_DISPLAY_LENGTH,
    "platform": MAX_HOST_DISPLAY_LENGTH,
    "architecture": MAX_HOST_DISPLAY_LENGTH,
    "router_version": MAX_ROUTER_VERSION_LENGTH,
    "claude_version": MAX_HOST_DISPLAY_LENGTH,
}


def is_wire_integer(value: object, *, minimum: int = 0) -> bool:
    """Return whether value is a non-boolean integer in the wire-safe range."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= MAX_WIRE_INTEGER
    )
