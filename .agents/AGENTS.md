# Macro Policy Radar — Workspace Custom Rules & Logic Consensus

This document defines the core domain rules and logic constraints for this specific workspace. Future agents must read this file first and strictly follow these rules to maintain logic consistency and prevent regressions.

---

## 1. Project Positioning & Architecture
- 全球关键矿产宏观地缘政策情报引擎，自动化采集→分析→过滤→推送流水线。
- 单文件主引擎 `main.py`（1743 行），管道式无状态设计。
- 配置全外置：`sources.yaml`（情报源）、`policy_schema.json`（LLM Schema）、`knowledge_baselines.yaml`（基线库，v3.0 起采用 documents 结构化列表，且影子审计脚本 audit_baselines.py 已适配此结构）、`.env`（凭证）。
- 五道防线：杀伤开关→关键词噪音→旧规拦截→时效校验→数字捏造净化。
- GitHub Actions 每周一 09:00 CST 自动运行。

## 2. Tech Stack & Conventions
- Python 3.10+，函数式管道风格。
- Type hints 强制、logging 替代 print、pathlib 优先。
- OpenAI SDK（DeepSeek 兼容接口）+ BeautifulSoup4 + lxml + diskcache 三层缓存（内存→磁盘→API）。
- 全局常量：大写蛇形 `MAX_AI_CALLS`；函数：小写蛇形，私有函数下划线前缀 `_should_push()`；映射字典：大写蛇形 `_COUNTRY_ZH_MAP`。
- 版本变更在注释中以 `v4.0`、`v5.0` 格式标注。

## 3. Domain Logic & Data Filters (领域业务与过滤规则)
<!-- Define specific classification rules, blocklists, or validation boundaries here. -->

## 4. Name Normalization & Cleaners (名称归一化规则)
<!-- Define mapping lists for data normalization to clean messy source inputs. -->

## 5. Contextual Milestones (上下文里程碑与时序规则)
<!-- Detail chronological rules or legacy phase mappings of the data here. -->

## 6. Local-First & Privacy Constraints (安全与隐私隔离防线)
<!-- Explicitly list files that MUST be ignored by Git (e.g. config keys, private raw data) to enforce zero data leakage. -->

---

<!-- This file is the single source of truth. .cursorrules, .windsurfrules, .github/copilot-instructions.md, and CLAUDE.md all symlink here. -->
