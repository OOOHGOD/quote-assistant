from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def field(value: Any = None, confidence: float = 0.0, source: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "value": value,
        "confidence": round(float(confidence), 3),
        "source": source or {},
    }


def issue(code: str, severity: str, message: str, path: str = "", actual: Any = None) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "path": path,
        "actual": actual,
    }

