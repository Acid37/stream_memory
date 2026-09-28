"""Stream Memory 数据模型。

定义摘要条目、新闻条目、人物引用、群聊摘要记录的结构，
并提供与 JSON 持久化格式互转的能力。

新增于 shameimaru_memory：
- NewsEntry 增加 sensitivity（三级敏感标记）与 origin_stream_id
  （记忆来源群聊 ID）两个字段，用于敏感记忆跨群过滤。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .utils import SENSITIVITY_NORMAL


@dataclass(slots=True)
class PersonRef:
    """人物引用：人物 ID + 展示名称。"""

    person_id: str
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {"person_id": self.person_id, "name": self.name}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PersonRef | None":
        """从字典构造。"""
        if not isinstance(data, dict):
            return None
        person_id = str(data.get("person_id") or "").strip()
        if not person_id:
            return None
        return cls(person_id=person_id, name=str(data.get("name") or ""))


def participants_from(data: Any) -> list[PersonRef]:
    """从任意输入中规范化出人物引用列表。"""
    refs: list[PersonRef] = []
    seen: set[str] = set()
    if isinstance(data, list):
        for item in data:
            ref = PersonRef.from_dict(item) if isinstance(item, dict) else None
            if ref is None:
                continue
            if ref.person_id in seen:
                continue
            seen.add(ref.person_id)
            refs.append(ref)
    return refs


@dataclass(slots=True)
class SummaryEntry:
    """摘要条目。

    格式要求：时间戳 + 摘要内容 + 参与人物及其 ID。
    ``deprecated``：新闻层消费后标记为 True（废弃），新闻层不会再读取废弃条目。
    """

    timestamp: float
    content: str
    participants: list[PersonRef] = field(default_factory=list)
    deprecated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "timestamp": self.timestamp,
            "content": self.content,
            "participants": [ref.to_dict() for ref in self.participants],
            "deprecated": self.deprecated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SummaryEntry | None":
        """从字典构造。"""
        if not isinstance(data, dict):
            return None
        content = str(data.get("content") or "").strip()
        if not content:
            return None
        return cls(
            timestamp=float(data.get("timestamp") or 0.0),
            content=content,
            participants=participants_from(data.get("participants")),
            deprecated=bool(data.get("deprecated") or False),
        )


@dataclass(slots=True)
class NewsEntry:
    """新闻条目。

    格式要求：时间戳 + 记忆标题（一句话总结）+ 记忆条目内容（200 字左右）
    + 参与人物及其 ID。

    新增字段：
    - ``sensitivity``：敏感等级（normal / soft_scoped / hard_scoped）。
      巩固时由 LLM 判断写入，召回时由程序按标记执行过滤或带约束放行。
    - ``origin_stream_id``：记忆来源群聊 ID。用于敏感记忆跨群过滤——
      hard_scoped 仅在 origin_stream_id 与当前群匹配时召回。

    长期记忆生命周期字段（由晋升判官写入，见 ``job._judge_persona_promotion``）：
    - ``memory_kind``：reject / transient / episode / commitment / stable_fact；
    - ``persona_eligible``：是否允许写入人物画像（默认 False，fail-closed）；
    - ``importance`` / ``confidence``：判官给出的重要性与置信度（0～1）；
    - ``ttl_days``：判官给出的寿命（天），0 表示未指定，按 memory_kind 默认值；
    - ``scope``：global / stream / roleplay；
    - ``promotion_reason``：判官给出的理由，供后台审计；
    - ``judged_at``：判官裁决时间戳，0 表示从未判官（此类条目不会被 TTL 淘汰）。
    """

    id: str
    timestamp: float
    title: str
    content: str
    participants: list[PersonRef] = field(default_factory=list)
    sensitivity: str = SENSITIVITY_NORMAL
    origin_stream_id: str = ""
    consolidated_at: float = 0.0
    event_time_known: bool = True
    memory_kind: str = "episode"
    persona_eligible: bool = False
    importance: float = 0.0
    confidence: float = 0.0
    ttl_days: int = 0
    scope: str = "stream"
    promotion_reason: str = ""
    judged_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "title": self.title,
            "content": self.content,
            "participants": [ref.to_dict() for ref in self.participants],
            "sensitivity": self.sensitivity,
            "origin_stream_id": self.origin_stream_id,
            "consolidated_at": self.consolidated_at,
            "event_time_known": self.event_time_known,
            "memory_kind": self.memory_kind,
            "persona_eligible": self.persona_eligible,
            "importance": self.importance,
            "confidence": self.confidence,
            "ttl_days": self.ttl_days,
            "scope": self.scope,
            "promotion_reason": self.promotion_reason,
            "judged_at": self.judged_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "NewsEntry | None":
        """从字典构造。"""
        if not isinstance(data, dict):
            return None
        entry_id = str(data.get("id") or "").strip()
        if not entry_id:
            return None
        has_consolidated_at = "consolidated_at" in data
        timestamp = float(data.get("timestamp") or 0.0)
        return cls(
            id=entry_id,
            timestamp=timestamp,
            title=str(data.get("title") or "").strip(),
            content=str(data.get("content") or "").strip(),
            participants=participants_from(data.get("participants")),
            sensitivity=str(data.get("sensitivity") or SENSITIVITY_NORMAL),
            origin_stream_id=str(data.get("origin_stream_id") or ""),
            consolidated_at=float(data.get("consolidated_at") or timestamp),
            event_time_known=bool(data.get("event_time_known", has_consolidated_at)),
            memory_kind=str(data.get("memory_kind") or "episode"),
            persona_eligible=bool(data.get("persona_eligible", False)),
            importance=float(data.get("importance") or 0.0),
            confidence=float(data.get("confidence") or 0.0),
            ttl_days=max(0, int(data.get("ttl_days") or 0)),
            scope=str(data.get("scope") or "stream"),
            promotion_reason=str(data.get("promotion_reason") or ""),
            judged_at=float(data.get("judged_at") or 0.0),
        )


@dataclass(slots=True)
class GroupSummary:
    """单个聊天流的摘要记录（群聊或私聊通用）。

    ``chat_type`` 标记流的类别：``"group"`` 为群聊，``"private"`` 为私聊。
    Bot 合一：私聊流同样产生摘要与新闻，新闻与人物画像与群聊共用
    同一记忆域（仅用于区分展示/统计，不做物理隔离）。
    """

    stream_id: str
    platform: str = ""
    group_id: str = ""
    group_name: str = ""
    chat_type: str = ""
    entries: list[SummaryEntry] = field(default_factory=list)
    last_message_id: str = ""
    last_message_timestamp: float = 0.0
    cursor_initialized: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "stream_id": self.stream_id,
            "platform": self.platform,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "chat_type": self.chat_type,
            "entries": [entry.to_dict() for entry in self.entries],
            "last_message_id": self.last_message_id,
            "last_message_timestamp": self.last_message_timestamp,
            "cursor_initialized": self.cursor_initialized,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GroupSummary | None":
        """从字典构造。"""
        if not isinstance(data, dict):
            return None
        stream_id = str(data.get("stream_id") or "").strip()
        if not stream_id:
            return None
        entries: list[SummaryEntry] = []
        for item in data.get("entries") or []:
            entry = SummaryEntry.from_dict(item)
            if entry is not None:
                entries.append(entry)
        return cls(
            stream_id=stream_id,
            platform=str(data.get("platform") or ""),
            group_id=str(data.get("group_id") or ""),
            group_name=str(data.get("group_name") or ""),
            chat_type=str(data.get("chat_type") or ""),
            entries=entries,
            last_message_id=str(data.get("last_message_id") or ""),
            last_message_timestamp=float(data.get("last_message_timestamp") or 0.0),
            cursor_initialized=bool(data.get("cursor_initialized") or False),
        )
