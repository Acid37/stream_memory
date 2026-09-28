"""Stream Memory 公共工具函数。

在 shameimaru_memory 的消息工具函数基础上，新增三级敏感标记常量
与跨群召回判定辅助函数。
"""
from __future__ import annotations

import time
from datetime import datetime
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
    if isinstance(value, datetime):
        return value.timestamp()
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


# ---------------------------------------------------------------------------
# 长期记忆寿命（TTL）
# ---------------------------------------------------------------------------
# 判官给出 ttl_days 时以其为准；判官没给出（0）时，按下表回退到 memory_kind
# 的默认寿命。0 表示不过期。未经过判官的条目（judged_at 为 0）一律不过期，
# 避免插件升级后历史记忆被突然清空。

#: memory_kind 的默认寿命（天），仅作为 news 配置项缺失时的兜底。
DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "reject": 1,
    "transient": 3,
    "episode": 30,
    "commitment": 0,
    "stable_fact": 0,
}

#: memory_kind → news 配置节中对应的字段名。
RETENTION_FIELD_BY_KIND: dict[str, str] = {
    "reject": "retention_days_reject",
    "transient": "retention_days_transient",
    "episode": "retention_days_episode",
    "commitment": "retention_days_commitment",
    "stable_fact": "retention_days_stable_fact",
}

_DAY_SECONDS = 86400.0


def retention_enabled(news_cfg: Any) -> bool:
    """长期记忆过期淘汰是否启用（配置缺失时默认启用）。"""
    return bool(getattr(news_cfg, "retention_enabled", True))


def entry_ttl_days(entry: Any, news_cfg: Any) -> int:
    """计算新闻条目的有效寿命（天），0 表示不过期。

    优先级：判官写入的 ``ttl_days`` > ``memory_kind`` 对应的配置默认值。
    从未判官（``judged_at <= 0``）的条目返回 0，即不参与自动淘汰。
    """
    ttl = max(0, int(getattr(entry, "ttl_days", 0) or 0))
    if ttl > 0:
        return ttl
    if float(getattr(entry, "judged_at", 0.0) or 0.0) <= 0.0:
        return 0
    kind = str(getattr(entry, "memory_kind", "") or "episode")
    field_name = RETENTION_FIELD_BY_KIND.get(kind)
    if field_name is None:
        return 0
    fallback = DEFAULT_RETENTION_DAYS.get(kind, 0)
    return max(0, int(getattr(news_cfg, field_name, fallback) or 0))


def entry_age_seconds(entry: Any, now: float | None = None) -> float:
    """条目已存在时长（秒）。

    以落库时间（``consolidated_at``）为基准，缺失时回退事件时间，避免
    「事件发生很久之后才被整理成新闻」的条目一落库就立刻过期。
    """
    current = time.time() if now is None else float(now)
    base = float(getattr(entry, "consolidated_at", 0.0) or 0.0)
    if base <= 0.0:
        base = float(getattr(entry, "timestamp", 0.0) or 0.0)
    if base <= 0.0:
        return 0.0
    return max(0.0, current - base)


def is_entry_expired(entry: Any, news_cfg: Any, now: float | None = None) -> bool:
    """判断新闻条目是否已超过寿命。

    淘汰未启用、条目无寿命（0 天）或从未判官时恒为 False。
    """
    if not retention_enabled(news_cfg):
        return False
    ttl = entry_ttl_days(entry, news_cfg)
    if ttl <= 0:
        return False
    return entry_age_seconds(entry, now) > ttl * _DAY_SECONDS
