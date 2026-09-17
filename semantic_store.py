"""Stream Memory 语义向量写入侧（与 semantic_recall 召回侧对称）。

在新闻巩固（``run_news_job``）落盘 JSON 的同时，把 NewsEntry 同步 upsert 进
Chroma 集合，供语义召回通道检索；被淘汰的新闻同步删除，避免向量库残留
「幽灵」条目（JSON 已删、向量仍可被召回）。

embed_fn 通过依赖注入传入：生产环境用 :func:`make_embedding_fn`（真实 bge-m3），
测试环境注入伪向量，避免依赖模型运行时与 API Key。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Sequence

from .models import NewsEntry

# 批量文本 -> 批量稠密向量
EmbedBatchFn = Callable[[list[str]], Awaitable[list[list[float]]]]


def news_embedding_text(entry: NewsEntry) -> str:
    """新闻条目的向量化文本：标题 + 内容，与注入行格式保持一致。"""
    title = entry.title or ""
    content = entry.content or ""
    return f"{title}：{content}"


def make_embedding_fn() -> EmbedBatchFn:
    """返回真实 bge-m3 批量 embedding 函数（依赖运行时已初始化 model config）。"""
    from src.app.plugin_system.api.llm_api import (
        create_embedding_request,
        get_model_set_by_task,
    )

    model_set = get_model_set_by_task("embedding")

    async def embed(texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        request = create_embedding_request(
            model_set,
            request_name="stream_memory_semantic_upsert",
            inputs=list(texts),
        )
        response = await request.send()
        return response.embeddings or []

    return embed


def make_single_embedding_fn() -> Callable[[str], Awaitable[list[float]]]:
    """返回「单条文本 -> 单条向量」的 embedding 函数（语义召回查询侧）。

    复用与写入侧 :func:`make_embedding_fn` 相同的 bge-m3 模型与向量空间，
    单条查询命中批量接口后取第一条向量。
    """
    batch_fn = make_embedding_fn()

    async def embed(text: str) -> list[float]:
        if not text:
            return []
        vectors = await batch_fn([text])
        return vectors[0] if vectors else []

    return embed


async def upsert_news_entries(
    vector_db: Any,
    collection_name: str,
    embed_fn: EmbedBatchFn,
    entries: Sequence[NewsEntry],
) -> None:
    """批量向量化并 upsert 新闻条目到向量库。

    metadata 携带敏感三级过滤所需的 ``sensitivity`` / ``origin_stream_id`` 与
    ``timestamp`` / ``title``，供召回阶段做跨流可见性过滤与文本还原。
    """
    if not entries:
        return
    # 向量化用「标题：内容」（语义更完整）；documents 存纯 content，
    # 供召回阶段注入文本还原时与 metadata 的 title 拼装，避免「标题：标题：内容」重复。
    embed_texts = [news_embedding_text(entry) for entry in entries]
    documents = [entry.content or "" for entry in entries]
    embeddings = await embed_fn(embed_texts)
    await vector_db.add(
        collection_name=collection_name,
        embeddings=embeddings,
        documents=documents,
        metadatas=[
            {
                "title": entry.title or "",
                "timestamp": entry.timestamp,
                "sensitivity": entry.sensitivity or "normal",
                "origin_stream_id": entry.origin_stream_id or "",
            }
            for entry in entries
        ],
        ids=[entry.id for entry in entries],
    )


async def remove_news_entries(
    vector_db: Any,
    collection_name: str,
    ids: Sequence[str],
) -> None:
    """从向量库删除指定新闻条目（用于新闻被淘汰后清理）。"""
    id_list = [str(item) for item in ids if item]
    if not id_list:
        return
    await vector_db.delete(collection_name=collection_name, ids=id_list)