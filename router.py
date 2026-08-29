"""Stream Memory 管理后台 Router。"""

from __future__ import annotations

import traceback
from typing import TYPE_CHECKING, Any
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from src.core.components.base.router import BaseRouter
from .service import StreamMemoryService
from .store import shared_store

if TYPE_CHECKING:
    from src.core.components.base.plugin import BasePlugin


class UpdatePersonaPayload(BaseModel):
    """更新人物背景请求体。"""

    person_id: str = Field(..., description="人物 ID")
    text: str = Field(..., description="背景描述")


class DeletePersonaPayload(BaseModel):
    """删除人物背景请求体。"""

    person_id: str = Field(..., description="人物 ID")


class ToggleSummaryPayload(BaseModel):
    """切换摘要条目废弃状态请求体。"""

    stream_id: str = Field(..., description="聊天流 ID")
    entry_index: int = Field(..., description="摘要索引")
    deprecated: bool = Field(..., description="是否废弃")


class UpdateSummaryPayload(BaseModel):
    """更新摘要条目正文请求体。"""

    stream_id: str = Field(..., description="聊天流 ID")
    entry_index: int = Field(..., description="摘要索引")
    content: str = Field(..., description="新的摘要正文")


class UpdateNewsPayload(BaseModel):
    """更新新闻条目请求体。"""

    news_id: str = Field(..., description="新闻条目 ID")
    title: str = Field(default="", description="标题")
    content: str = Field(..., description="正文内容")
    sensitivity: str = Field(default="normal", description="敏感等级")
    person_ids: list[str] = Field(
        default_factory=list,
        description="参与人物 ID 列表（platform:user_id 格式），空列表表示清空参与者",
    )


class DeleteGroupPayload(BaseModel):
    """删除群聊分组请求体。"""

    stream_id: str = Field(..., description="聊天流 ID")


class DeleteNewsPayload(BaseModel):
    """删除新闻条目请求体。"""

    news_ids: list[str] = Field(..., description="要删除的新闻条目 ID 列表")


class StreamMemoryRouter(BaseRouter):
    """Stream Memory 管理后台路由。"""

    router_name: str = "stream_memory"
    router_description: str = "Stream Memory 三层记忆大脑控制中心"
    custom_route_path: str = "/stream-memory-admin"
    cors_origins: list[str] = ["*"]

    def __init__(self, plugin: "BasePlugin") -> None:
        self._service: StreamMemoryService | None = None
        super().__init__(plugin)

    def _get_service(self) -> StreamMemoryService:
        if self._service is None:
            self._service = StreamMemoryService(plugin=self.plugin)
        return self._service

    def _get_store(self):
        return shared_store(self.plugin, lambda: self.plugin.config)

    @staticmethod
    def _html_path() -> Path:
        """获取 HTML 文件路径。"""
        return Path(__file__).with_name("stream_memory_admin.html")

    @classmethod
    def _load_html(cls) -> str:
        """读取 HTML 页面。"""
        try:
            return cls._html_path().read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError("stream_memory_admin.html 不存在，无法加载管理后台") from exc

    def register_endpoints(self) -> None:
        """注册页面及管理接口。"""

        @self.app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def dashboard_page() -> HTMLResponse:
            resp = HTMLResponse(self._load_html())
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return resp

        @self.app.get("/api/dashboard")
        async def get_dashboard_data() -> dict[str, Any]:
            try:
                service = self._get_service()
                store = self._get_store()
                
                # 获取数据
                summaries = await service.get_group_summaries()
                news = await service.get_news_entries()
                personas = await service.get_all_personas()
                
                # 统计健康状况
                import os
                storage_cfg = self.plugin.config.storage
                data_dir = getattr(storage_cfg, "data_dir", "data/stream_memory")
                
                size_summaries = 0
                size_news = 0
                size_personas = 0
                try:
                    if os.path.exists(os.path.join(data_dir, "summaries.json")):
                        size_summaries = os.path.getsize(os.path.join(data_dir, "summaries.json"))
                    if os.path.exists(os.path.join(data_dir, "news.json")):
                        size_news = os.path.getsize(os.path.join(data_dir, "news.json"))
                    if os.path.exists(os.path.join(data_dir, "personas.json")):
                        size_personas = os.path.getsize(os.path.join(data_dir, "personas.json"))
                except Exception:
                    pass

                total_size_kb = round((size_summaries + size_news + size_personas) / 1024, 2)

                # 按时间降序对新闻排序
                news.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

                return {
                    "status": "success",
                    "summaries": summaries,
                    "news": news,
                    "personas": personas,
                    "health": {
                        "total_size_kb": total_size_kb,
                        "summaries_count": len(summaries),
                        "news_count": len(news),
                        "personas_count": len(personas),
                        "last_sync": store._last_watch_check_at if hasattr(store, "_last_watch_check_at") else None
                    }
                }
            except Exception as e:
                err_trace = traceback.format_exc()
                with open("stream_memory_debug.log", "w", encoding="utf-8") as f:
                    f.write(err_trace)
                return {
                    "status": "error",
                    "message": str(e),
                    "traceback": err_trace
                }

        @self.app.post("/api/persona/update")
        async def update_persona(payload: UpdatePersonaPayload) -> dict[str, Any]:
            try:
                store = self._get_store()
                await store.set_persona(payload.person_id, payload.text)
                return {"status": "success", "message": f"人物 {payload.person_id} 已更新"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/api/persona/delete")
        async def delete_persona(payload: DeletePersonaPayload) -> dict[str, Any]:
            try:
                store = self._get_store()
                await store.delete_persona(payload.person_id)
                return {"status": "success", "message": f"人物 {payload.person_id} 已从大脑中忘却"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/api/summary/toggle")
        async def toggle_summary(payload: ToggleSummaryPayload) -> dict[str, Any]:
            try:
                store = self._get_store()
                ok = await store.toggle_summary_deprecated(payload.stream_id, payload.entry_index, payload.deprecated)
                if not ok:
                    raise HTTPException(status_code=400, detail="未找到对应的摘要条目")
                return {"status": "success"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/api/summary/delete-group")
        async def delete_group(payload: DeleteGroupPayload) -> dict[str, Any]:
            try:
                store = self._get_store()
                ok = await store.remove_group(payload.stream_id)
                if not ok:
                    raise HTTPException(status_code=400, detail="未找到对应的群聊分组")
                return {"status": "success"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/api/news/delete")
        async def delete_news(payload: DeleteNewsPayload) -> dict[str, Any]:
            try:
                store = self._get_store()
                removed = await store.remove_news(payload.news_ids)
                return {"status": "success", "count": len(removed)}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/api/trigger/summary")
        async def trigger_summary_job() -> dict[str, Any]:
            try:
                from .job import run_summary_job
                stats = await run_summary_job(self.plugin)
                return {"status": "success", "stats": stats}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self.app.post("/api/trigger/news")
        async def trigger_news_job() -> dict[str, Any]:
            try:
                from .job import run_news_job
                stats = await run_news_job(self.plugin)
                return {"status": "success", "stats": stats}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        @self.app.post("/api/summary/update")
        async def update_summary(payload: UpdateSummaryPayload) -> dict[str, Any]:
            try:
                store = self._get_store()
                ok = await store.update_summary_entry(
                    payload.stream_id, payload.entry_index, payload.content
                )
                if not ok:
                    raise HTTPException(status_code=400, detail="未找到对应的摘要条目或正文为空")
                return {"status": "success"}
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/api/news/update")
        async def update_news(payload: UpdateNewsPayload) -> dict[str, Any]:
            try:
                store = self._get_store()
                ok = await store.update_news_entry(
                    payload.news_id,
                    payload.title,
                    payload.content,
                    payload.sensitivity,
                    payload.person_ids,
                )
                if not ok:
                    raise HTTPException(status_code=400, detail="未找到对应的新闻条目或正文为空")
                return {"status": "success"}
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
