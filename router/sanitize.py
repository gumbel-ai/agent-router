"""Output safety for operator-visible text: no content rewriting, no pattern matching.

Text is passed through as written. It is only made safe to store and display:
terminal control sequences and control characters are removed, line breaks become
spaces, and length is bounded. Router state is private to the operator (0700/0600),
like the host transcripts the text comes from.
"""

import unicodedata

_ESC, _BEL = "\x1b", "\x07"
_STRING_INTRODUCERS = {"]", "P", "X", "^", "_"}  # OSC, DCS, SOS, PM, APC
# 8-bit C1 controls are not interpreted by UTF-8 terminals; each is dropped as a
# single control character, so a stray one can never consume following text.


def _skip_control_sequence(value: str, index: int) -> int:
    """Return the index after a CSI body (parameters, intermediates, final byte).

    C0 controls embedded in the body are executed by terminals without ending the
    sequence, so they are skipped as part of it.
    """
    while index < len(value) and ("\x30" <= value[index] <= "\x3f"
                                  or (value[index] < "\x20" and value[index] != _ESC)):
        index += 1
    while index < len(value) and "\x20" <= value[index] <= "\x2f":
        index += 1
    if index < len(value) and "\x40" <= value[index] <= "\x7e":
        index += 1
    return index


def _skip_control_string(value: str, index: int) -> int:
    """Return the index after a control string ended by BEL or ST.

    Any other ESC ends the string without being consumed, as in terminals, so an
    unterminated string cannot swallow text after a later sequence.
    """
    while index < len(value):
        if value[index] == _BEL:
            return index + 1
        if value[index] == _ESC:
            return index + 2 if value[index + 1:index + 2] == "\\" else index
        index += 1
    return index


def strip_terminal_sequences(value: str) -> str:
    """Remove whole 7-bit ECMA-48 escape sequences, not just their ESC byte.

    Dropping only ESC would leave fragments such as "[31m" or "(B" in the text.
    """
    if _ESC not in value:
        return value
    result = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != _ESC:
            result.append(character)
            index += 1
            continue
        introducer = value[index + 1:index + 2]
        if introducer == "[":
            index = _skip_control_sequence(value, index + 2)
        elif introducer in _STRING_INTRODUCERS:
            index = _skip_control_string(value, index + 2)
        elif "\x20" <= introducer <= "\x2f":
            # nF escape: intermediates, then one final byte (for example ESC ( B).
            index += 1
            while index < len(value) and "\x20" <= value[index] <= "\x2f":
                index += 1
            if index < len(value) and "\x30" <= value[index] <= "\x7e":
                index += 1
        elif "\x30" <= introducer <= "\x7e":
            index += 2  # Fp, Fe, and Fs escapes (for example ESC 7, ESC =, ESC c).
        else:
            index += 1  # A lone ESC; the following text is kept.
    return "".join(result)


def sanitize_output_text(value: str) -> str:
    """Return single-line, UTF-8-encodable text safe for terminal output."""
    result = []
    for character in strip_terminal_sequences(value):
        category = unicodedata.category(character)
        if character in "\r\n" or category in {"Zl", "Zp"}:
            result.append(" ")
        elif category not in {"Cc", "Cf", "Cs"}:
            result.append(character)
    return "".join(result)


def bound_text(value: str, limit: int = 1000) -> str:
    return sanitize_output_text(value)[:limit]


def sanitize_structure(value):
    """Apply output safety to every string value and mapping key."""
    if isinstance(value, str):
        return sanitize_output_text(value)
    if isinstance(value, list):
        return [sanitize_structure(item) for item in value]
    if isinstance(value, dict):
        return {
            sanitize_output_text(key) if isinstance(key, str) else key: sanitize_structure(item)
            for key, item in value.items()
        }
    return value
