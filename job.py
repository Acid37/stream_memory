"""Stream Memory 周期任务。

两个事件：
- 摘要更新（SummaryJob）：按配置间隔从每个群聊的聊天流生成摘要并持久化。
- 新闻记录（NewsJob）：按配置间隔读取全部群聊摘要，整理总结性记忆条目。
  巩固时对本组全部新闻条目批量做三级敏感分级
  （hard_scoped / soft_scoped / normal），并记录来源群聊 ID（origin_stream_id），
  供召回时跨群过滤。

人物层更新：新闻条目因达到上限被删除时，
逐人物增量更新本地持久化的人物背景信息表。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from src.app.plugin_system.api import log_api, stream_api
from src.core.models.message import Message

from .models import NewsEntry, PersonRef, SummaryEntry
from .prompts import (
    NEWS_PROMPT,
    NEWS_PROMPT_NAME,
    NO_MEANINGFUL_CONTENT_TOKEN,
    PERSONA_PROMPT,
    PERSONA_PROMPT_NAME,
    SENSITIVITY_PROMPT,
    SENSITIVITY_PROMPT_NAME,
    SUMMARY_PROMPT,
    SUMMARY_PROMPT_NAME,
)
from .store import StreamMemoryStore, shared_store
from .sub_agent import call_sub_agent, extract_json_array, resolve_prompt
from .utils import (
    ALL_SENSITIVITY_LEVELS,
    SENSITIVITY_NORMAL,
    SENSITIVITY_SOFT_SCOPED,
    format_local_time,
    message_time,
    person_id_of,
    person_name_of,
)

logger = log_api.get_logger("stream_memory.job")


def _get_config(plugin: Any) -> Any:
    """读取插件配置。"""
    return getattr(plugin, "config", None)


def _build_store(plugin: Any) -> StreamMemoryStore:
    """获取插件级共享存储实例（所有周期任务共用一个 store，避免并发写文件互相覆盖）。"""
    return shared_store(plugin, lambda: _get_config(plugin))


def _llm_task(config: Any, attr: str) -> str:
    """读取插件配置中的模型任务名。"""
    try:
        return str(getattr(getattr(config, "llm", None), attr, "") or "tool_use")
    except Exception:  # noqa: BLE001
        return "tool_use"


def _collect_participants(messages: list[Message]) -> list[PersonRef]:
    """从消息列表提取去重后的人物引用（排除 bot 自身消息）。"""
    refs: list[PersonRef] = []
    seen: set[str] = set()
    for message in messages:
        if str(getattr(message, "sender_role", "") or "").lower() == "bot":
            continue
        person_id = person_id_of(message)
        if not person_id or person_id in seen:
            continue
        seen.add(person_id)
        refs.append(PersonRef(person_id=person_id, name=person_name_of(message)))
    return refs


def _build_chat_flow(messages: list[Message]) -> str:
    """将消息列表格式化为聊天记录文本（按时间先后）。

    排除 bot 自身消息，避免摘要/新闻以 Bot 为主角，导致人物信息被错误归属。
    """
    lines: list[str] = []
    for message in messages:
        if str(getattr(message, "sender_role", "") or "").lower() == "bot":
            continue
        text = str(getattr(message, "processed_plain_text", "") or "").strip()
        if not text:
            continue
        sender = person_name_of(message)
        clock = format_local_time(message_time(message))
        prefix = f"[{clock}] " if clock else ""
        lines.append(f"{prefix}{sender}: {text}")
    return "\n".join(lines)


def _parse_participants(raw: Any) -> list[PersonRef]:
    """从子 agent 返回的 participants 字段解析人物引用。"""
    refs: list[PersonRef] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return refs
    for item in raw:
        if not isinstance(item, dict):
            continue
        person_id = str(item.get("person_id") or "").strip()
        if not person_id or person_id in seen:
            continue
        seen.add(person_id)
        refs.append(PersonRef(person_id=person_id, name=str(item.get("name") or "")))
    return refs


def _resolve_participants(
    raw: Any,
    roster_by_id: dict[str, PersonRef],
    roster_by_name: dict[str, PersonRef],
) -> list[PersonRef]:
    """把子 agent 返回的 participants 映射回真实人物引用。

    子 agent 只能看到摘要正文中的人物名，无法获知真实 person_id，
    因此其返回的 person_id 可能是编造的。这里按「先 id 后 name」从
    摘要条目的真实参与人物清单（roster）中匹配：

    - 匹配成功：使用清单中的真实 person_id（name 保留子 agent 的写法）；
    - 匹配失败：视为编造的人物，直接丢弃。

    Args:
        raw: 子 agent 返回的 participants 原始字段。
        roster_by_id: person_id -> 真实人物引用。
        roster_by_name: 人物名 -> 真实人物引用。

    Returns:
        list[PersonRef]: 解析后的真实人物引用列表。
    """
    resolved: list[PersonRef] = []
    seen: set[str] = set()
    for parsed in _parse_participants(raw):
        real = roster_by_id.get(parsed.person_id) or roster_by_name.get(parsed.name)
        if real is None or real.person_id in seen:
            continue
        seen.add(real.person_id)
        resolved.append(
            PersonRef(person_id=real.person_id, name=parsed.name or real.name)
        )
    return resolved


# ----------------------------------------------------------------------
# 摘要层：摘要更新事件
# ----------------------------------------------------------------------


async def run_summary_job(plugin: Any) -> dict[str, Any]:
    """摘要更新事件：按摘要文件中出现的群聊依次生成摘要并持久化。

    只对摘要文件中已注册的群聊发起 LLM 调用（新群聊由回复前注入器
    在发生对话时注册），避免对数据库中全部（含长期不活跃）群聊调用。

    Args:
        plugin: 插件实例。

    Returns:
        dict[str, Any]: 统计信息。
    """
    config = _get_config(plugin)
    store = _build_store(plugin)
    summary_cfg = config.summary
    task = _llm_task(config, "summary_task")

    groups = await store.list_group_summaries()
    stats: dict[str, Any] = {"groups": len(groups), "summarized": 0, "skipped": 0}

    for group in groups:
        try:
            ok = await _summarize_group(store, group.stream_id, summary_cfg, task)
            stats["summarized" if ok else "skipped"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(f"摘要生成失败 stream_id={group.stream_id}: {exc}")
            stats["skipped"] += 1

    logger.info(f"摘要更新完成: {stats}")
    return stats


async def _summarize_group(
    store: StreamMemoryStore,
    stream_id: str,
    summary_cfg: Any,
    task: str,
) -> bool:
    """为单个群聊生成并持久化一条摘要。无有意义内容时跳过。"""
    group = await store.get_group_summary(stream_id)
    limit = int(summary_cfg.max_messages_per_run)

    messages = await stream_api.get_stream_messages(stream_id, limit=limit)
    chat_flow = _build_chat_flow(messages)
    if not messages or not chat_flow:
        return False

    group_name = group.group_name
    platform = group.platform
    group_id = group.group_id

    stream = await stream_api.get_stream(stream_id)
    if stream is not None:
        if not platform:
            platform = str(getattr(stream, "platform", "") or "")
        if not group_name:
            group_name = str(getattr(stream, "stream_name", "") or "")

    # ChatStream 对象不携带 group_id，从数据库流记录补全 platform/group_id/group_name
    info = await stream_api.get_stream_info(stream_id)
    if info is not None:
        if not platform:
            platform = str(info.get("platform") or "")
        if not group_id:
            group_id = str(info.get("group_id") or "")
        if not group_name:
            group_name = str(info.get("group_name") or "")

    # 最后兜底：从消息中提取
    for message in messages:
        if not platform:
            platform = str(getattr(message, "platform", "") or "")
        if not group_id:
            extra = getattr(message, "extra", None)
            group_id = str((extra or {}).get("group_id") or "")
        if platform and group_id:
            break

    system = resolve_prompt(SUMMARY_PROMPT_NAME, SUMMARY_PROMPT)
    user = f"群聊名称：{group_name or stream_id}\n聊天记录（按时间先后排列）：\n{chat_flow}"
    result = await call_sub_agent(
        task=task,
        request_name="stream_memory_summary",
        system=system,
        user=user,
        stream_id=stream_id,
    )
    if not result or result == NO_MEANINGFUL_CONTENT_TOKEN:
        return False

    entry = SummaryEntry(
        timestamp=time.time(),
        content=result,
        participants=_collect_participants(messages),
    )
    await store.append_summary(
        stream_id=stream_id,
        entry=entry,
        platform=platform,
        group_id=group_id,
        group_name=group_name,
        max_entries=int(summary_cfg.max_entries_per_group),
    )
    return True


# ----------------------------------------------------------------------
# 新闻层：新闻记录事件
# ----------------------------------------------------------------------


async def run_news_job(plugin: Any) -> dict[str, Any]:
    """新闻记录事件：对每个群聊分别执行新闻整理。

    每个群聊的摘要单独分组，各自调用一次子 agent（不混合所有群）。
    只消费未废弃的摘要条目；消费后的摘要被标记为废弃而非删除。

    新增于 shameimaru_memory：巩固时对本组全部新闻条目批量做三级敏感
    分级，并记录来源群聊 ID（origin_stream_id），供召回时跨群过滤。

    Args:
        plugin: 插件实例。

    Returns:
        dict[str, Any]: 统计信息。
    """
    config = _get_config(plugin)
    store = _build_store(plugin)
    news_cfg = config.news
    news_task = _llm_task(config, "news_task")
    sensitivity_cfg = config.sensitivity
    sensitivity_task = str(
        getattr(sensitivity_cfg, "classify_task", "") or "tool_use"
    )
    persona_task = _llm_task(config, "persona_task")
    max_text_length = int(getattr(config.persona, "max_text_length", 0) or 0)

    groups = await store.list_group_summaries()
    stats: dict[str, Any] = {
        "groups": len(groups),
        "processed": 0,
        "created": 0,
        "evicted": 0,
        "personas_updated": 0,
        "skipped": 0,
    }

    # 本轮全部新生成的新闻，用于循环结束后统一更新人物画像
    all_new_entries: list[NewsEntry] = []

    for group in groups:
        if not any(not entry.deprecated for entry in group.entries):
            continue
        try:
            created, new_entries, evicted = await _news_for_group(
                store,
                group,
                news_cfg,
                news_task,
                sensitivity_cfg,
                sensitivity_task,
            )
            stats["processed"] += 1
            stats["created"] += created
            stats["evicted"] += len(evicted)
            all_new_entries.extend(new_entries)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"新闻整理失败 stream_id={group.stream_id}: {exc}")
            stats["skipped"] += 1

    # 人物画像即时建档：用本轮全部新新闻涉及的人物统一更新画像。
    # 相比旧的「新闻淘汰时才更新」，每次有实质内容整理成新闻就会立即
    # 沉淀人物画像，显著缩短建档周期，避免大量用户长期没有档案。
    if all_new_entries:
        max_updates_per_round = int(
            getattr(config.persona, "max_updates_per_round", 0) or 0
        )
        try:
            stats["personas_updated"] = await _update_personas_from_news(
                store,
                all_new_entries,
                persona_task,
                max_text_length,
                max_per_round=max_updates_per_round,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"人物画像即时建档失败: {exc}")

    logger.info(f"新闻记录完成: {stats}")
    return stats


async def _news_for_group(
    store: StreamMemoryStore,
    group: Any,
    news_cfg: Any,
    task: str,
    sensitivity_cfg: Any,
    sensitivity_task: str,
) -> tuple[int, list[NewsEntry]]:
    """为单个群聊的未废弃摘要执行一次新闻整理，随后标记已消费摘要为废弃。

    新增于 shameimaru_memory：
    - 每条新闻记录来源群聊 ID（origin_stream_id = group.stream_id）；
    - 若敏感分级开启，对本组全部新闻条目批量调用一次 LLM 做三级敏感判断
      （hard_scoped / soft_scoped / normal），就地更新各条目的 sensitivity 字段；
    - 分级失败时（LLM 异常 / 空响应 / 解析失败）保留 SENSITIVITY_NORMAL
      作为安全默认，不阻塞新闻写入流水线。

    流程：
    1. 收集未废弃摘要，构建人物清单与摘要文本；
    2. 调用新闻整理子 agent，解析出 JSON 数组；
    3. 创建全部 NewsEntry 对象，设置 origin_stream_id；
    4. 若敏感分级开启，对本组全部条目批量调用 ``_classify_sensitivity``；
    5. 逐一写入存储（携带 sensitivity 与 origin_stream_id）；
    6. 标记已消费摘要为废弃。

    Args:
        store: 存储层实例。
        group: 群聊摘要记录。
        news_cfg: 新闻层配置节。
        task: 新闻层子 agent 使用的模型任务名。
        sensitivity_cfg: 敏感标记配置节。
        sensitivity_task: 敏感分级子 agent 使用的模型任务名。

    Returns:
        tuple[int, list[NewsEntry], list[NewsEntry]]:
        (创建的新闻条数, 本组新生成的新闻条目, 因上限被淘汰的新闻条目)。
    """
    entries = [entry for entry in group.entries if not entry.deprecated]
    if not entries:
        return 0, [], []
    cap = int(getattr(news_cfg, "max_input_summaries", 0))
    if cap > 0 and len(entries) > cap:
        entries = entries[-cap:]

    # 构建本群可用人物清单（真实 person_id），供子 agent 选择，防止编造 ID
    roster_by_id: dict[str, PersonRef] = {}
    roster_by_name: dict[str, PersonRef] = {}
    for entry in entries:
        for ref in entry.participants:
            roster_by_id.setdefault(ref.person_id, ref)
            if ref.name:
                roster_by_name.setdefault(ref.name, ref)
    roster_lines = [
        f"- {ref.person_id}（{ref.name or '无名称'}）"
        for ref in sorted(roster_by_id.values(), key=lambda item: item.person_id)
    ]
    roster_text = "\n".join(roster_lines) if roster_lines else "（无）"

    lines: list[str] = []
    for entry in entries:
        clock = format_local_time(entry.timestamp)
        lines.append(f"【{clock}】\n{entry.content}")
    summaries_text = "\n\n".join(lines)

    system = resolve_prompt(NEWS_PROMPT_NAME, NEWS_PROMPT)
    user = (
        f"群聊名称：{group.group_name or group.stream_id}\n"
        f"群聊摘要：\n{summaries_text}\n\n"
        f"可用人物清单（participants 的 person_id 必须从中选择，不得编造）：\n{roster_text}"
    )
    result = await call_sub_agent(
        task=task,
        request_name="stream_memory_news",
        system=system,
        user=user,
        stream_id=group.stream_id,
    )
    # 调用失败（LLM 异常/空响应）时保留摘要，等待下轮重试，避免摘要数据永久丢失
    if not result:
        return 0, [], []
    items = extract_json_array(result)

    now = time.time()
    # 先创建全部新闻条目，再批量做敏感分级，最后逐一写入存储
    new_entries: list[NewsEntry] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        if not title or not content:
            continue
        participants = _resolve_participants(
            item.get("participants"), roster_by_id, roster_by_name
        )
        entry = NewsEntry(
            id=f"news-{uuid.uuid4().hex}",
            timestamp=now,
            title=title,
            content=content,
            participants=participants,
        )
        entry.origin_stream_id = group.stream_id
        new_entries.append(entry)

    # 敏感分级：对本组全部新闻条目批量调用一次 LLM（仅在有新闻条目时调用）。
    # Bot 合一：私聊与群聊新闻统一走三级敏感分级，不再按流强制 hard_scoped。
    if new_entries and getattr(sensitivity_cfg, "enabled", False):
        try:
            await _classify_sensitivity(
                new_entries, sensitivity_task, group.stream_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"敏感分级失败 stream_id={group.stream_id}，"
                f"本组新闻按普通级处理: {exc}"
            )

    created = 0
    evicted: list[NewsEntry] = []
    for entry in new_entries:
        evicted.extend(
            await store.append_news(
                entry,
                int(news_cfg.max_entries),
                sensitivity=entry.sensitivity,
                origin_stream_id=entry.origin_stream_id,
            )
        )
        created += 1

    # 参与处理的摘要已消费，标记为废弃（新闻层不再消费）
    await store.deprecate_group_summaries(group.stream_id)
    return created, new_entries, evicted


async def _classify_sensitivity(
    entries: list[NewsEntry],
    task: str,
    stream_id: str,
) -> None:
    """调用 LLM 对新闻条目批量进行敏感分级，就地更新各条目的 sensitivity 字段。

    将本组全部新闻条目一次性交给 LLM 判断，返回 JSON 数组：
    ``[{"index": 0, "sensitivity": "normal"}, ...]``。

    分级失败时（LLM 异常 / 空响应 / 解析失败）保留 SENSITIVITY_NORMAL
    作为安全默认，不阻塞新闻写入流水线。

    Args:
        entries: 待分级的新闻条目列表（就地更新 sensitivity 字段）。
        task: 敏感分级子 agent 使用的模型任务名。
        stream_id: 来源群聊 ID，用于 LLM 统计聚合。
    """
    if not entries:
        return

    # 构建输入：[{index, title, content}, ...]
    items_input = json.dumps(
        [
            {"index": idx, "title": entry.title, "content": entry.content}
            for idx, entry in enumerate(entries)
        ],
        ensure_ascii=False,
        indent=1,
    )

    system = resolve_prompt(SENSITIVITY_PROMPT_NAME, SENSITIVITY_PROMPT)
    user = f"以下是需要判断敏感等级的记忆条目：\n{items_input}"

    result = await call_sub_agent(
        task=task,
        request_name="stream_memory_sensitivity",
        system=system,
        user=user,
        stream_id=stream_id,
    )
    if not result:
        return

    items = extract_json_array(result)

    # 构建 index -> sensitivity 映射，仅接受合法等级
    sensitivity_map: dict[int, str] = {}
    for item in items:
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        sensitivity = str(item.get("sensitivity") or "").strip()
        if sensitivity in ALL_SENSITIVITY_LEVELS:
            sensitivity_map[index] = sensitivity

    # 就地更新各条目的 sensitivity 字段
    for idx, entry in enumerate(entries):
        if idx in sensitivity_map:
            entry.sensitivity = sensitivity_map[idx]
        # 未命中时保留 SENSITIVITY_NORMAL（NewsEntry 默认值，安全兜底）


# ----------------------------------------------------------------------
# 人物层：本轮新新闻涉及人物的即时画像建档/更新
# ----------------------------------------------------------------------


async def _update_personas_from_news(
    store: StreamMemoryStore,
    new_entries: list[NewsEntry],
    task: str,
    max_text_length: int,
    max_per_round: int = 0,
) -> int:
    """用本轮新生成的新闻，对涉及人物进行即时画像建档/增量更新。

    相比旧的「新闻淘汰时才更新画像」：
    - 每次群聊/私聊有实质内容被整理成新闻，涉及人物当轮就会被更新画像，
      大幅缩短建档周期，避免大量用户长期没有档案；
    - 人物在多个群出现时按人聚合，同一个人一轮只更新一次（少一次 LLM 调用）。

    ``max_per_round`` > 0 时，按「本轮出现次数」排序只更新最活跃的前 N 人，
    用于控制单轮 LLM 调用成本（默认 0 表示不限制）。

    Returns:
        int: 本轮实际更新的人物数。
    """
    by_person: dict[str, list[NewsEntry]] = {}
    for entry in new_entries:
        for ref in entry.participants:
            if not ref.person_id:
                continue
            by_person.setdefault(ref.person_id, []).append(entry)

    if not by_person:
        return 0

    ranked = sorted(
        by_person.items(), key=lambda item: len(item[1]), reverse=True
    )
    if max_per_round > 0:
        ranked = ranked[: max_per_round]

    system = resolve_prompt(PERSONA_PROMPT_NAME, PERSONA_PROMPT)
    if max_text_length > 0:
        system = (
            f"{system}\n\n"
            f"重要约束：输出的人物信息总长度请控制在 {max_text_length} 字以内，"
            "不要输出过长的文本。"
        )

    updated = 0
    for person_id, entries in ranked:
        try:
            await _update_persona(
                store, system, person_id, entries, task, max_text_length
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"人物背景更新失败，该人物的本次增量信息将永久丢失 "
                f"person_id={person_id}: {exc}"
            )
    return updated


async def _update_persona(
    store: StreamMemoryStore,
    system: str,
    person_id: str,
    entries: list[NewsEntry],
    task: str,
    max_text_length: int,
) -> None:
    """更新单个人物的背景信息。

    将旧文本与新内容一并交给 LLM 融合生成新文本（而非机械拼接），
    写入前按 ``max_text_length`` 硬截断兜底。
    提示词中携带该人物在新闻中的名字，并强调以该人物本人为视角，
    防止 LLM 把内容主角（如 Bot）误当作被维护的人物。

    人物库为统一 personas.json（Bot 合一，群聊与私聊共用同一画像域）。
    """
    current = await store.get_persona(person_id)
    person_name = ""
    for entry in sorted(entries, key=lambda item: item.timestamp):
        for ref in entry.participants:
            if ref.person_id == person_id and ref.name:
                person_name = ref.name
                break
        if person_name:
            break
    content_lines = [
        f"- {entry.title}: {entry.content}"
        for entry in sorted(entries, key=lambda item: item.timestamp)
    ]
    user = (
        f"人物 ID：{person_id}\n"
        f"人物名字：{person_name or '（未知，请根据新内容推断）'}\n"
        f"现有背景信息：\n{current or '（无）'}\n\n"
        f"关于该人物的新内容：\n" + "\n".join(content_lines)
    )
    new_text = await call_sub_agent(
        task=task,
        request_name="stream_memory_persona",
        system=system,
        user=user,
    )
    if not new_text:
        return
    if max_text_length > 0 and len(new_text) > max_text_length:
        new_text = new_text[:max_text_length]
    await store.set_persona(person_id, new_text)
