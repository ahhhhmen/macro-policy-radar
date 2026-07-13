# Macro Policy Radar — Workspace Custom Rules & Logic Consensus

This document defines the core domain rules and logic constraints for this specific workspace. Future agents must read this file first and strictly follow these rules to maintain logic consistency and prevent regressions.

---

## 1. Project Positioning & Architecture
- 全球关键矿产宏观地缘政策情报引擎，自动化采集→分析→过滤→推送流水线。
- 单文件主引擎 `main.py`（基于 `radar-infra` 共享基础设施深度重构瘦身），管道式无状态设计。
- 配置全外置：`sources.yaml`（情报源）、`policy_schema.json`（LLM Schema）、`knowledge_baselines.yaml`（基线库，v3.0 起采用 documents 结构化列表，且影子审计脚本 audit_baselines.py 已适配此结构）、`.env`（凭证）。
- 五道防线：杀伤开关→关键词噪音→旧规拦截→时效校验→数字捏造净化。
- GitHub Actions 每周一 09:00 CST 自动运行。

## 2. Tech Stack & Conventions
- Python 3.10+，函数式管道风格。
- Type hints 强制、logging 替代 print、pathlib 优先。
- OpenAI SDK（DeepSeek 兼容接口）+ BeautifulSoup4 + lxml + diskcache 三层缓存（与 `radar-infra` 底层协作）。
- 全局常量：大写蛇形 `MAX_AI_CALLS`；函数：小写蛇形，私有函数下划线前缀 `_should_push()`；映射字典：大写蛇形 `_COUNTRY_ZH_MAP`。
- 版本变更在注释中以 `v4.0`、`v5.0`、`v6.0` 格式标注。

## 3. Domain Logic & Data Filters (领域业务与过滤规则)
- **推送状态三元分离 (`v5.9`)**：`_should_push` 严格返回 `ALERT`、`ROUTINE`、`MUTE` 三种状态。
- **强制静默 (MUTE) 底线**：
  - `event_update.event_classification == Routine_Commentary` 时必须静默入库，不得被 `supply_chain_impact_level` 覆盖为钉钉推送。
  - `news_recency_verification.dingtalk_alert_required == false` 时必须视为硬闸门，禁止推送。
  - `_numbers_flagged > 0` 时必须进入人工复核，不得直接推送。
  - `semantic_diff.has_material_change == false` 时保持静默，即使旧文件的烈度字段为高，也不能升级为告警。
- **自动寻源安全门槛**：`log_discovered_source` 必须且只能在 `article_type == "Official_Announcement"`（官方公告）时，才能提取网址域名并追加至 `discovered_sources.yaml`，严禁将媒体（如 cryptobriefing.com）或分析网站作为官方源沉淀。
- **Notion 去重与 API 通信底层**：全面调用新版 `NotionSink` 单例，所有的 Notion 查询/新建/PATCH 动作收敛于 `sink.api_request()`。Notion 去重遵循 v5.5 规范：首道防线使用 `原文链接` 精确过滤 URL，第二道防线使用 `policy_entity.official_name` 核心清洗后模糊检索，提取缩写的正则表达式须支持带数字与连字符的复杂编号（如 `SP8000-26-R-0021`），第三道防线使用 `document_signature` 兜底；相似度计算针对英文采用单词词组级 Jaccard (>=0.60) 与 Overlap (>=0.85)，并支持括号内简称/全称的递归比对，防止因行政前缀噪声/翻译错位/简称变体引起重复建档。
- 钉钉输出不得包含 `⚠️` 这类模型自检标记；复核标记仅保留在 Notion 内部字段。
- 钉钉推送采用 v5.6 智能简报格式：将监测动态按锂、钴、镍、其它关键金属进行分组排版，重磅警告展示完整三层研判，常规动态以紧凑列表合并展现，保障信息完备度。
- v5.7 监控网拓宽：宏观雷达不仅追踪 10 种特定关键金属，亦涵盖以循环经济、再生回收目标（如再生塑料/钢/铝/镁回收门槛、EPR 生产者延伸责任、报废车辆 ELV 追溯）为切入点且辐射关键原材料供应链的跨行业上位法案。寻源矩阵需在 `policy_keywords` 中涵盖相关回收合规术语，且在 `broad_minerals` 兜底中包含 "critical raw materials" 和 "critical materials" 等概念伞。
- v5.8 Google News 链接解析与安全过滤：使用升级版的 `resolve_google_news_url` 进行解码。优先通过 `googlenewsdecoder` 解析；若失败则依次尝试离线解析 Base64 编码路径、提取 URL Query 参数；最后通过 `HEAD/GET` 网络重定向和 HTML Canonical 元数据提取进行兜底。所有解析结果必须经过 `_is_valid_news_url` 强过滤规则，排除统计追踪器、广告链接及样式表、脚本、图片等静态资源，防止数据污染。

## 4. Name Normalization & Cleaners (名称归一化规则)
<!-- Define mapping lists for data normalization to clean messy source inputs. -->

## 5. Contextual Milestones (上下文里程碑与时序规则)
<!-- Detail chronological rules or legacy phase mappings of the data here. -->

## 6. Local-First & Privacy Constraints (安全与隐私隔离防线)
- `.env` 必须忽略并且不得提交。
- `discovered_sources.yaml` 只记录候选新源，不写入凭证或私密原始数据。
- 任何临时调试输出、下载的新闻原文、人工标注草稿都不得加入版本控制。
- 若新增测试依赖外部服务，必须提供本地可运行的 stub 或 mock，不得强迫 CI 以真实密钥回放.

## 7. CI / GitHub Operations Consensus (CI 与远端同步共识)
- `audit_baselines.py` 的季度影子审计依赖 `DEEPSEEK_API_KEY`。GitHub Actions 中若该 secret 未配置，脚本必须输出 `::warning::` 并以退出码 0 跳过 AI 审计，不得让季度任务红灯；配置 secret 后才执行真实 DeepSeek 审计。
- `OPENAI_API_KEY` 不得被视为季度基线审计的可用凭证；当前 `DeepSeekProvider()` 明确要求 `DEEPSEEK_API_KEY`。
- 缺少外部服务凭证的测试必须使用本地 stub/mock，覆盖无密钥降级路径，禁止要求 CI 使用真实密钥回放。
- 判断“是否已完全更新到 GitHub”时，至少检查 `git status --short`、当前分支、本地 `HEAD` 与 `origin/<branch>` 是否一致；如网络允许，再用 `git ls-remote` 核对远端真实哈希。
- 工作区里的未跟踪副本文件（例如 `main 2.py`）不得默认推送。若其内容匹配历史提交或明显是本地备份，应视为非生产文件；经用户确认后删除，避免把过期主引擎副本加入仓库。

---

<!-- This file is the single source of truth. .cursorrules, .windsurfrules, .github/copilot-instructions.md, and CLAUDE.md all symlink here. -->
