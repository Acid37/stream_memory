# Stream Memory 三层流式记忆管理后台

三层记忆系统插件，提供**摘要层 / 新闻层 / 人物层**的分级记忆管理，内置可视化后台面板，支持记忆的编辑、删除、备份导出与手动触发整理。

作者：可可 | 版本：0.3.0

## 功能特性

- **三层记忆架构**：摘要（流级快照）到新闻（跨流总结）再到人物（背景信息），逐层提炼，读写分离
- **三级敏感标记**：hard_scoped / soft_scoped / normal，跨流召回时按标记执行过滤或带约束放行
- **Bot 合一**：私聊与群聊共用同一记忆域，新闻与人物画像双向可达，hard_scoped 仍锁定来源流
- **长期记忆晋升判官**：新闻进入人物画像前先过一次判官，只有身份、长期偏好、未完成承诺才允许晋升，闲聊不再固化
- **寿命淘汰器**：判官同时给出记忆寿命，过期条目会被物理删除且不再注入；从未判官的历史数据不会被误删
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
| LLM 集成 | 子代理调用（默认 tool_use 任务）：摘要提炼、新闻整理、敏感分级、晋升判官、人物画像融合 |
| 记忆注入 | 框架 prompt_api stream reminder 机制，回复前实时拾取 |
| 数据存储 | 本地 JSON 文件（summaries.json / news.json / personas.json），无外部数据库 |

## 三层记忆架构

| 层级 | 说明 | 默认间隔 |
|------|------|----------|
| 摘要层 | 从聊天流生成摘要，按流持久化（群聊 + 私聊） | 30 分钟 |
| 新闻层 | 从摘要整理总结性记忆条目，巩固时做敏感分级 | 120 分钟 |
| 人物层 | 从整理出的新闻中提取人物背景，按人聚合（先经晋升判官筛选） | 随新闻整理触发（每轮最多 N 人） |

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

> 敏感度只表示**可见范围**，不表示重要性或寿命。hard_scoped 不因为“更敏感”就更容易进入长期人物画像。

## 长期记忆晋升判官

新闻层落库后、人物画像更新前，会先跑一次晋升判官（`llm.promotion_task`，提示词注册名 `stream_memory.promotion`），为每条新闻标注 `memory_kind` 与是否允许进入人物画像。

分类：`reject`（无保存价值） / `transient`（1～3 天） / `episode`（7～30 天） / `commitment`（未完成承诺，完成前保留） / `stable_fact`（身份、昵称、明确声明的长期偏好与边界）。

晋升需同时满足三个条件（与关系，任一不满足即不晋升）：

1. 判官返回 `persona_eligible=true`
2. `memory_kind` ∈ {`stable_fact`, `commitment`}
3. `sensitivity` ≠ `hard_scoped`

判官调用失败、返回为空或 JSON 不可解析时，本轮所有条目按**不晋升**处理（fail-closed）——宁可丢掉一次闲聊，也不要把错误印象永久固化。判官还会写入 `importance` / `confidence` / `ttl_days` / `scope` / `promotion_reason`，供后台审计与寿命淘汰使用。

### 记忆寿命与淘汰

判官裁决在**新闻落库之前**执行，所以 `memory_kind`、`ttl_days`、`judged_at` 等结论会随条目一起写进 `news.json`，后台面板可见。

寿命优先级：判官给出的 `ttl_days` > `memory_kind` 对应的默认寿命（见 `news.retention_days_*`）；两者都为 0 表示不过期。计时基准是**落库时间**（`consolidated_at`），避免「很久以前的事件」刚被整理成新闻就立刻过期。

两处生效：

1. **召回侧**（`store.get_recallable_news`）：过期条目直接跳过，即使淘汰任务还没跑，过期闲聊也不会进入回复；
2. **淘汰侧**（`job._expire_news`，每轮新闻整理开始时）：物理删除过期条目并同步清理语义向量，统计写入 `stats["expired"]`。

**从未判官的条目（`judged_at = 0`）永不淘汰**，历史数据不会因为插件升级被清空。

## 安装部署

将 stream_memory 文件夹放入项目的 plugins 目录即可，插件管理器会自动加载并注册周期任务与路由。

## 配置说明

| 区段 | 关键项 | 默认值 | 说明 |
|------|--------|--------|------|
| plugin | enabled / run_on_start | true / false | 插件开关、启动立即执行 |
| storage | data_dir | data/stream_memory | 记忆数据目录 |
| llm | summary_task / news_task / persona_task | tool_use | 各层子 agent 模型任务名 |
| llm | promotion_task | tool_use | 新闻进入人物画像前的晋升判官任务名 |
| summary | interval_minutes | 30 | 摘要生成间隔（分钟） |
| summary | max_entries_per_group | 50 | 每群摘要条目上限 |
| summary | max_messages_per_run | 300 | 单群单次读取消息上限 |
| news | interval_minutes | 120 | 新闻整理间隔（分钟） |
| news | max_entries | 50 | 新闻条目总数上限 |
| news | max_input_summaries | 100 | 单群单次整理读取摘要上限 |
| news | retention_enabled | true | 是否启用寿命淘汰（关闭后过期条目仍不会被召回，但不会被删除） |
| news | retention_days_reject / _transient / _episode | 1 / 3 / 30 | reject / transient / episode 的默认寿命（天） |
| news | retention_days_commitment / _stable_fact | 0 / 0 | commitment / stable_fact 的默认寿命（0 = 不过期） |
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

- LLM 相关任务（summary/news/persona/promotion/classify）默认使用 tool_use 任务，请确保该任务在 model.toml 中已配置
- 合一模式下私聊与群聊记忆互相可达（normal / soft_scoped），hard_scoped 仍锁定来源流
- 达到条目上限时会自动删除最早的一条
- 人物画像只接受判官放行的 `stable_fact` / `commitment` 条目；一次性话题不会再写进画像
- 新闻寿命由判官结论决定，判官未给出时按 `news.retention_days_*` 的 `memory_kind` 默认值；模型漏判的条目按 `episode`（默认 30 天）处理
- 判官整体失败（空响应/不可解析）时条目不进画像、也不参与淘汰，靠 `news.max_entries` 上限兜底

## 变更留痕

### 2026-09-25 · 0.3.0（判官结论持久化 + 寿命淘汰器）

承接 0.2.0 留下的两条边界：判官结论没落库、`ttl_days` 没有淘汰器消费。

代码改动：

- `job.py`：晋升判官从「本轮全部群聊批量判定」前移到 `_news_for_group` 内、**落库之前**逐群执行
  - 结论因此随 `NewsEntry` 一起写入 `news.json`（原本判官在落库后跑，结论只活在内存里，后台看不到）
  - 顺带消掉两个副作用：不再用假的 `stream_memory_news_batch` 当 stream_id；某个群判官失败不再连带整轮所有群都不晋升（改为逐群隔离）
  - `run_news_job` 退化为只汇总 `persona_eligible=True` 的条目统一更新画像
- `models.py`：`NewsEntry` 新增 `judged_at`（判官裁决时间，0 = 从未判官），`to_dict` / `from_dict` 双向兼容
- `utils.py`：新增寿命判定 `retention_enabled` / `entry_ttl_days` / `entry_age_seconds` / `is_entry_expired`
  - 寿命优先级：判官 `ttl_days` > `memory_kind` 默认值
  - 计时基准为 `consolidated_at`（落库时间），避免很久以前的事件一整理就立刻过期
  - 从未判官的条目恒不淘汰
- `store.py`：`get_recallable_news` 跳过过期条目（召回侧兜底）
- `job.py`：新增 `_expire_news()`，每轮新闻整理开始时物理删除过期条目并清理对应语义向量，写进 `stats["expired"]`
- `config.py` / `config.toml`：`news` 段新增 `retention_enabled` 与五档 `retention_days_*`（reject 1 / transient 3 / episode 30 / commitment 0 / stable_fact 0，0 = 不过期）
- `stream_memory_admin.html`：新闻卡片显示「判官: episode · 默认寿命」「已入画像」「未判官（不淘汰）」

设计取舍：

- 判官跑过即给整批打 `judged_at`：模型**漏判**的条目退回默认 `episode`（30 天）寿命，而不是因为漏判变成永久记忆
- 判官**整体失败**（空响应/不可解析）不打标：这类条目不进画像也不被淘汰，避免误删，条目上限淘汰仍然兜底
- reject 类闲聊不做「立即删除」，只给 1 天寿命——判官可能误判，留一条退路，也可用 `scripts/cleanup_stream_memory.py` 手动清理

验证：

- `pytest test/plugins/stream_memory` → **50 passed**（新增 `test_news_retention.py` 12 例：未判官不过期 / 判官 ttl 优先 / kind 默认寿命 / stable_fact 与 commitment 不过期 / 以落库时间计时 / 关闭淘汰 / 召回跳过过期 / 淘汰器删除与返回 / 关闭时不动作 / 全批判官打标 / 判官失败不打标 / 结论写入 news.json 并可读回）
- `_news_for_group` 新增 `promotion_task` 形参，同步修正 `test_timestamp_pipeline.py` 的调用与假判官响应
- 配置模型默认值与 `config.toml` 一致，`load_manifest` 通过（版本 0.3.0）

仍待下一步：

- 人物画像自身没有 TTL / 证据计数：判官闸门只拦新增，已固化的画像仍需手动清理
- `importance` / `confidence` 目前只用于审计，未参与淘汰权重

### 2026-09-25 · 0.2.0（长期记忆晋升判官 + 数据收口）

起因：人物画像会把一次性的闲聊（例如某次项目讨论）当作长期事实固化，且画像没有 TTL 与淘汰机制；同时 `hard_scoped` 被当成“最高级重要性”，敏感度与记忆寿命被混成一个概念。

代码改动：

- `models.py`：`NewsEntry` 新增 `memory_kind / persona_eligible / importance / confidence / ttl_days / scope / promotion_reason`，`to_dict` / `from_dict` 双向兼容（旧数据缺字段时回退默认值，`persona_eligible` 默认 `false`）
- `prompts.py`：新增 `stream_memory.promotion` 判官提示词，明确硬规则：一次性项目讨论、技术闲聊、寒暄、表情包、玩笑、临时情绪/健康/作息、第三方评价与人格推断、未标记作用域的角色扮演，一律不得进入人物画像
- `job.py`：新增 `_judge_persona_promotion()`；`run_news_job` 在本轮新闻落库后先跑判官，只有 `persona_eligible=True` 的条目才进入 `_update_personas_from_news`
- `config.py` / `config.toml`：新增 `llm.promotion_task`（默认 `tool_use`）

运行态收口（数据与配置，非代码）：

- `data/stream_memory/personas.json`：36 → 0，清空已固化的历史画像，避免错误印象继续参与回复
- `data/stream_memory/news.json`：50 → 12，保留明确任务、技术项目、创作内容、待办，删除寒暄/表情/天气快照/角色扮演流水账
- 清理前完整备份：`data/stream_memory_backups/20260925_204539`（8 个文件约 1.9 MB，含原 JSON 与原向量库）；同逻辑可用 `scripts/cleanup_stream_memory.py` 复现（执行前自动备份，不静默覆盖）
- `config/plugins/stream_memory/config.toml`：`injection.inject_personas=false`、`semantic.enabled=false`、`news_max_inject=2`、`persona_max_inject=1`、`person_scan_history_limit=3`，敏感分级保持开启

验证：

- `pytest test/plugins/stream_memory` → **38 passed**（含新增 `test_persona_promotion.py` 3 例：拒绝一次性项目闲聊 / 接受明确的长期偏好 / hard_scoped 永不自动晋升）
- 同步修正 `test_e2e_recall_inject.py` 两条失效断言：原断言用向量库元数据标题“家事”去匹配**新闻层**文本，而新闻层标题是“老张家里出事”，导致同流放行那条永久失败、跨流阻断那条恒真（恒真即没测到东西）
- 新增字段 JSON 往返验证通过：写入 `news.json` 后重建 store 可完整读回判官字段
- `load_manifest('plugins/stream_memory')` 通过，版本 0.2.0，service / event_handler / router / tool 四个组件声明完整

已知边界（下一步方向）：

- 判官结论目前只作用于本轮画像闸门，**尚未回写 `news.json`**（新闻条目在判官执行前就已落库），所以后台面板暂时看不到 `memory_kind` 等字段；若要持久化，应在落库前判官，或补一个按 ID 回写判官结论的 store 方法
- `ttl_days` / `importance` 已记录但还没有淘汰器消费，新闻层仍然只按 `news.max_entries` 淘汰最早条目
- 判官一次批量调用（跨群合并），失败即整轮不晋升；后续可考虑按群拆分以便局部失败不影响全部
