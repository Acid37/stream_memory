"""Stream Memory 插件配置。

三层记忆系统（读写分离、三级敏感标记）：
- 摘要层（summary）：周期性从群聊聊天流生成摘要，按群分别持久化。
- 新闻层（news）：周期性读取所有群聊摘要，整理出总结性的记忆条目。
- 人物层（persona）：新闻条目被删除（上限淘汰）时增量维护人物背景信息。

设计要点：
- 三级敏感标记（sensitivity）：巩固时 LLM 做三级敏感判断（hard_scoped / soft_scoped / normal），
  召回时程序按标记执行过滤或带约束放行。
- 注入层软敏感跨群记忆控制（inject_soft_scoped + soft_scoped_warning）。
"""
from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class StreamMemoryConfig(BaseConfig):
    """Stream Memory 插件配置模型。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "Stream Memory 三层记忆系统配置"

    @config_section("plugin", title="插件设置", tag="plugin")
    class PluginSection(SectionBase):
        """插件级开关。"""

        enabled: bool = Field(
            default=True,
            description="是否启用插件",
            label="启用插件",
            tag="plugin",
        )
        run_on_start: bool = Field(
            default=False,
            description="插件加载后立即执行一次摘要/新闻任务",
            label="启动立即执行",
            tag="timer",
            hint="会立即产生 LLM 调用，建议仅在需要时开启",
        )

    @config_section("storage", title="存储配置", tag="database")
    class StorageSection(SectionBase):
        """本地 JSON 持久化配置。"""

        data_dir: str = Field(
            default="data/stream_memory",
            description="记忆数据目录（摘要/新闻/人物信息三个 JSON 数据库均存放于此）",
            label="数据目录",
            input_type="text",
            tag="file",
        )

    @config_section("llm", title="LLM 配置", tag="ai")
    class LLMSection(SectionBase):
        """内部子 agent 使用的模型任务配置。"""

        summary_task: str = Field(
            default="tool_use",
            description="摘要层子 agent 使用的模型任务名",
            label="摘要任务",
            placeholder="tool_use",
            tag="ai",
            hint="确保该任务在 model.toml 中已配置",
        )
        news_task: str = Field(
            default="tool_use",
            description="新闻层子 agent 使用的模型任务名",
            label="新闻任务",
            placeholder="tool_use",
            tag="ai",
        )
        persona_task: str = Field(
            default="tool_use",
            description="人物层子 agent 使用的模型任务名",
            label="人物任务",
            placeholder="tool_use",
            tag="ai",
        )

    @config_section("summary", title="摘要层配置", tag="timer")
    class SummarySection(SectionBase):
        """摘要层：从群聊聊天流生成摘要并持久化。"""

        interval_minutes: int = Field(
            default=30,
            description="摘要生成间隔（分钟）",
            label="摘要间隔（分钟）",
            ge=1,
            le=1440,
            tag="timer",
        )
        max_entries_per_group: int = Field(
            default=50,
            description="每个群聊的摘要条目数上限，达到上限时删除最早的一条",
            label="每群摘要上限",
            ge=1,
            le=500,
            tag="performance",
        )
        max_messages_per_run: int = Field(
            default=300,
            description="每次为单个群聊读取的最大消息条数",
            label="单群消息上限",
            ge=1,
            le=2000,
            tag="performance",
        )

    @config_section("news", title="新闻层配置", tag="timer")
    class NewsSection(SectionBase):
        """新闻层：从所有群聊摘要中整理总结性记忆条目。"""

        interval_minutes: int = Field(
            default=120,
            description="新闻整理间隔（分钟）",
            label="新闻间隔（分钟）",
            ge=1,
            le=1440,
            tag="timer",
        )
        max_entries: int = Field(
            default=50,
            description="新闻条目总数上限，达到上限时删除最早的一条",
            label="新闻条目上限",
            ge=1,
            le=500,
            tag="performance",
        )
        max_input_summaries: int = Field(
            default=100,
            description="单个群聊单次整理最多读取的摘要条数（按时间取最新的，不含已废弃条目）",
            label="单群摘要输入上限",
            ge=1,
            le=500,
            tag="performance",
            hint="参与处理的摘要会在整理后标记为废弃",
        )

    @config_section("persona", title="人物层配置", tag="ai")
    class PersonaSection(SectionBase):
        """人物层：新闻整理后对涉及人物的背景信息即时建档/增量更新。"""

        max_text_length: int = Field(
            default=2000,
            description="单个人物背景信息文本的最大长度",
            label="人物信息长度上限",
            ge=100,
            le=10000,
            tag="performance",
        )
        max_updates_per_round: int = Field(
            default=3,
            description="每轮新闻整理后最多更新的人物画像数（按本轮出现次数取最活跃的 N 人；0 表示不限制）",
            label="单轮画像更新上限",
            ge=0,
            le=20,
            tag="performance",
            hint="控制人物画像建档的 LLM 调用成本；活跃用户每轮都会优先建档",
        )

    @config_section("injection", title="回复前注入配置", tag="ai")
    class InjectionSection(SectionBase):
        """每次回复前根据 unread message 中出现的人物注入相关记忆。"""

        bucket: str = Field(
            default="actor",
            description="system reminder 写入的 bucket 名（需与 chatter 的 with_reminder 一致）",
            label="Reminder Bucket",
            placeholder="actor",
            tag="general",
            hint="default_chatter 使用 with_reminder=\"actor\"，写入流私有 bucket 后自动拾取",
        )
        inject_news: bool = Field(
            default=True,
            description="是否在回复前注入涉及当前人物的新闻记忆",
            label="注入新闻",
            tag="ai",
        )
        inject_personas: bool = Field(
            default=True,
            description="是否在回复前注入涉及当前人物的人物背景信息",
            label="注入人物信息",
            tag="ai",
        )
        allow_private_news: bool = Field(
            default=True,
            description="是否允许私聊接收跨流新闻记忆（Bot 合一，默认开启）；关闭则私聊不再注入任何新闻",
            label="私聊注入跨流新闻",
            tag="moderation",
            hint="合一模式下私聊与群聊共享记忆域，此开关仅作为需要重新隔离时的逃生门",
        )
        allow_private_personas: bool = Field(
            default=True,
            description="是否允许私聊注入人物背景（画像已统一存于 personas.json）；关闭则私聊不再注入人物画像",
            label="私聊注入人物画像",
            tag="moderation",
            hint="合一模式下群聊与私聊共用同一人物画像域",
        )
        news_max_inject: int = Field(
            default=5,
            description="单次回复最多注入的新闻条数",
            label="新闻注入上限",
            ge=1,
            le=20,
            tag="performance",
        )
        persona_max_inject: int = Field(
            default=5,
            description="单次回复最多注入的人物背景条数",
            label="人物注入上限",
            ge=1,
            le=20,
            tag="performance",
        )
        person_scan_history_limit: int = Field(
            default=20,
            description="收集当前对话人物时扫描的最近历史消息条数（unread 被 flush 后兜底）",
            label="人物扫描历史条数",
            ge=1,
            le=200,
            tag="performance",
        )
        inject_soft_scoped: bool = Field(
            default=True,
            description="是否注入软敏感跨群记忆（注入时附加警示前缀，由 LLM 语境判断是否引用）",
            label="注入软敏感记忆",
            tag="ai",
            hint="soft_scoped 记忆跨群可见但带约束，hard_scoped 记忆始终不跨群",
        )
        soft_scoped_warning: str = Field(
            default="[跨群敏感记忆：请谨慎参考，仅在自然相关时温和提及]",
            description="软敏感记忆跨群召回时附加的警示前缀文本",
            label="软敏感警示前缀",
            input_type="text",
            tag="general",
        )

    @config_section("sensitivity", title="敏感标记配置", tag="moderation")
    class SensitivitySection(SectionBase):
        """三级敏感标记：巩固时 LLM 做两级敏感判断，召回时程序按标记执行。"""

        enabled: bool = Field(
            default=True,
            description="是否启用三级敏感标记",
            label="启用敏感标记",
            tag="moderation",
            hint="关闭后所有记忆按普通级处理，跨群自由召回",
        )
        classify_task: str = Field(
            default="tool_use",
            description="敏感分级子 agent 使用的模型任务名",
            label="敏感分级任务",
            placeholder="tool_use",
            tag="ai",
            hint="巩固时调用，对每条新闻做 hard_scoped / soft_scoped / 无标记 三级判断",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    storage: StorageSection = Field(default_factory=StorageSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    summary: SummarySection = Field(default_factory=SummarySection)
    news: NewsSection = Field(default_factory=NewsSection)
    persona: PersonaSection = Field(default_factory=PersonaSection)
    injection: InjectionSection = Field(default_factory=InjectionSection)
    sensitivity: SensitivitySection = Field(default_factory=SensitivitySection)
