# Stream Memory 三层流式记忆管理后台

三层记忆系统插件，提供**摘要层 / 新闻层 / 人物层**的分级记忆管理，内置可视化后台面板，支持记忆的编辑、删除、备份导出与手动触发整理。

作者：可可 | 版本：0.1.0

## 功能特性

- **三层记忆架构**：摘要（流级快照）到新闻（跨流总结）再到人物（背景信息），逐层提炼，读写分离
- **三级敏感标记**：hard_scoped / soft_scoped / normal，跨流召回时按标记执行过滤或带约束放行
- **Bot 合一**：私聊与群聊共用同一记忆域，新闻与人物画像双向可达，hard_scoped 仍锁定来源流
- **可视化管理后台**：Web 面板查看/编辑/删除记忆，支持手动触发整理任务
- **回复前记忆注入**：根据对话中出现的人物，自动注入相关新闻与人物背景
- **本地 JSON 持久化**：数据落盘为 JSON 文件，无需额外数据库

## 技术栈

| 层 | 技术 |
|------|------|
| 运行时 | Python 3.10+ · Neo-MoFox Core 1.2.0+（插件系统） |
| 后端 API | FastAPI + Pydantic v2（管理后台 REST 接口） |
| 前端面板 | Vue 3（CDN 引入，单文件 HTML，免构建） |
| 任务调度 | asyncio 周期任务（统一调度器），分层整理 |
| LLM 集成 | 子代理调用（默认 tool_use 任务）：摘要提炼、新闻整理、敏感分级、人物画像融合 |
| 记忆注入 | 框架 prompt_api stream reminder 机制，回复前实时拾取 |
| 数据存储 | 本地 JSON 文件（summaries.json / news.json / personas.json），无外部数据库 |

## 三层记忆架构

| 层级 | 说明 | 默认间隔 |
|------|------|----------|
| 摘要层 | 从聊天流生成摘要，按流持久化（群聊 + 私聊） | 30 分钟 |
| 新闻层 | 从摘要整理总结性记忆条目，巩固时做敏感分级 | 120 分钟 |
| 人物层 | 从整理出的新闻中提取人物背景，按人聚合 | 随新闻整理触发（每轮最多 N 人） |

## Bot 合一模式

私聊与群聊共享同一记忆域（摘要 / 新闻 / 人物画像），不做物理隔离：

- 私聊流同样产生摘要与新闻，参与人物画像建档
- 群聊与私聊的 normal / soft_scoped 记忆在涉及同一人物时可双向召回
- hard_scoped 记忆仍锁定来源流，跨流物理不可达（隐私兜底）
- 如需恢复隔离，可将 injection.allow_private_news / allow_private_personas 设为 false（逃生开关）

## 三级敏感标记

- **hard_scoped**：仅来源群内可召回，绝不跨群
- **soft_scoped**：跨群可召回，但注入时附加警示前缀，由 LLM 语境判断
- **normal**：自由召回

## 安装部署

将 stream_memory 文件夹放入项目的 plugins 目录即可，插件管理器会自动加载并注册周期任务与路由。

## 配置说明

| 区段 | 关键项 | 默认值 | 说明 |
|------|--------|--------|------|
| plugin | enabled / run_on_start | true / false | 插件开关、启动立即执行 |
| storage | data_dir | data/stream_memory | 记忆数据目录 |
| llm | summary_task / news_task / persona_task | tool_use | 各层子 agent 模型任务名 |
| summary | interval_minutes | 30 | 摘要生成间隔（分钟） |
| summary | max_entries_per_group | 50 | 每群摘要条目上限 |
| summary | max_messages_per_run | 300 | 单群单次读取消息上限 |
| news | interval_minutes | 120 | 新闻整理间隔（分钟） |
| news | max_entries | 50 | 新闻条目总数上限 |
| news | max_input_summaries | 100 | 单群单次整理读取摘要上限 |
| persona | max_text_length | 2000 | 单个人物背景文本上限 |
| persona | max_updates_per_round | 3 | 每轮新闻整理后最多更新的画像人数 |
| injection | inject_news / inject_personas | true / true | 是否注入新闻/人物记忆 |
| injection | allow_private_news | true | 是否允许新闻注入私聊（合一模式默认开启） |
| injection | allow_private_personas | true | 是否允许人物画像注入私聊（合一模式默认开启） |
| injection | news_max_inject / persona_max_inject | 5 / 5 | 单次回复注入条数上限 |
| injection | inject_soft_scoped | true | 是否注入软敏感跨群记忆 |
| sensitivity | enabled | true | 是否启用三级敏感标记 |
| sensitivity | classify_task | tool_use | 敏感分级子 agent 模型任务 |

## 管理后台

访问路由：/stream-memory-admin

主要 API：

- GET /api/dashboard：面板总览（摘要/新闻/人物统计）
- POST /api/persona/update、/api/persona/delete：人物信息编辑与删除
- POST /api/summary/toggle、/api/summary/delete-group：摘要启用切换、整群删除
- POST /api/news/delete、/api/news/update：新闻删除与编辑
- POST /api/trigger/summary、/api/trigger/news：手动触发整理任务
- 后台页「导出 JSON 备份」：前端本地导出 summaries/news/personas 全量数据

## 数据存储

默认存放在 data/stream_memory/ 下：

- summaries.json：摘要层数据
- news.json：新闻层数据
- personas.json：人物层数据

## 注意事项

- LLM 相关任务（summary/news/persona/classify）默认使用 tool_use 任务，请确保该任务在 model.toml 中已配置
- 合一模式下私聊与群聊记忆互相可达（normal / soft_scoped），hard_scoped 仍锁定来源流
- 达到条目上限时会自动删除最早的一条
