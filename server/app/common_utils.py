"""
Mini-Drop 公共工具函数。

提供跨模块共享的基础函数，避免在多个模块中重复定义相同逻辑。
"""

from __future__ import annotations

import os
import math
from typing import Any


def env_bool(name: str, default: bool = False) -> bool:
    """从环境变量读取布尔值。

    支持的值（大小写不敏感）：1, true, yes, on, enabled。
    未设置时返回 default。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def status_value(status: Any) -> str:
    """从状态对象中提取字符串值。

    兼容 Enum（有 .value 属性）和普通字符串两种形态。
    """
    return status.value if hasattr(status, "value") else str(status)


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
