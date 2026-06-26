# Macro Policy Radar — 全球关键矿产宏观政策雷达

> **当前版本：v5.5** — 自动寻源系统与政策覆盖修复（泛在域名通配 RSS + 逆向域名回溯闭环自适应寻源）

## 项目概述

全自动化的**全球关键矿产宏观地缘政策情报引擎**。周期性扫描全球官方政策源和 Google News 动态聚合网，抓取原始政策文本，调用 DeepSeek v4-pro 进行智库级结构化研判（**事实 → 基线 → 定向推演**），结果持久化至 Notion 知识库，高冲击力政策通过钉钉推送给决策层。

**核心业务领域**：锂、钴、镍、铜、稀土、石墨等关键矿产的出口禁令、资源民族主义、关税调整、外资股权限制、税收与特许权使用费变动等宏观政策。

**设计哲学**：事实准确是底线，时效性、重要性、相关性都在此之上收敛。**宁可漏推，不可错推。**

**技术栈**：Python 3.10+ / OpenAI SDK (DeepSeek v4-pro) / BeautifulSoup4 + lxml / PyYAML / requests / Notion API + DingTalk Webhook / GitHub Actions（每周研判 + 每季度基线审计）

---

## 核心机制速览

| 机制 | 说明 |
|------|------|
| **五道防线** | `is_valid_macro_policy` 杀伤开关 → `noise_patterns` 28 关键词过滤 → `Historical_Noise` 旧规拦截 → 时效性校验（年份预扫 + LLM 自检） → `_sanitize_fabricated_numbers` 数字净化兜底 |
| **智库分析框架** | `factual_basis`（原文溯源）→ `industry_baseline_recall`（CoT 常识锚）→ `impact_deduction`（🔻上游→🔺中游→🔻下游 节点推演）→ `baseline_shift_detected`（范式转移自检） |
| **双闸门推送** | 四重 AND（烈度 + 官方源 + 非程序性说明 + 置信度≠Low），通过才推送钉钉 |
| **自适应寻源 (Layer 4)** | **国别域名通配 RSS 扫描 (Scheme 1)** 自动捕捉新政；**成功政策域名回溯探针 (Scheme 2)** 自动抽取未知官网域名并写入 YAML 候选池 |
| **基线运维** | `knowledge_baselines.yaml` 7 国解耦基线 + 启动交叉表 + `audit_baselines.py` 季度影子审计 |
| **分析范式** | 对标 Rhodium / CSIS / 情报界 ICD 203：Fact → Baseline → Directional Impact |

---

## 文件结构

```
macro-policy-radar/
├── main.py                              # 核心引擎（唯一运行入口）
├── policy_schema.json                   # JSON Schema 约束（认知滤网 + 输出规范）
├── sources.yaml                         # 四层寻源配置（含通配扫描源）
├── discovered_sources.yaml             # 自动发现的新官方源候选池 (v5.5 新增)
├── knowledge_baselines.yaml             # 7 国产业基线库（解耦代码，独立维护）
├── audit_baselines.py                   # 季度影子审计脚本
├── requirements.txt                     # Python 依赖
├── README.md                            # 本文档
└── .github/workflows/
    ├── macro_policy_radar.yml           # 每周研判调度（周一 09:00 CST）
    └── baseline_auditor.yml             # 每季度基线审计调度
```

共享基础设施（LLM 缓存/重试/抓取/推送等）由 [radar-infra](https://github.com/ahhhhmen/radar-infra) 提供。

---

## 快速开始

### 前置要求

- Python 3.10+
- [DeepSeek API key](https://platform.deepseek.com/)
- （可选）[NewsAPI key](https://newsapi.org/) — 不配则走 Google News RSS 回退
- （可选）Notion 集成 token + 数据库 ID

```bash
# 1. 克隆项目
git clone https://github.com/ahhhhmen/macro-policy-radar.git
cd macro-policy-radar

# 2. 创建 .env 文件，至少填入 DeepSeek key
cat > .env << 'EOF'
DEEPSEEK_API_KEY=sk-your-key-here
# 可选配置
# NEWSAPI_KEY=xxx
# NOTION_TOKEN=secret_xxx
# NOTION_DATABASE_ID=xxx
# DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
# DINGTALK_SECRET=xxx
EOF

# 3. 安装依赖（自动拉取 radar-infra）
pip install -r requirements.txt

# 4. 运行
python main.py

# 仅更新基线时间戳
python audit_baselines.py --update-timestamp
```

---

## 架构总览

```
                              ┌──────────────────────┐
                              │  knowledge_baselines  │
                              │       .yaml           │
                              │  (7 国，独立维护)      │
                              └──────────┬───────────┘
                                         │ load + inject per country
                                         ▼
┌────────────────────────────────────────────────────────────────────┐
│  输入层 (四层自适应寻源)                                            │
│  ├── 第 1 层：静态 HTML 靶向抓取（esdm.go.id 等 20+ 精选源）          │
│  ├── 第 2 层：声明式查询矩阵（10 矿种 × 30 关键词 → NewsAPI + RSS）    │
│  ├── 第 3 层：自适应热点发现（DeepSeek 每周推荐当周查询）              │
│  ├── 第 4 层：自适应自动寻源（域名通配 RSS 泛搜 + 逆向域名回溯探针）    │
│  └── 二级抓取深度补全：RSS headline → fetch_article_full_text → 全文  │
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
│  推送终判                                                           │
│  ├── 双闸门（四重 AND · v4.0）→ 通过才推送钉钉                       │
│  ├── Notion 入库：properties + children 块级排版 + 📝 待核标签       │
│  └── 范式转移基线破裂 ⚠️ 特殊渲染（v4.2）                            │
└────────────────────────────────────────────────────────────────────┘
```

---

## 智库级分析框架详解

对标 Rhodium 每行标注 `Source: [primary data], Rhodium Group analysis` 和情报界 "the source says vs we assess" 的分层标准：

| 层 | 字段 | 来源 | 约束 |
|----|------|------|------|
| 事实层 | `substantive_provisions` + `factual_basis` | 原文逐字摘录 | 禁止增加原文没有的事实、数字 |
| 基线层 | `industry_baseline_recall` | YAML 注入 + LLM 训练数据 | CoT 先回忆常识再推演 |
| 分析层 | `impact_deduction` | 事实+基线性推理 | 🔻上游→🔺中游→🔻下游 节点格式，禁止编造百分比 |
| 自检 | `baseline_shift_detected` | LLM 对比基线 vs 最新情报 | true → 🚨 运维告警 + 推送卡片标红 |
| 置信度 | `analytic_confidence` | High/Medium/Low 自评 | Low 默认不推送 |

---

## 五道防线

| # | 防线 | 触发条件 | 效果 |
|---|------|---------|------|
| 1 | `is_valid_macro_policy` | LLM 判定无关内容（导航/社会新闻/纯内政） | 直接丢弃 |
| 2 | `noise_patterns` | 标题含 28 个否定关键词 | 过滤 |
| 3 | `Historical_Noise` | `current_stage == "Historical_Noise"` | 丢弃（旧规复述无增量） |
| 4 | 时效性校验 | 年份预扫 + LLM `is_recent_policy_action` + 交叉验证 | 降级为 `Low_Monitoring` |
| 5 | `_sanitize_fabricated_numbers` | LLM 输出中的数字未在原文中出现 | 标记 `⚠️(待核)` |

---

## 双闸门推送规则

```python
PUSH_REQUIRED_SOURCE_TYPES = {"Official_Gazette", "Ministry_Website", "Customs_Announcement"}
REGULATION_PATTERNS = [
    r"Permen ESDM", r"Keputusan Menteri", r"UU", r"Peraturan",
    r"国务院令", r"商务部公告", r"发改委令", r"海关总署公告",
    r"Regulation No", r"Decree No", r"Executive Order", r"Act of \d{4}",
]

def _should_push(data, fetched_text, source_depth):
    # 闸门 1: 烈度 ∈ {High_Disruption, Moderate_Adjustment}
    # 闸门 2: 官方源 OR 文本含法规编号白名单
    # 闸门 3: 非 Procedural_Statement（程序性说明仅入库）
    # 闸门 4: 置信度 ≠ Low（信息不足时宁可漏推）
    # 闸门 5: 非 Unverified（阶段未核实不推）
```

---

## 推送卡片格式（v4.3 三层结构）

```
📌 [ID] 印尼能矿部发布采矿治理程序说明 (镍/铜)
   🏛️ 颁布机构：ESDM
   📝 Dari Perizinan hingga RKAB...
   🌍 🇮🇩 印尼 ｜ 矿种 镍、铜
   ⚖️ 程序性说明 ｜ 生效 未定 ｜ 🟡 中度调整 ｜ 🔴 置信度：低

📜 事实层（原文可核）
   - 原文为岩石类 IUP 申请程序说明...
   📎 原文依据：Tata Cara Pemberian Izin Usaha Pertambangan Batuan

⚓ 产业基线（行业共识 · CoT 常识锚）
   印尼自2020年起全面禁止镍原矿出口，RKAB为唯一配额凭证...

🔮 分析层 ｜ 置信度：低
   🔻 上游采矿端：审批收紧→国内矿权人供给受限...
   🔺 中游冶炼端：内贸矿溢价上行→独立冶炼厂利润挤压...
   🔻 下游终端：原文信息不足，待细则发布后评估

🔗 查看原文
```

范式转移时基线段变为 `⚓ 产业基线 ｜ ⚠️【历史基线已被打破】`。

---

## 基线运维

### 启动交叉表（每次运行）

```
📚 基线知识库已加载，覆盖 7 个国家，最后更新: 2026-06-22
⚠️ [基线缺口] 3 国有情报源但无基线: ['AU', 'CD', 'CL']
ℹ️ [待接源] 4 个基线国家暂无情报源: ['AR', 'GH', 'PH', 'ZW']
```

### 季度影子审计

| 命令 | 作用 |
|------|------|
| `python3 audit_baselines.py` | 全量审计：NewsAPI+RSS 采编 → DeepSeek Diff |
| `python3 audit_baselines.py --country ID` | 单国审计 |
| `python3 audit_baselines.py --update-timestamp` | 仅更新 `last_updated` |

审计结论：`NO_CHANGE` → 自动更新时间戳 / `CHANGE` → CI 告警 + 打印 Diff 报告。

### 新增/更新基线

编辑 `knowledge_baselines.yaml`，按已有格式增删条目。系统下次运行自动加载，无需改代码。

---

## 环境变量

| 变量名 | 用途 | 必填 |
|--------|------|------|
| `OPENAI_API_KEY` | DeepSeek API 密钥（注入 `DEEPSEEK_API_KEY`） | 是 |
| `OPENAI_BASE_URL` | API 端点（默认 `https://api.deepseek.com`） | 否 |
| `NEWSAPI_KEY` | NewsAPI 密钥 | 否 |
| `NOTION_TOKEN` | Notion Integration Token | 否 |
| `NOTION_DATABASE_ID` | Notion 目标数据库 ID | 否 |
| `DINGTALK_WEBHOOK` | 钉钉 Webhook URL | 否 |
| `DINGTALK_SECRET` | 钉钉加签密钥 | 否 |
| `MAX_AI_CALLS` | 单轮最大 AI 调用数（默认 8，0=不限） | 否 |
| `MAX_CONSECUTIVE_EMPTY` | 连续空转熔断上限（默认 5） | 否 |
| `MIN_TEXT_LENGTH` | 文本最短字符数门槛（默认 300） | 否 |
| `MIN_FULLTEXT_LENGTH` | 二级抓取最小正文长度（默认 300） | 否 |
| `RSS_MAX_AGE_DAYS` | RSS 文章硬过滤天数（默认 14） | 否 |
| `NOTION_HAS_AUTHORITY_FIELD` | 启用 Notion 颁布机构列 | 否 |

---

## 扩展指南

### 新增情报源

在 `sources.yaml` 的 `macro_sources` 下新增条目。系统启动时自动检测国家代码，若 `knowledge_baselines.yaml` 中无对应基线，打印 `⚠️ [基线缺口]` 提示。

### 新增/更新国家基线

编辑 `knowledge_baselines.yaml`，按已有格式新增条目即可。

### 手动触发审计

```bash
python3 audit_baselines.py                     # 全量审计
python3 audit_baselines.py --country ID        # 单国审计
python3 audit_baselines.py --update-timestamp  # 仅更新时间戳
```

---

## 设计哲学

1. **管道式无状态架构** — 每次运行独立，适合 Cron 调度
2. **Schema 驱动** — AI 输出结构由 `policy_schema.json` 统一定义，改 Schema 即改变系统行为
3. **基线解耦** — 产业常识与代码物理分离，YAML 可独立维护
4. **事实准确优先** — 五道防线 + CoT 定锚 + 数字净化兜底，宁可漏推不可错推
5. **人机协同** — LLM 生成分析 → 代码层拦截 + 置信度标注 → 📝 标签引导人工复核
