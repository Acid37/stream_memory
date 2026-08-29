"""Stream Memory 回复前召回注入器。

订阅 ``on_message_received``，在消息入站时根据当前聊天流中出现的人物，
按人物匹配 + 三级敏感分级过滤召回记忆，将涉及的相关新闻与人物背景
写入流私有 system reminder（insert_type=dynamic，consume=forever），
由 LLMContextManager 在每次请求发送前动态注入，而不是直接修改 prompt。

注入位置与去重语义：
- ``dynamic`` reminder 会被注入到最后一条 user 消息（对话尾部）；
- 每次请求前，旧 reminder 文本会先从所有 user 消息中剥离，再注入新文本，
  因此全上下文中同一 reminder 始终只出现一次，不会重复累积。

敏感分级召回：使用 ``store.get_recallable_news`` 按三级敏感标记过滤跨群可见性，
hard_scoped 物理阻断，soft_scoped 跨群带警示前缀放行。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api import log_api, prompt_api, stream_api
from src.app.plugin_system.base import BaseEventHandler
from src.core.components.types import EventType
from src.core.prompt import SystemReminderConsumeType, SystemReminderInsertType
from src.kernel.event import EventDecision

from .config import StreamMemoryConfig
from .store import StreamMemoryStore, shared_store
from .utils import (
    SENSITIVITY_SOFT_SCOPED,
    format_local_time,
    person_id_of,
    person_name_of,
)

logger = log_api.get_logger("stream_memory.injector")

_NEWS_REMINDER_NAME = "相关新闻"
_PERSONA_REMINDER_NAME = "相关人物背景"

_NEWS_GUIDE_HEADER = (
    "以下是与当前对话中出现的相关人物有关的近期新闻记忆，供你回复时参考："
)
_NEWS_GUIDE_FOOTER = (
    "请自然地参考这些信息，仅在当前对话明确相关时使用，不要一次性罗列；"
    "这是内部参考资料，不是当前对方已经说过的事实。"
    "除非当前对话已确认，否则不要复述、暗示或据此推断对方的经历，"
    "也不要向对方说明这些信息的来源。"
)

_PERSONA_GUIDE_HEADER = (
    "以下是你对当前对话中出现的相关人物长期了解到的背景信息："
)
_PERSONA_GUIDE_FOOTER = (
    "请依据这些背景自然地理解对方，但只把它作为内部参考；"
    "不要把其他聊天流中的内容当作当前私聊已经发生的事实，"
    "除非当前对话明确确认，否则不要复述、暗示或据此推断；"
    "不要向对方透露你掌握这些信息的来源。"
)


def resolve_injection_flags(
    chat_type: str,
    injection: Any,
) -> tuple[bool, bool]:
    """按聊天类型决定新闻和人物背景是否允许注入。

    Bot 合一：私聊与群聊共享同一记忆域，默认都允许注入跨流记忆。
    ``allow_private_news`` / ``allow_private_personas`` 保留为逃生开关，
    需要重新隔离时可在配置中关闭。
    """
    normalized_type = str(chat_type or "").strip().lower()
    if normalized_type == "private":
        return (
            bool(getattr(injection, "inject_news", False))
            and bool(getattr(injection, "allow_private_news", True)),
            bool(getattr(injection, "inject_personas", False))
            and bool(getattr(injection, "allow_private_personas", True)),
        )
    if normalized_type != "group":
        return False, False
    return (
        bool(getattr(injection, "inject_news", False)),
        bool(getattr(injection, "inject_personas", False)),
    )


class StreamMemoryRecallInjector(BaseEventHandler):
    """回复前记忆召回注入器。

    订阅 ``on_message_received``，在消息入站时：
    1. 读取当前聊天流的 unread message 与最近历史消息，收集出现的人物 ID；
    2. 按人物匹配 + 三级敏感分级过滤召回新闻记忆，写入流私有 system reminder；
    3. 过滤人物层中涉及相关人物的人物背景信息，写入流私有 system reminder。

    无相关内容时删除对应 reminder，避免过期内容被继续注入。
    """

    name: str = "stream_memory_recall_injector"
    description: str = "回复前召回注入器：程序必执行路径，按人物匹配+敏感分级过滤召回记忆"
    weight: int = 5
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_MESSAGE_RECEIVED]
    dependencies: list[str] = []

    def _get_config(self) -> StreamMemoryConfig:
        if isinstance(self.plugin.config, StreamMemoryConfig):
            return self.plugin.config
        return StreamMemoryConfig()

    def _build_store(self) -> StreamMemoryStore:
        return shared_store(self.plugin, self._get_config)

    @staticmethod
    def _collect_unread_person_ids(
        stream: Any,
        history_limit: int = 20,
    ) -> set[str]:
        """收集当前对话中出现的人物 ID。

        优先读取 unread_messages；兼容已把 unread flush 进 history 的
        chatter 时序（neo_default_chatter 构建 prompt 前先 flush），
        再扫描最近 ``history_limit`` 条历史消息。排除 bot 自身消息。

        Args:
            stream: 聊天流实例。
            history_limit: 扫描最近历史消息的条数上限。
        """
        if stream is None:
            return set()
        context = getattr(stream, "context", None)
        if context is None:
            return set()
        person_ids: set[str] = set()

        def _collect(messages: Any) -> None:
            for message in messages or []:
                if str(getattr(message, "sender_role", "") or "").lower() == "bot":
                    continue
                person_id = person_id_of(message)
                if person_id:
                    person_ids.add(person_id)

        _collect(getattr(context, "unread_messages", None) or [])
        history = getattr(context, "history_messages", None) or []
        _collect(history[-max(0, int(history_limit)):])
        return person_ids

    @staticmethod
    def _sync_reminder(
        stream_id: str,
        bucket: str,
        name: str,
        content: str,
    ) -> None:
        """同步一条流私有 system reminder。

        有内容时写入（dynamic 注入到对话尾部）；无内容时删除，
        防止过期内容残留在后续请求中被重复注入。

        Args:
            stream_id: 聊天流 ID。
            bucket: reminder bucket（与 chatter 的 with_reminder 一致）。
            name: reminder 名称（同一 bucket 内唯一）。
            content: reminder 内容，空字符串表示删除。
        """
        if content:
            prompt_api.add_stream_reminder(
                stream_id,
                bucket,
                name,
                content,
                insert_type=SystemReminderInsertType.DYNAMIC,
                consume=SystemReminderConsumeType.FOREVER,
            )
        else:
            prompt_api.delete_stream_reminder(stream_id, bucket, name)

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 on_message_received 事件，按需同步相关记忆 reminder。"""
        config = self._get_config()
        injection = config.injection

        message = params.get("message")
        if message is None:
            return EventDecision.SUCCESS, params

        stream_id = str(getattr(message, "stream_id", "") or "").strip()
        if not stream_id:
            return EventDecision.SUCCESS, params
        bucket = str(injection.bucket or "actor").strip()
        chat_type = str(getattr(message, "chat_type", "") or "").strip().lower()
        stream = await stream_api.get_stream(stream_id)
        if not chat_type:
            chat_type = str(getattr(stream, "chat_type", "") or "").strip().lower()
        # 私聊默认不接收跨流记忆。即使配置热更新或之前曾在该流注入过，
        # 也要主动清理旧 reminder，避免上一轮群聊内容残留到当前私聊。
        news_enabled, personas_enabled = resolve_injection_flags(chat_type, injection)
        person_ids = self._collect_unread_person_ids(
            stream,
            int(getattr(injection, "person_scan_history_limit", 20) or 20),
        )
        # 显式补充当前消息的发送者（消息可能尚未被 distributor 注入 unread）
        current_person = person_id_of(message)
        if current_person:
            person_ids.add(current_person)

        store = self._build_store()

        # 群聊/私聊流首次对话时注册到摘要文件，使摘要任务只对活跃流发起 LLM 调用。
        # 私聊流带 chat_type="private"（仅用于区分展示与统计；Bot 合一，
        # 新闻与人物画像与群聊共用同一记忆域，不做物理隔离）。
        if chat_type == "group":
            await store.ensure_group(
                stream_id,
                platform=str(getattr(message, "platform", "") or ""),
                group_name=(
                    str(getattr(stream, "stream_name", "") or "")
                    if stream is not None
                    else ""
                ),
            )
        elif chat_type == "private":
            await store.ensure_group(
                stream_id,
                platform=str(getattr(message, "platform", "") or ""),
                group_name=(
                    person_name_of(message)
                    or (str(getattr(stream, "stream_name", "") or "") if stream is not None else "")
                ),
                chat_type="private",
            )

        news_block = ""
        persona_block = ""
        news_matched = 0
        personas_matched = 0

        if news_enabled:
            news_block, news_matched = await self._build_news_block(
                store, person_ids, stream_id, int(injection.news_max_inject)
            )

        if personas_enabled:
            persona_block, personas_matched = await self._build_persona_block(
                store, person_ids, int(injection.persona_max_inject)
            )

        # 同步新闻与人物背景 reminder；私聊禁用时也要清理旧内容。
        self._sync_reminder(
            stream_id,
            bucket,
            _NEWS_REMINDER_NAME,
            news_block if news_enabled else "",
        )
        self._sync_reminder(
            stream_id,
            bucket,
            _PERSONA_REMINDER_NAME,
            persona_block if personas_enabled else "",
        )

        logger.debug(
            f"已同步流私有 system reminder stream={stream_id[:8]} "
            f"chat_type={chat_type or 'unknown'} injection="
            f"news:{news_enabled}/personas:{personas_enabled} "
            f"bucket={bucket} persons={len(person_ids)} "
            f"news_matched={news_matched} personas_matched={personas_matched} "
            f"person_ids={sorted(person_ids)}"
        )
        return EventDecision.SUCCESS, params

    async def _build_news_block(
        self,
        store: StreamMemoryStore,
        person_ids: set[str],
        stream_id: str,
        max_inject: int,
    ) -> tuple[str, int]:
        """构建涉及当前人物的新闻注入块（敏感分级过滤）。

        使用 ``store.get_recallable_news`` 按三级敏感标记过滤跨流可见性：
        - hard_scoped 已在 store 层物理阻断；
        - soft_scoped 跨流可见，注入时附加警示前缀（由 LLM 语境判断是否引用）；
        - 若配置关闭 ``inject_soft_scoped``，则跨流 soft_scoped 条目也被跳过。

        Returns:
            tuple[str, int]: (注入块文本，匹配到的新闻条数；无匹配时块为空)。
        """
        config = self._get_config()
        injection = config.injection

        matched = await store.get_recallable_news(
            person_ids, stream_id, max_inject
        )

        # 配置关闭软敏感跨群注入时，过滤掉跨群的 soft_scoped 条目
        if not injection.inject_soft_scoped:
            matched = [
                entry
                for entry in matched
                if not (
                    entry.sensitivity == SENSITIVITY_SOFT_SCOPED
                    and (entry.origin_stream_id or "") != stream_id
                )
            ]

        if not matched:
            return "", 0

        warning = str(injection.soft_scoped_warning or "").strip()
        lines: list[str] = []
        for entry in matched:
            clock = format_local_time(entry.timestamp)
            line = f"- [{clock}] {entry.title}：{entry.content}"
            # 软敏感跨群记忆附加警示前缀
            if (
                entry.sensitivity == SENSITIVITY_SOFT_SCOPED
                and (entry.origin_stream_id or "") != stream_id
                and warning
            ):
                line = f"{warning}\n{line}"
            lines.append(line)
        body = "\n".join(lines)
        return f"{_NEWS_GUIDE_HEADER}\n{body}\n\n{_NEWS_GUIDE_FOOTER}", len(matched)

    async def _build_persona_block(
        self, store: StreamMemoryStore, person_ids: set[str], max_inject: int
    ) -> tuple[str, int]:
        """构建涉及当前人物的人物背景信息注入块。

        Returns:
            tuple[str, int]: (注入块文本，匹配到的人物条数；无匹配时块为空)。
        """
        all_personas = await store.get_all_personas()
        matched = [
            (person_id, text)
            for person_id, text in all_personas.items()
            if person_id in person_ids and text
        ]
        matched.sort(key=lambda item: len(item[1]), reverse=True)
        matched = matched[:max_inject]
        if not matched:
            return "", 0

        lines: list[str] = []
        for person_id, text in matched:
            name = self._person_display_name(person_id)
            lines.append(f"{name}（{person_id}）：\n{text}")
        body = "\n\n".join(lines)
        return (
            f"{_PERSONA_GUIDE_HEADER}\n\n{body}\n\n{_PERSONA_GUIDE_FOOTER}",
            len(matched),
        )

    def _person_display_name(self, person_id: str) -> str:
        """从人物 ID 推断展示名称（无昵称表时退回 ID 末段）。"""
        if ":" in person_id:
            return person_id.rsplit(":", 1)[-1]
        return person_id
