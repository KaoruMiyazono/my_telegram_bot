from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(api[_-]?key|authorization|bearer|token|password|passwd|secret|cookie|session[_-]?id)(?:$|[_-])",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}")
_KEY_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"
)


def redact_sensitive(value: Any) -> Any:
    """Return a JSON-friendly copy with common credential shapes removed."""

    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED if _is_sensitive_key(str(key)) else redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        text = _BEARER_VALUE.sub(f"Bearer {REDACTED}", value)
        return _KEY_VALUE.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return bool(_SENSITIVE_KEY.search(normalized))
