"""Stream Memory 语义召回模块（最小闭环验证）。

在现有「精确 person_id 交集召回」（:meth:`StreamMemoryStore.get_recallable_news`）之外，
提供一条「语义向量召回」通道：文本 -> embedding -> Chroma query -> 敏感级作用域过滤 -> 注入文本。

设计要点（缓存契约）：
- 本模块只产出「召回 + 过滤后的纯文本行」，header/footer 与 dynamic 注入位置
  由调用方（event_handler）决定。召回结果必须走 dynamic 尾部注入，
  严禁进入 chatter 人设 / system 的 fixed 前缀区，避免破坏前缀缓存命中。
- 敏感级作用域过滤复用 store 里已被验证的三级语义：hard_scoped 跨流物理阻断，
  soft_scoped 跨流保留但注入时附加警示前缀，normal 自由召回。

纯函数（可离线单测）与 thin service（embed_fn 通过依赖注入）分离，
使向量检索 / 敏感过滤链路无需真实模型运行时即可被验证。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .utils import (
    SENSITIVITY_HARD_SCOPED,
    SENSITIVITY_SOFT_SCOPED,
    format_local_time,
)

# 单条文本 -> 稠密向量
EmbedFn = Callable[[str], Awaitable[list[float]]]


@dataclass(slots=True)
class SemanticRecallHit:
    """语义召回命中条目。

    ``sensitivity`` / ``origin_stream_id`` 承载跨流三级敏感过滤所需的元数据，
    与 NewsEntry 保持一致，便于后续接通到现有注入器。
    """

    id: str
    content: str
    distance: float = 0.0
    title: str = ""
    timestamp: float = 0.0
    sensitivity: str = "normal"
    origin_stream_id: str = ""


def parse_query_hits(raw: dict[str, Any]) -> list[SemanticRecallHit]:
    """把 Chroma query 的嵌套返回结构解析为结构化命中列表。

    Chroma 的 ``query`` 返回形如：:

        {
            "ids": [["a", "b"]],
            "distances": [[0.1, 0.2]],
            "metadatas": [[{...}, {...}]],
            "documents": [["doc1", "doc2"]],
        }

    外层 list 对应每个 query_embedding（本模块一次只查一个），取 ``[0]`` 行。
    """
    ids_row: list[Any] = []
    if raw.get("ids"):
        ids_row = raw["ids"][0] if raw["ids"] else []
    distances_row: list[Any] = []
    if raw.get("distances"):
        distances_row = raw["distances"][0] if raw["distances"] else []
    metadatas_row: list[Any] = []
    if raw.get("metadatas"):
        metadatas_row = raw["metadatas"][0] if raw["metadatas"] else []
    documents_row: list[Any] = []
    if raw.get("documents"):
        documents_row = raw["documents"][0] if raw["documents"] else []

    hits: list[SemanticRecallHit] = []
    for idx, hit_id in enumerate(ids_row):
        md = metadatas_row[idx] if idx < len(metadatas_row) and isinstance(metadatas_row[idx], dict) else {}
        content = documents_row[idx] if idx < len(documents_row) else ""
        distance = distances_row[idx] if idx < len(distances_row) else 0.0
        hits.append(
            SemanticRecallHit(
                id=str(hit_id),
                content=str(content or ""),
                distance=float(distance),
                title=str(md.get("title") or ""),
                timestamp=float(md.get("timestamp") or 0.0),
                sensitivity=str(md.get("sensitivity") or "normal"),
                origin_stream_id=str(md.get("origin_stream_id") or ""),
            )
        )
    return hits


def apply_sensitivity_scope(
    hits: list[SemanticRecallHit],
    current_stream_id: str,
) -> list[SemanticRecallHit]:
    """按三级敏感标记过滤跨流可见性。

    语义与 :meth:`StreamMemoryStore.get_recallable_news` 一致：
    - hard_scoped：仅当 ``origin_stream_id == current_stream_id`` 放行，跨流物理阻断；
    - soft_scoped / normal：放行（soft_scoped 的警示前缀由注入文本阶段附加）。
    """
    kept: list[SemanticRecallHit] = []
    for hit in hits:
        sensitivity = hit.sensitivity or "normal"
        if sensitivity == SENSITIVITY_HARD_SCOPED:
            if (hit.origin_stream_id or "") != current_stream_id:
                continue
        kept.append(hit)
    return kept


def build_injection_lines(
    hits: list[SemanticRecallHit],
    current_stream_id: str,
    warning: str = "",
) -> list[str]:
    """把过滤后的命中组装为注入文本行（与现有新闻注入行格式保持一致）。

    soft_scoped 跨流条目在行首附加警示前缀，交由 LLM 语境判断是否引用。
    """
    warning = (warning or "").strip()
    lines: list[str] = []
    for hit in hits:
        clock = format_local_time(hit.timestamp)
        line = f"- [{clock}] {hit.title}：{hit.content}"
        if (
            hit.sensitivity == SENSITIVITY_SOFT_SCOPED
            and (hit.origin_stream_id or "") != current_stream_id
            and warning
        ):
            line = f"{warning}\n{line}"
        lines.append(line)
    return lines


def time_decay_weight(age_days: float, decay_lambda: float) -> float:
    """按年龄计算时间衰减系数 ``exp(-λ·age)``。

    ``decay_lambda`` <= 0 时不衰减（恒为 1）；``age_days`` 为负时视为 0。
    """
    lam = max(0.0, float(decay_lambda))
    if lam <= 0.0:
        return 1.0
    return math.exp(-lam * max(0.0, float(age_days)))


def rerank_by_time_decay(
    hits: list[SemanticRecallHit],
    now: float,
    decay_lambda: float,
) -> list[SemanticRecallHit]:
    """按「相似度 × 时间衰减」重排命中列表（返回新列表，不改原列表）。

    - 相似度权重：``1 / (1 + distance)``，把距离（越小越相似）映射到
      单调递增的 (0, 1] 相似度区间；
    - 时间衰减：``exp(-λ·age_days)``，让近期记忆在相关性相近时优先；
    - ``timestamp`` <= 0（时间未知）按 ``age=0`` 处理，避免历史条目被
      无差别压底，保守信任相似度。
    """
    lam = max(0.0, float(decay_lambda))
    if lam <= 0.0:
        # 不启用衰减时保持原始（Chroma 相似度）顺序
        return list(hits)

    def score(hit: SemanticRecallHit) -> float:
        similarity = 1.0 / (1.0 + max(0.0, float(hit.distance)))
        ts = float(hit.timestamp or 0.0)
        age_days = 0.0
        if ts > 0.0:
            age_days = max(0.0, (now - ts) / 86400.0)
        return similarity * time_decay_weight(age_days, lam)

    return sorted(hits, key=score, reverse=True)


class StreamMemorySemanticRecall:
    """语义召回 thin service。

    ``embed_fn`` 与 ``vector_db`` 通过依赖注入传入：真实环境用 bge-m3 embedding
    与 ``get_vector_db_service``，测试环境可注入伪向量以避免依赖模型运行时。
    """

    def __init__(
        self,
        embed_fn: EmbedFn,
        vector_db: Any,
        collection_name: str,
    ) -> None:
        self._embed_fn = embed_fn
        self._vector_db = vector_db
        self._collection_name = collection_name

    async def recall_hits(
        self,
        text: str,
        current_stream_id: str,
        top_k: int = 5,
        *,
        decay_lambda: float = 0.0,
        now: float | None = None,
    ) -> list[SemanticRecallHit]:
        """执行 embedding -> query -> 敏感过滤 -> （可选）时间衰减重排 -> 截断。

        供调用方在拼注入文本前做去重等处理（如排除精确召回已注入的条目）。
        ``decay_lambda`` > 0 时按「相似度 × 时间衰减」重排，让近期记忆在
        相关性相近时优先，避免语义相似但久远的记忆挤占名额。
        """
        vector = await self._embed_fn(text)
        # 多召回若干再过滤，避免 hard_scoped 跨流阻断后不足 top_k 条。
        fetch_k = max(1, top_k * 3)
        raw = await self._vector_db.query(
            self._collection_name,
            query_embeddings=[vector],
            n_results=fetch_k,
        )
        hits = apply_sensitivity_scope(parse_query_hits(raw), current_stream_id)
        if decay_lambda > 0.0:
            current = now if now is not None else time.time()
            hits = rerank_by_time_decay(hits, current, decay_lambda)
        return hits[: max(0, top_k)]

    async def recall(
        self,
        text: str,
        current_stream_id: str,
        top_k: int = 5,
        warning: str = "",
        *,
        decay_lambda: float = 0.0,
        now: float | None = None,
    ) -> str:
        """执行完整闭环：embedding -> query -> 敏感过滤 -> 时间衰减 -> 注入文本。

        Args:
            text: 当前需要召回上下文的文本（通常为最近消息拼接）。
            current_stream_id: 当前发起召回的聊天流 ID，用于 hard_scoped 跨流过滤。
            top_k: 最终注入条数上限。
            warning: soft_scoped 跨流条目附加的警示前缀。
            decay_lambda: 时间衰减系数（每天），0 表示不衰减。
            now: 当前时间戳（测试可注入，默认取真实时间）。

        Returns:
            注入文本（多行，可能为空字符串）。
        """
        hits = await self.recall_hits(
            text,
            current_stream_id,
            top_k,
            decay_lambda=decay_lambda,
            now=now,
        )
        return "\n".join(build_injection_lines(hits, current_stream_id, warning))