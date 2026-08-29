"""Stream Memory 插件入口。

三层记忆系统（读写分离、三级敏感标记）：
- 摘要层：周期事件（默认 30 分钟）从群聊聊天流生成摘要，按群持久化。
- 新闻层：周期事件（默认 2 小时）从所有群聊摘要整理总结性记忆条目。
  巩固时对本组全部新闻条目批量做三级敏感分级（hard_scoped / soft_scoped / normal），
  并记录来源群聊 ID（origin_stream_id），供召回时跨群过滤。
- 人物层：新闻条目被删除时增量维护本地人物背景信息表。

回复前根据 unread message 中出现的人物，注入相关新闻与人物背景。
召回时按三级敏感标记执行跨群过滤或带约束放行（hard_scoped 仅来源群内召回，
soft_scoped 跨群带警示，normal 自由召回）。
"""

from __future__ import annotations

import asyncio

from src.app.plugin_system.api import log_api, prompt_api
from src.app.plugin_system.base import BasePlugin, register_plugin
from src.app.plugin_system.types import PromptTemplate
from src.kernel.concurrency import get_task_manager

from .config import StreamMemoryConfig
from .event_handler import StreamMemoryRecallInjector
from .job import run_news_job, run_summary_job
from .prompts import PROMPT_TEMPLATES
from .service import StreamMemoryService
from .store import shared_store

logger = log_api.get_logger("stream_memory.plugin")


@register_plugin
class StreamMemoryPlugin(BasePlugin):
    """Stream Memory 三层记忆系统插件。"""

    plugin_name: str = "stream_memory"
    plugin_description: str = "三层记忆系统 with 读写分离、三级敏感标记"
    plugin_version: str = "0.1.0"

    configs: list[type] = [StreamMemoryConfig]
    dependent_components: list[str] = []

    def __init__(self, config: StreamMemoryConfig | None = None) -> None:
        super().__init__(config)
        self._schedule_ids: list[str] = []
        self._register_task_id: str | None = None
        self._job_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 组件
    # ------------------------------------------------------------------

    def get_components(self) -> list[type]:
        """返回插件组件类。"""
        from .router import StreamMemoryRouter
        from .tool import BrowseGroupNewsTool
        return [
            StreamMemoryService,
            StreamMemoryRecallInjector,
            StreamMemoryRouter,
            BrowseGroupNewsTool,
        ]

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_plugin_loaded(self) -> None:
        """插件加载完成后：注册提示词模板、共享存储并启动文件监视。"""
        for name, template in PROMPT_TEMPLATES.items():
            try:
                prompt_api.register_template(PromptTemplate(name=name, template=template))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"注册提示词模板失败 {name}: {exc}")

        # 预创建共享 store 单例（所有周期任务/注入器/服务共用），并监视本地文件变化
        store = shared_store(
            self,
            lambda: (
                self.config
                if isinstance(self.config, StreamMemoryConfig)
                else StreamMemoryConfig()
            ),
        )
        try:
            await store.start_watcher()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"启动记忆文件监视失败: {exc}")

        tm = get_task_manager()
        task = tm.create_task(
            self._register_schedules_when_ready(),
            name="stream_memory_register_schedule",
            daemon=True,
        )
        self._register_task_id = task.task_id

    async def on_plugin_unloaded(self) -> None:
        """插件卸载前：移除周期事件、停止文件监视并取消后台注册任务。"""
        from src.kernel.scheduler import get_unified_scheduler

        store = getattr(self, "_memory_store", None)
        if store is not None:
            try:
                await store.close()
            except Exception:  # noqa: BLE001
                pass

        scheduler = get_unified_scheduler()
        for schedule_id in list(self._schedule_ids):
            try:
                await scheduler.remove_schedule(schedule_id)
            except Exception:  # noqa: BLE001
                pass
        self._schedule_ids.clear()

        if self._register_task_id:
            try:
                get_task_manager().cancel_task(self._register_task_id)
            except Exception:  # noqa: BLE001
                pass
            self._register_task_id = None

    # ------------------------------------------------------------------
    # 周期事件注册
    # ------------------------------------------------------------------

    async def _register_schedules_when_ready(self) -> None:
        """等待 scheduler 运行后注册三个周期事件。"""
        from src.kernel.scheduler import TriggerType, get_unified_scheduler

        if not isinstance(self.config, StreamMemoryConfig):
            logger.warning("stream_memory config 未加载，无法注册 schedule")
            return

        scheduler = get_unified_scheduler()
        plans = [
            (
                "stream_memory_summary",
                int(self.config.summary.interval_minutes) * 60,
                self._run_summary_job,
            ),
            (
                "stream_memory_news",
                int(self.config.news.interval_minutes) * 60,
                self._run_news_job,
            ),
        ]

        for attempt in range(600):
            try:
                registered: list[str] = []
                for task_name, interval_seconds, callback in plans:
                    schedule_id = await scheduler.create_schedule(
                        callback=callback,
                        trigger_type=TriggerType.TIME,
                        trigger_config={"interval_seconds": interval_seconds},
                        is_recurring=True,
                        task_name=task_name,
                        force_overwrite=True,
                    )
                    registered.append(schedule_id)

                if bool(getattr(self.config.plugin, "run_on_start", False)):
                    once_id = await scheduler.create_schedule(
                        callback=self._run_all_jobs_once,
                        trigger_type=TriggerType.TIME,
                        trigger_config={"delay_seconds": 0},
                        is_recurring=False,
                        task_name="stream_memory_startup_once",
                        force_overwrite=True,
                    )
                    registered.append(once_id)

                self._schedule_ids = registered
                logger.info(
                    f"stream_memory 周期事件已注册: "
                    f"summary={plans[0][0]} news={plans[1][0]}"
                )
                return
            except RuntimeError:
                await asyncio.sleep(0.5)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"注册 stream_memory 周期事件失败: {exc}")
                await asyncio.sleep(2.0)

        logger.warning("等待 scheduler 就绪超时，stream_memory 周期事件未注册")

    # ------------------------------------------------------------------
    # 任务回调
    # ------------------------------------------------------------------

    async def _run_summary_job(self) -> None:
        """摘要更新事件回调。"""
        await self._run_job_locked("summary", run_summary_job)

    async def _run_news_job(self) -> None:
        """新闻记录事件回调。"""
        await self._run_job_locked("news", run_news_job)

    async def _run_all_jobs_once(self) -> None:
        """启动立即执行：依次运行两个任务。"""
        await self._run_summary_job()
        await self._run_news_job()

    async def _run_job_locked(self, name: str, job) -> None:
        """带互斥锁执行周期任务，防止重叠运行。"""
        lock = self._job_locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            logger.info(f"stream_memory {name} 任务已在运行，跳过本次")
            return
        try:
            async with lock:
                await job(self)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"stream_memory {name} 任务执行失败: {exc}", exc_info=True)
