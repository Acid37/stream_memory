"""Stream Memory 新闻浏览工具组件。

提供一个面向角色（LLM 可调用）的小工具 ``browse_group_news``，满足“自己就能看群里发生的事”
的诉求：
1. 直接读取 news.json 里的全部全局事实记忆；
2. 关键词搜索标题、正文与涉及人物，找大家聊过的话题；
3. 默认按来源群聊分组展示，让人分清哪个群发生了什么事。

工具本身只做只读检索，不产生任何 LLM 调用，也不改写记忆文件。
"""

from __future__ import annotations

from typing import Annotated

from src.app.plugin_system.base import BaseTool

from .config import StreamMemoryConfig
from .store import StreamMemoryStore, shared_store
from .utils import format_local_time


class BrowseGroupNewsTool(BaseTool):
    """浏览各群聊的全局事实记忆（news.json），支持按群分类与关键词搜索。"""

    tool_name = "browse_group_news"
    tool_description = (
        "浏览你大脑里记录的「群聊新闻/全局事实记忆」（即 news.json 的内容）。\n"
        "用途：当你想自己回顾最近各个群里都发生了什么事、或者想搜索大家之前聊过的某个话题时使用。\n"
        "默认会【按来源群聊分组】展示，让你一眼分清哪件事发生在哪个群。\n"
        "可以配合关键词搜索标题/正文/涉及人物，也可以只限定某个群。\n"
        "注意：本工具只读取已有记忆，不会修改任何记忆文件，也不会触发新的总结。"
    )

    chatter_allow: list[str] = []

    def _get_config(self) -> StreamMemoryConfig:
        if isinstance(getattr(self.plugin, "config", None), StreamMemoryConfig):
            return self.plugin.config
        return StreamMemoryConfig()

    def _build_store(self) -> StreamMemoryStore:
        return shared_store(self.plugin, self._get_config)

    async def execute(
        self,
        keyword: Annotated[
            str,
            "可选：关键词。会同时匹配新闻标题、正文内容、以及涉及人物的名字/ID。留空表示不过滤，返回全部记忆。",
        ] = "",
        group_identifier: Annotated[
            str,
            "可选：只查看某个群的记忆。可填群名（模糊匹配）、群号、或索引号（如 '1'）。留空表示查看所有群的记忆。",
        ] = "",
        sensitivity: Annotated[
            str,
            "可选：按敏感等级过滤，留空表示不过滤。可选值：normal（普通）、soft_scoped（跨群软警告）、hard_scoped（仅本群可见）。",
        ] = "",
        limit: Annotated[
            int,
            "返回的新闻条数上限，默认 20 条。设置更大可以一次看更多历史。",
        ] = 20,
    ) -> tuple[bool, str]:
        """按群分类检索并展示群聊新闻记忆。"""

        try:
            store = self._build_store()
            all_news = await store.get_news()
            groups = await store.list_group_summaries()
        except Exception as error:
            return False, f"读取记忆库失败: {error}"

        if not all_news:
            return True, "目前大脑里还没有任何群聊新闻记忆哦～（news.json 是空的）"

        # 群聊元数据：stream_id -> (group_name, group_id, chat_type)
        group_meta: dict[str, tuple[str, str, str]] = {}
        for g in groups:
            group_meta[g.stream_id] = (
                g.group_name or "未命名群聊",
                g.group_id,
                str(getattr(g, "chat_type", "") or "") or "group",
            )

        # 1. 关键词过滤
        kw = (keyword or "").strip().lower()
        if kw:
            filtered = []
            for entry in all_news:
                haystack = [entry.title, entry.content]
                for ref in entry.participants:
                    haystack.append(ref.name)
                    haystack.append(ref.person_id)
                if any(kw in (str(part) or "").lower() for part in haystack):
                    filtered.append(entry)
            all_news = filtered

        # 2. 群过滤
        if group_identifier.strip():
            ident = group_identifier.strip()
            matched_stream_ids = self._resolve_groups(ident, groups)
            if not matched_stream_ids:
                return False, (
                    f"没有找到匹配 '{group_identifier}' 的群聊。"
                    "可以试试更精确的群名、群号，或留空查看所有群。"
                )
            all_news = [e for e in all_news if e.origin_stream_id in set(matched_stream_ids)]

        # 3. 敏感等级过滤
        sens = (sensitivity or "").strip().lower()
        if sens:
            if sens not in ("normal", "soft_scoped", "hard_scoped"):
                return False, "sensitivity 只能填 normal / soft_scoped / hard_scoped 之一，或留空。"
            all_news = [e for e in all_news if (e.sensitivity or "normal") == sens]

        if not all_news:
            scope = f"关键词「{keyword}」" if kw else "当前筛选条件"
            return True, f"在记忆里没有找到符合{scope}的群聊新闻～"

        # 4. 按时间降序
        all_news.sort(key=lambda item: item.timestamp, reverse=True)

        # 5. 分组：按 origin_stream_id
        grouped: dict[str, list] = {}
        for entry in all_news:
            grouped.setdefault(entry.origin_stream_id, []).append(entry)

        # 群分组排序：有名字的优先，未知流排后面
        def _group_sort_key(stream_id: str) -> str:
            name, _, _ = group_meta.get(stream_id, ("", "", ""))
            return name or f"__{stream_id}"

        ordered_streams = sorted(grouped.keys(), key=_group_sort_key)

        # 6. 拼装输出（受 limit 限制总数）
        lines: list[str] = []
        remaining = max(1, int(limit))
        shown = 0
        for stream_id in ordered_streams:
            name, gid, chat_type = group_meta.get(
                stream_id, ("未知流", "", "group")
            )
            entries = grouped[stream_id]
            if str(chat_type or "").strip().lower() == "private":
                header = f"💬 私聊：{name}"
            else:
                header = f"📌 群聊：{name}"
            if gid:
                header += f"（群号 {gid}）"
            header += f"  —— 共 {len(entries)} 条相关记忆"
            lines.append(header)
            lines.append("─" * len(header))
            for entry in entries:
                if shown >= remaining:
                    break
                clock = format_local_time(entry.timestamp)
                sens_tag = (entry.sensitivity or "normal").upper()
                title = entry.title or "（无标题）"
                content = entry.content or "（无内容）"
                # 正文较长时截断，避免刷屏
                if len(content) > 400:
                    content = content[:400] + "…（已截断）"
                line = [
                    f"  [{clock}] [{sens_tag}] {title}",
                    f"  {content}",
                ]
                if entry.participants:
                    people = "、".join(
                        (p.name or p.person_id) for p in entry.participants
                    )
                    line.append(f"  涉及：{people}")
                lines.extend(line)
                lines.append("")
                shown += 1
            lines.append("")
            if shown >= remaining:
                break

        total = sum(len(v) for v in grouped.values())
        if shown >= remaining and total > remaining:
            lines.append(f"（已按 limit={limit} 截断，更多记忆请缩小关键词或提高 limit）")

        lines.insert(0, f"共找到 {total} 条群聊新闻记忆（当前展示 {shown} 条）：")
        lines.insert(1, "")
        return True, "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_groups(
        identifier: str,
        groups: list,
    ) -> list[str]:
        """根据标识符解析出匹配的群聊 stream_id 列表。"""

        ident = identifier.strip()
        # 索引号
        if ident.isdigit() and len(ident) <= 4:
            idx = int(ident) - 1
            if 0 <= idx < len(groups):
                return [groups[idx].stream_id]

        # 群号精确匹配（长数字）
        if ident.isdigit():
            for g in groups:
                if g.group_id and str(g.group_id) == ident:
                    return [g.stream_id]

        # 群名模糊匹配
        return [
            g.stream_id
            for g in groups
            if ident.lower() in (g.group_name or "").lower()
        ]
