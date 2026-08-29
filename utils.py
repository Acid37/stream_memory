"""Stream Memory 公共工具函数。

在 shameimaru_memory 的消息工具函数基础上，新增三级敏感标记常量
与跨群召回判定辅助函数。
"""
from __future__ import annotations

import time
from typing import Any

from src.core.models.message import Message

# ---------------------------------------------------------------------------
# 三级敏感标记常量
# ---------------------------------------------------------------------------

SENSITIVITY_NORMAL = "normal"
SENSITIVITY_SOFT_SCOPED = "soft_scoped"
SENSITIVITY_HARD_SCOPED = "hard_scoped"

ALL_SENSITIVITY_LEVELS = (
    SENSITIVITY_NORMAL,
    SENSITIVITY_SOFT_SCOPED,
    SENSITIVITY_HARD_SCOPED,
)


def should_recall_cross_group(sensitivity: str) -> bool:
    """判断给定敏感等级的记忆是否允许跨群召回。

    hard_scoped 记忆仅允许在来源群内召回，跨群时程序直接过滤；
    normal 与 soft_scoped 记忆跨群可见（soft_scoped 注入时附加警示前缀）。
    """
    return sensitivity != SENSITIVITY_HARD_SCOPED


def needs_warning_prefix(sensitivity: str) -> bool:
    """判断给定敏感等级的记忆在跨群召回时是否需要附加警示前缀。"""
    return sensitivity == SENSITIVITY_SOFT_SCOPED


# ---------------------------------------------------------------------------
# 消息工具函数
# ---------------------------------------------------------------------------


def message_time(message: Any) -> float:
    """获取消息时间戳（秒）。"""
    value = getattr(message, "time", None)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def person_id_of(message: Any) -> str:
    """从消息中提取人物 ID。

    人物 ID 格式为 ``platform:user_id``。若消息的 sender_id 已经是该格式
    则原样使用，否则用平台 + sender_id 拼接。

    Args:
        message: 运行时 Message 对象。

    Returns:
        str: 人物 ID；信息不足时返回空字符串。
    """
    platform = str(getattr(message, "platform", "") or "").strip()
    sender_id = str(getattr(message, "sender_id", "") or "").strip()
    if not platform or not sender_id:
        return ""
    prefix = f"{platform}:"
    if sender_id.startswith(prefix):
        return sender_id
    return f"{prefix}{sender_id}"


def person_name_of(message: Any) -> str:
    """从消息中提取人物展示名称。"""
    name = str(getattr(message, "sender_name", "") or "").strip()
    if name:
        return name
    sender_id = str(getattr(message, "sender_id", "") or "").strip()
    return sender_id


def format_local_time(timestamp: float) -> str:
    """将时间戳格式化为本地时间字符串（HH:MM）。"""
    try:
        return time.strftime("%H:%M", time.localtime(timestamp))
    except (OSError, ValueError, OverflowError):
        return ""


def is_group_message(message: Any) -> bool:
    """判断消息是否属于群聊。"""
    return str(getattr(message, "chat_type", "") or "") == "group"
