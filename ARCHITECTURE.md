# Macro Policy Radar — 全球关键矿产宏观政策雷达

## 项目概述

本系统是一个全自动化的**全球关键矿产宏观地缘政策情报引擎**。它周期性扫描全球官方政策源和 Google News 动态聚合网，抓取原始政策文本，调用 DeepSeek 大模型进行结构化研判与战略推演，最终将成果沉淀至 Notion 知识库并通过钉钉 Webhook 向高管群推送政策预警。

**v4.3 设计哲学**：事实准确是底线，时效性、重要性、相关性都在此之上收敛。**宁可漏推，不可错推。**

**核心业务领域**：锂 (Lithium)、钴 (Cobalt)、镍 (Nickel)、铜 (Copper)、稀土 (Rare Earths)、石墨 (Graphite) 等关键矿产的出口禁令、资源民族主义、关税调整、外资股权限制、税收与特许权使用费变动等宏观政策。

**分析范式**：对标 Rhodium / CSIS / 情报界 ICD 203，采用 **Fact → Baseline → Directional Impact** 智库级框架。

---

## 文件结构

```
macro-policy-radar/
├── main.py                              # 核心引擎（唯一运行入口）
├── policy_schema.json                   # JSON Schema 约束（认知滤网）
├── sources.yaml                         # 三层寻源配置
├── knowledge_baselines.yaml             # 7 国产业基线库（解耦代码，独立维护）
├── audit_baselines.py                   # 季度影子审计脚本
├── clean_old_titles.py                  # Notion 旧标题清理脚本
├── clear_notion.py                      # Notion 全量归档脚本
├── requirements.txt                     # Python 依赖
├── AI_CONTEXT.md                        # AI 上下文卡片
├── ARCHITECTURE.md                      # 本文档
└── .github/workflows/
    ├── macro_policy_radar.yml           # 每周研判调度
    └── baseline_auditor.yml             # 每季度基线审计调度
```

---

## 架构总览：五道防线 + CoT 基线定锚

```
                              ┌──────────────────────┐
                              │  knowledge_baselines  │
                              │       .yaml           │
                              │  (7 国，独立维护)      │
                              └──────────┬───────────┘
                                         │ load + inject per country
                                         ▼
┌────────────────────────────────────────────────────────────────────┐
│  输入层                                                             │
│  ├── 静态 HTML 靶向抓取（esdm.go.id 等 15 源）                       │
│  ├── 声明式查询矩阵（6 矿种 × 25 关键词 → NewsAPI + RSS）            │
│  ├── 自适应热点发现（DeepSeek 推荐当周查询）                          │
│  └── v4.0 二级抓取：RSS headline → fetch_article_full_text → 全文   │
└──────────────────────────┬─────────────────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────────────────┐
│  防线 1: is_valid_macro_policy 杀伤开关                              │
│  防线 2: noise_patterns 噪音过滤（28 关键词）                        │
│  防线 3: Historical_Noise 旧规拦截（v4.2 新增）                     │
│  防线 4: 时效性校验（年份预扫 + LLM 自检 + 交叉验证）                  │
│  防线 5: 数字净化 _sanitize_fabricated_numbers（v4.0 兜底）          │
└──────────────────────────┬─────────────────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────────────────┐
│  DeepSeek v4-pro 智库级分析 (temperature=0.2)                        │
│  ├── factual_basis: 原文逐字摘录（事实层）                            │
│  ├── industry_baseline_recall: CoT 常识锚（基线层 · v4.1）           │
│  ├── baseline_shift_detected: 范式转移自检（v4.2）                   │
│  └── impact_deduction: 节点定向推演 🔻上游→🔺中游→🔻下游（分析层）    │
└──────────────────────────┬─────────────────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────────────────┐
│  双闸门推送（四重 AND · v4.0）                                       │
│  ✅ 烈度 ∈ {High, Moderate}                                        │
│  ✅ 官方源 OR 文本含法规编号                                         │
│  ✅ 非 Procedural_Statement                                         │
│  ✅ 置信度 ≠ Low                                                    │
└──────────┬──────────────┬──────────────────────────────────────────┘
           ▼              ▼
    ┌──────────┐   ┌──────────────┐
    │ 钉钉推送   │   │ Notion 入库   │
    │ 三层卡片   │   │ properties + │
    │ + 基线破裂│   │ children 排版 │
    │ 特殊渲染   │   │ + 📝 待核标签 │
    └──────────┘   └──────────────┘
```

---

## v4.3 核心设计决策

### 智库级分析框架（Fact → Baseline → Directional Impact）

| 层 | 字段 | 来源 | 约束 |
|----|------|------|------|
| 事实层 | `substantive_provisions` + `factual_basis` | 原文逐字摘录 | 禁止增加原文没有的事实/数字 |
| 基线层 | `industry_baseline_recall` | YAML 注入 + LLM 训练数据 | CoT 先回忆常识再推演 |
| 分析层 | `impact_deduction` | 以事实层+基线层为基础 | 节点格式 🔻🔺🔻，禁止编造百分比 |
| 自检 | `baseline_shift_detected` | LLM 对比基线 vs 最新情报 | true → 🚨 运维告警 + 推送卡片标红 |

### 五道防线

| # | 防线 | 触发条件 | 效果 |
|---|------|---------|------|
| 1 | `is_valid_macro_policy` | LLM 判定无关内容 | 直接丢弃 |
| 2 | `noise_patterns` | 标题含 28 个否定关键词 | 过滤 |
| 3 | `Historical_Noise` | `current_stage == "Historical_Noise"` | 丢弃（旧规复述无增量） |
| 4 | 时效性校验 | 年份预扫 + LLM `is_recent_policy_action` + 交叉验证 | 降级为 `Low_Monitoring` |
| 5 | `_sanitize_fabricated_numbers` | LLM 输出数字不在原文中 | 标记 `⚠️(待核)` |

### 双闸门推送（四重 AND）

```python
PUSH_REQUIRED_SOURCE_TYPES = {"Official_Gazette", "Ministry_Website", "Customs_Announcement"}
REGULATION_PATTERNS = [r"Permen ESDM", r"Keputusan Menteri", r"UU", r"国务院令", ...]

def _should_push(data, fetched_text, source_depth):
    # 闸门 1: 烈度 High/Moderate
    # 闸门 2: 官方源 OR 文本含法规编号
    # 闸门 3: 非 Procedural_Statement
    # 闸门 4: 置信度 ≠ Low
```

### 基线运维体系

| 频率 | 机制 | 产物 |
|------|------|------|
| 每次运行 | `main.py` 启动交叉表 | `⚠️ [基线缺口] AU, CD, CL` |
| 每季度 | `audit_baselines.py` 影子审计 | NewsAPI+RSS → DeepSeek Diff → `NO_CHANGE` 更新时间戳 or `CHANGE` CI 告警 |

---

## 环境变量一览

| 变量名 | 用途 | 必填 |
|--------|------|------|
| `OPENAI_API_KEY` (注入 `DEEPSEEK_API_KEY`) | DeepSeek API 密钥 | 是 |
| `OPENAI_BASE_URL` | API 端点 (默认 `https://api.deepseek.com`) | 否 |
| `NEWSAPI_KEY` | NewsAPI 密钥 | 否 |
| `NOTION_TOKEN` | Notion Integration Token | 否 |
| `NOTION_DATABASE_ID` | Notion 目标数据库 ID | 否 |
| `DINGTALK_WEBHOOK` | 钉钉 Webhook URL | 否 |
| `DINGTALK_SECRET` | 钉钉加签密钥 | 否 |
| `MAX_AI_CALLS` | 单轮最大 AI 调用数 (默认 8) | 否 |
| `MAX_CONSECUTIVE_EMPTY` | 连续空转熔断上限 (默认 5) | 否 |
| `MIN_TEXT_LENGTH` | 文本最短字符数门槛 (默认 300) | 否 |
| `MIN_FULLTEXT_LENGTH` | 二级抓取最小正文长度 (默认 300) | 否 |
| `RSS_MAX_AGE_DAYS` | RSS 文章硬过滤天数 (默认 14) | 否 |
| `NOTION_HAS_AUTHORITY_FIELD` | 启用 Notion 颁布机构列 | 否 |

---

## 扩展指南

### 新增情报源

在 `sources.yaml` 的 `macro_sources` 下新增条目即可。系统启动时自动检测国家代码，若 `knowledge_baselines.yaml` 中无对应基线，会在日志中打印 `⚠️ [基线缺口]` 提示。

### 新增/更新国家基线

编辑 `knowledge_baselines.yaml`，按已有格式新增条目。系统下次运行时自动加载，无需改代码。

### 审计手动触发

```bash
python3 audit_baselines.py                    # 全量审计
python3 audit_baselines.py --country ID       # 单国审计
python3 audit_baselines.py --update-timestamp # 仅更新时间戳
```

---

## 项目设计哲学

1. **管道式无状态架构**：每次运行独立，适合 Cron 调度
2. **Schema 驱动**：AI 输出结构与分发逻辑由 `policy_schema.json` 统一定义
3. **基线解耦**：产业常识与代码物理分离，YAML 文件可独立维护
4. **事实准确优先**：五道防线 + CoT 基线定锚 + 数字净化兜底，宁可漏推不可错推
5. **人机协同**：LLM 生成分析 → 代码层拦截 + 置信度标注 → 人工可在 Notion 复核 📝 项

---

*最后更新: 2026-06-22 — v4.3*
