"""Stream Memory 服务。

对外提供三层记忆状态的读取能力（摘要 / 新闻 / 人物信息），
供其他插件或组件以 Service 方式复用。

新增于 shameimaru_memory：
- ``get_recallable_news``：按三级敏感标记过滤的召回读取入口，
  委托 ``StreamMemoryStore.get_recallable_news`` 执行 hard_scoped 物理阻断。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseService

from .config import StreamMemoryConfig
from .store import StreamMemoryStore, shared_store

logger = log_api.get_logger("stream_memory.service")


class StreamMemoryService(BaseService):
    """Stream Memory 记忆系统服务。"""

    name: str = "stream_memory"
    description: str = "Stream Memory 记忆系统服务：摘要 / 新闻 / 人物信息读取"
    version: str = "1.0.0"

    def _get_config(self) -> StreamMemoryConfig:
        if isinstance(self.plugin.config, StreamMemoryConfig):
            return self.plugin.config
        return StreamMemoryConfig()

    def _build_store(self) -> StreamMemoryStore:
        return shared_store(self.plugin, self._get_config)

    async def get_group_summaries(self) -> list[dict[str, Any]]:
        """获取全部群聊摘要（含群元信息与条目列表）。"""
        store = self._build_store()
        groups = await store.list_group_summaries()
        return [
            {
                "stream_id": group.stream_id,
                "platform": group.platform,
                "group_id": group.group_id,
                "group_name": group.group_name,
                "last_summarized_at": max((entry.timestamp for entry in group.entries), default=0.0),
                "entries": [entry.to_dict() for entry in group.entries],
            }
            for group in groups
        ]

    async def get_news_entries(self) -> list[dict[str, Any]]:
        """获取全部新闻条目。"""
        store = self._build_store()
        return [entry.to_dict() for entry in await store.get_news()]

    async def get_recallable_news(
        self,
        person_ids: set[str],
        current_stream_id: str,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """获取可召回的新闻条目（按三级敏感标记过滤跨群可见性）。

        委托 ``StreamMemoryStore.get_recallable_news`` 执行过滤：
        - 按人物命中过滤（参与者与 person_ids 取交集）；
        - hard_scoped 仅在来源群聊内可召回，跨群物理不可达；
        - soft_scoped 跨群可见（警示前缀由调用方附加）；
        - 按时间戳降序排序后截断到 ``max_results`` 条。

        Args:
            person_ids: 当前对话出现的人物 ID 集合。
            current_stream_id: 当前发起召回的群聊 ID，用于 hard_scoped 过滤。
            max_results: 返回条目数上限。

        Returns:
            list[dict[str, Any]]: 可召回的新闻条目字典列表。
        """
        store = self._build_store()
        entries = await store.get_recallable_news(
            person_ids, current_stream_id, max_results
        )
        return [entry.to_dict() for entry in entries]

    async def get_persona(self, person_id: str) -> str:
        """获取指定人物的背景信息文本。"""
        store = self._build_store()
        return await store.get_persona(person_id)

    async def get_all_personas(self) -> dict[str, str]:
        """获取全部人物背景信息。"""
        store = self._build_store()
        return await store.get_all_personas()
