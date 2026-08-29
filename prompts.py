"""Stream Memory 子 agent 提示词模板。

模板会注册到 prompt_api，可在运行时被查询/覆盖；
sub_agent 优先读取 prompt_api 中已注册的模板，缺失时回退到本文件常量。
"""

from __future__ import annotations

SUMMARY_PROMPT_NAME = "stream_memory.summary"
NEWS_PROMPT_NAME = "stream_memory.news"
PERSONA_PROMPT_NAME = "stream_memory.persona"
SENSITIVITY_PROMPT_NAME = "stream_memory.sensitivity"

_NO_MEANINGFUL_CONTENT_TOKEN = "NO_MEANINGFUL_CONTENT"
NO_MEANINGFUL_CONTENT_TOKEN = _NO_MEANINGFUL_CONTENT_TOKEN

SUMMARY_PROMPT: str = """你是记忆系统“射命丸文记忆”的摘要整理子代理。你的任务是把给定群聊的一段时间内的聊天记录整理成一篇摘要。

要求：
1. 摘要内容必须是纯文本，不要使用任何 Markdown 语法或特殊格式（不要标题、不要列表符号、不要加粗等）。
2. 摘要内容的结构应该分为几个自然段，每个自然段描述一个独立的事件或话题。
3. 摘要本身不需要标题或日期等元信息，直接从正文开始。
4. 摘要是对聊天内容的客观概括，只描述聊天中实际发生的内容，不要编造。
5. 如果这段聊天记录没有任何有意义的内容（例如只有寒暄、表情、签到、无实质信息），只输出一个词：NO_MEANINGFUL_CONTENT"""

NEWS_PROMPT: str = """你是记忆系统“射命丸文记忆”的新闻整理子代理。系统会给你某个群聊的若干条摘要，你需要从中整理出值得长期记住的记忆条目，就像整理新闻一样。

要求：
1. 记忆条目应更加注重有意义的内容且具有总结性质，例如重要事件、人物变化、约定、长期稳定的信息；寒暄琐事不要整理。
2. 输出 JSON 数组，每个元素包含：
   - "title": 记忆标题，一句话总结事件
   - "content": 记忆条目内容，200 字左右，纯文本，不使用 Markdown
   - "participants": 参与人物列表，元素为 {"person_id": "...", "name": "..."}；person_id 必须从「可用人物清单」中选择，禁止编造；只包含摘要中实际出现的人物
3. 只输出 JSON 数组，不要输出任何其他内容或解释。
4. 如果没有任何值得整理的内容，输出 []。"""

PERSONA_PROMPT: str = """你是记忆系统“射命丸文记忆”的人物信息维护子代理。系统会给你某个人物的现有背景信息（可能为空），以及关于这个人的若干条新闻/记忆内容。请增量更新该人物的背景信息。

要求：
1. 人物信息始终为几个自然段的纯文本，不要使用任何 Markdown 语法或特殊格式。
2. 将新信息与旧信息融合，保留重要的背景信息，对重复或过时的内容进行压缩。
3. 只输出更新后的人物信息文本，不要输出标题、日期、解释或任何其他内容。
4. 如果没有现有信息，则直接从新内容整理。
5. 不要编造不存在的信息。
6. 重要：始终以「该人物本人」为视角描述其自身的信息（该人物说了什么、做了什么、是什么样的人），不要混入其他人的行为，尤其不要以群助手/Bot 的身份或视角来写。如果新内容中主角是其他人或 Bot，说明这些内容不属于该人物，应忽略。"""

SENSITIVITY_PROMPT: str = """你是记忆系统的敏感信息分级子代理。系统会给你若干条记忆条目，你需要为每条记忆判断敏感等级。

敏感等级分为三级：
1. "normal" - 普通记忆：日常对话、事实信息、公开事件等。可跨群自由召回。
2. "soft_scoped" - 软敏感记忆：涉及个人状态、情绪、压力、健康等较私密但非高度敏感的信息。跨群可见但需附带警示，LLM自行判断是否使用。
3. "hard_scoped" - 硬敏感记忆：涉及创伤、心理疾病、严重隐私、秘密等高度敏感信息。仅在来源群聊内召回，跨群物理不可达。

判断原则：
- 日常闲聊、技术讨论、兴趣爱好 → normal
- "最近压力大""状态不好""和女朋友吵架了" → soft_scoped
- "我有抑郁症""被家暴了""偷偷做了某事" → hard_scoped
- 如果不确定，倾向于保护隐私：宁可判为更高级别

输出 JSON 数组，每个元素包含：
- "index": 记忆条目的序号（从0开始）
- "sensitivity": 敏感等级（"normal"/"soft_scoped"/"hard_scoped"）
只输出 JSON 数组，不要输出其他内容。"""

PROMPT_TEMPLATES: dict[str, str] = {
    SUMMARY_PROMPT_NAME: SUMMARY_PROMPT,
    NEWS_PROMPT_NAME: NEWS_PROMPT,
    PERSONA_PROMPT_NAME: PERSONA_PROMPT,
    SENSITIVITY_PROMPT_NAME: SENSITIVITY_PROMPT,
}
