"""项目通用数据结构小工具。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    """返回 UTC ISO 时间，统一用于 job、review、alert 的时间戳。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def field(value: Any = None, confidence: float = 0.0, source: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造标准字段包装对象。

    系统内部所有可审核字段都使用 `{value, confidence, source}`，方便前端展示来源和置信度。
    """
    return {
        "value": value,
        "confidence": round(float(confidence), 3),
        "source": source or {},
    }


def issue(code: str, severity: str, message: str, path: str = "", actual: Any = None) -> dict[str, Any]:
    """构造统一校验/告警问题对象。"""
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "path": path,
        "actual": actual,
    }
