# Macro Policy Radar — 全球关键矿产宏观政策雷达

## 项目概述

本系统是一个全自动化的**全球关键矿产宏观地缘政策情报引擎**。它周期性扫描全球官方政策源和 Google News 动态聚合网，抓取原始政策文本，调用 DeepSeek 大模型进行结构化研判与战略推演，最终将成果沉淀至 Notion 知识库并通过钉钉 Webhook 向高管群推送重磅预警。

**核心业务领域**：锂 (Lithium)、钴 (Cobalt)、镍 (Nickel)、铜 (Copper)、稀土 (Rare Earths)、石墨 (Graphite) 等关键矿产的出口禁令、资源民族主义、关税调整、外资股权限制、税收与特许权使用费变动等宏观政策。

---

## 文件结构

```
macro-policy-radar/
├── main.py                          # 核心引擎（唯一运行入口）
├── policy_schema.json               # AI 输出格式的 JSON Schema 约束
├── sources.yaml                     # 情报源配置（静态靶向源 + 动态聚合网）
├── .github/workflows/
│   └── macro_policy_radar.yml       # GitHub Actions 定时/手动触发编排
└── ARCHITECTURE.md                  # 本文档
```

**依赖项**（无 `requirements.txt`，依赖直接写在 CI 的 pip install 中）：
- `openai` — 对接 DeepSeek API（兼容 OpenAI SDK）
- `pyyaml` — 解析 `sources.yaml`
- `beautifulsoup4` + `lxml` — HTML/XML/RSS 解析
- `requests` — HTTP 客户端

---

## 架构总览（管道式流处理）

```
┌─────────────────────────────────────────────────────────────┐
│                    GitHub Actions 调度器                      │
│         每周一 09:00 CST (cron: 0 1 * * 1)                   │
│         或手动触发 (workflow_dispatch)                       │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  步骤 1: 加载配置                                            │
│  ┌──────────────────┐   ┌──────────────────┐                │
│  │ policy_schema.json│   │  sources.yaml     │                │
│  │ (认知滤网/输出规范)│   │ (情报源编排)       │                │
│  └──────────────────┘   └────────┬─────────┘                │
│                                  │                           │
│        ┌─────────────────────────▼──────────────────────┐   │
│        │  load_all_sources()                              │   │
│        │  • 静态源 (macro_sources): html 靶向抓取         │   │
│        │  • 动态聚合网 (dynamic_aggregators):            │   │
│        │    将布尔查询 + 时间窗口编译为 Google News RSS   │   │
│        └─────────────────────────┬──────────────────────┘   │
└──────────────────────────────────┼──────────────────────────┘
                                   │
                     ┌─────────────▼─────────────┐
                     │   for each source:          │
                     │   并行遍历所有探测节点       │
                     └─────────────┬─────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
              ▼                    ▼                    ▼
     feed_type == "rss"    feed_type == "html"
              │                    │
              ▼                    ▼
   fetch_and_parse_rss()   fetch_and_clean_html()
   • 用 BeautifulSoup      • 用 CSS Selector 定位
     解析 XML/RSS           目标 DOM 区域
   • 取前 3 条 item        • 剥离 script/style/
   • 拼接为单一文本          nav/footer/header
              │                    │
              └──────────┬─────────┘
                         │
          ┌──────────────▼──────────────┐
          │  文本长度 > 100 字符?         │
          │  YES → 进入 AI 研判          │
          │  NO  → 跳过（无有效情报）     │
          └──────────────┬──────────────┘
                         │
                         ▼
          ┌──────────────────────────────┐
          │  extract_macro_policy()       │
          │  • 调用 DeepSeek v4-pro      │
          │  • System Prompt: 资深产业    │
          │    顾问角色 + Schema 约束     │
          │  • response_format:          │
          │    json_object               │
          │  • temperature: 0.2          │
          └──────────────┬──────────────┘
                         │
                         ▼
          ┌──────────────────────────────┐
          │  终端双流控闭环                │
          │                              │
          │  ┌──────────────────────┐    │
          │  │ insert_to_notion()   │    │
          │  │ → Notion 知识沉淀     │    │
          │  └──────────────────────┘    │
          │  ┌──────────────────────┐    │
          │  │ send_dingtalk_alert()│    │
	          │  │ → 钉钉高管群即时触达  │    │
	          │  │  (High+Medium 级别     │    │
	          │  │   才触发告警)         │    │
          │  └──────────────────────┘    │
          └──────────────────────────────┘
```

---

## 各模块详解

### 1. `sources.yaml` — 情报源编排

这是系统的"眼睛"，定义了从哪里抓取原始政策信息。采用双层架构：

**第一层：静态靶向源 (`macro_sources`)**
- 人工精选的官方政策网站，如印尼能矿部新闻中心
- `feed_type: "html"` 表示用 CSS Selector 直接抓取网页特定区域
- 每个源有唯一 `id`、`country`、`agency`、`url`、`dom_selector`
- 通过 `enabled: true/false` 控制开关

**第二层：动态全球聚合网 (`dynamic_aggregators`)**
- 类型固定为 `google_news_rss`
- 通过 Google News 的布尔搜索语法动态生成 RSS 订阅 URL
- 支持 `mineral_focus`（矿种聚焦）、`query`（布尔查询关键词）、`time_window`（时间窗口如 `7d`）
- 编译后的 URL 格式：`https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en`
- 自动附加 `when:7d` 时间过滤防止信息过载

**设计意图**：静态源保证权威性（"一锤定音"），动态聚合网防止遗漏任何地区的突发政策（"升维防遗漏"）。

---

### 2. `policy_schema.json` — 认知滤网与输出规范

这是系统对 AI 输出的"强约束"。定义了 DeepSeek 必须返回的 JSON 结构，包含 4 个顶层模块：

| 模块 | 用途 | 关键字段 |
|------|------|---------|
| `metadata` | 基础元信息 | `country` (ISO二字码), `mineral_types` (枚举7种矿), `policy_source_type` |
| `policy_dynamics` | 政策动态核心 | `policy_name_zh` (中文名), `current_stage` (5阶段枚举), `core_category` (6种政策手段), `substantive_provisions` (≤300字实质条款) |
| `strategic_implications` | 战略影响研判 | `supply_chain_impact_level` (3级烈度), `impact_deduction` (顾问级推演: 产能波及→冶炼成本传导→对策) |
| `notion_integration` | 分发控制 | `master_tag` (固定值"宏观地缘与产业政策"), `dingtalk_alert_required` (熔断开关) |

**Schema 设计约束**：
- `dingtalk_alert_required` 由 AI 判定：当冲击烈度为 `High_Disruption` 或 `Moderate_Adjustment` 时强制为 `true`；`Low_Monitoring` 强制为 `false`
- `master_tag` 使用 `const` 固化，确保所有入库条目打上统一标签
- `core_category` 支持多选，覆盖出口禁令、资源民族主义、选矿强制令等 6 类政策工具

---

### 3. `main.py` — 核心引擎（9 个函数）

#### 函数调用关系图

```
main() ▸ if __name__ == "__main__"
  ├── load_schema("policy_schema.json")        → schema_dict
  ├── load_all_sources("sources.yaml")          → [source, ...]
  │     └── 将 dynamic_aggregators 编译为 RSS URL 条目
  │
  └── for source in all_active_sources:
        ├── fetch_and_parse_rss(url)            # feed_type == "rss"
        │     └── BeautifulSoup('xml') 解析
        │     └── 取前 3 条 item，拼接为文本
        │
        ├── fetch_and_clean_html(url, selector) # feed_type == "html"
        │     └── BeautifulSoup('html.parser') 解析
        │     └── CSS Selector 定位目标区域
        │     └── 剥离 script/style/nav/footer/header 噪声标签
        │
        ├── extract_macro_policy(raw_text, schema_dict)
        │     └── OpenAI Client → DeepSeek API
        │     └── System Prompt = 资深顾问角色 + Schema JSON 嵌入
        │     └── response_format = json_object, temperature = 0.2
        │
        ├── insert_to_notion(analysis_result, source['url'])   # 流控闭环 1
        │     └── Notion API POST /v1/pages
        │     └── 将 Schema 字段映射到 Notion 数据库属性（含新字段）
        │     └── 注入原文链接 source_url 到 "原文链接" 列
        │     └── 动态安全注入生效日期（拦截空值/NULL）
        │
        └── send_dingtalk_alert(analysis_result, source['url']) # 流控闭环 2
              └── 检查 dingtalk_alert_required 熔断条件
              └── 构建 Markdown 卡片 + 原文溯源链接 → POST DingTalk Webhook
```

#### 各函数详细说明

| 函数 | 输入 | 输出 | 异常处理 |
|------|------|------|---------|
| `load_schema(path)` | JSON 文件路径 | dict | 无（依赖文件存在） |
| `load_all_sources(yaml_path)` | YAML 路径 | `list[dict]` | 仅加载 `enabled: true` 的源 |
| `fetch_and_clean_html(url, selector)` | URL + CSS选择器 | `str\|None` | try/except + 退化为 body 全文 |
| `fetch_and_parse_rss(url, limit=3)` | RSS URL | `str\|None` | try/except，无 item 返回 None |
| `extract_macro_policy(raw_text, schema_dict)` | 原始文本 + Schema | `dict\|None` | 空响应/JSON解析异常均返回 None |
| `insert_to_notion(data, source_url)` | AI 研判结果 + 溯源URL | `bool` | 无凭证/disaled → 静默跳过；API 错误打印状态码 |
| `send_dingtalk_alert(data, source_url)` | AI 研判结果 + 溯源URL | `None` | 无 Webhook/disabled → 静默跳过；先检查熔断条件 |

#### 关键设计决策

1. **OpenAI SDK 兼容 DeepSeek**：通过设置 `base_url="https://api.deepseek.com"` 和 `OPENAI_API_KEY`（实际注入 `DEEPSEEK_API_KEY` 密钥）实现无缝对接。

2. **温度参数 0.2**：低温度确保输出的确定性，适合结构化提取任务。

3. **RSS 条目限制为 3 条**：防止单次调用上下文过长，也控制 API 费用。

4. **HTML 降级策略**：当 CSS Selector 匹配失败时，退化为抓取整个 `<body>` 文本，确保不丢失信息。

5. **门控阈值（100 字符）**：过滤空页面或几乎无内容的响应，避免浪费 AI 调用。

6. **钉钉熔断机制**：不是所有政策都推送告警，只有 AI 判定为 High_Disruption 或 Moderate_Adjustment 级别的才触发钉钉通知，Low_Monitoring 被静默过滤，实现"防打扰"。Python 端双重保障：同时检查 AI 的 `dingtalk_alert_required` 标记和 `supply_chain_impact_level` 枚举值。

---

### 4. `.github/workflows/macro_policy_radar.yml` — 调度编排

| 配置项 | 值 |
|--------|-----|
| 触发方式 | 定时：每周一 01:00 UTC (北京时间 09:00) + 手动触发 |
| 运行环境 | `ubuntu-latest` |
| Python 版本 | 3.10 |
| 依赖安装 | `openai pyyaml beautifulsoup4 requests lxml` |
| 密钥变量 | `DEEPSEEK_API_KEY`, `DINGTALK_WEBHOOK`, `NOTION_TOKEN`, `NOTION_DATABASE_ID` |

**注意**：CI 中将 `OPENAI_BASE_URL` 硬编码为 `"https://api.deepseek.com"`，但 `OPENAI_API_KEY` 来自 GitHub Secrets 的 `DEEPSEEK_API_KEY`。这利用了 OpenAI SDK 的环境变量命名约定。

---

## 数据流与状态

系统是**无状态**的：每次运行都是全新的管道，不依赖本地数据库或文件系统存储中间结果。状态持久化由外部服务承担：

- **Notion**：所有结构化情报的长期知识沉淀
- **钉钉**：高优先级即时触达通道

运行日志通过 `print()` 输出到 GitHub Actions 的 stdout，可直接在 Actions 页面查看。

---

## 环境变量一览

| 变量名 | 用途 | 是否必填 |
|--------|------|---------|
| `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | 是 |
| `OPENAI_BASE_URL` | API 端点 (默认 `https://api.deepseek.com`) | 否 |
| `NOTION_TOKEN` | Notion Internal Integration Token | 否（未配置则跳过） |
| `NOTION_DATABASE_ID` | Notion 目标数据库 ID | 否（未配置则跳过） |
| `DINGTALK_WEBHOOK` | 钉钉群机器人 Webhook URL | 否（未配置则跳过） |

---

## 扩展与维护指南

### 添加新的情报源

**静态 HTML 源**：在 `sources.yaml` 的 `macro_sources` 下新增条目：
```yaml
- id: "id_new_source"
  country: "XX"
  agency: "机构名称"
  feed_type: "html"
  url: "https://..."
  dom_selector: "div.target-content-area"
  enabled: true
```

**动态聚合查询**：在 `dynamic_aggregators` 下新增：
```yaml
- id: "agg_new_mineral"
  type: "google_news_rss"
  mineral_focus: ["Copper"]
  query: 'Copper AND ("Export" OR "Tariff") AND (Chile OR Peru)'
  time_window: "7d"
  enabled: true
```

### 修改 AI 输出结构

编辑 `policy_schema.json`，AI 会自动按新 Schema 输出。但需要同步修改：
- `insert_to_notion()` 中的 Notion 属性映射（第 119-127 行）
- `send_dingtalk_alert()` 中的 Markdown 模板（第 176-189 行）

### 更换 AI 模型

修改 `main.py` 第 85 行的 `model` 参数，例如改为 `"deepseek-chat"` 或接入 OpenAI 官方模型（需同步修改 `base_url`）。

### 调整运行频率

修改 `.github/workflows/macro_policy_radar.yml` 第 8 行的 cron 表达式。GitHub Actions 的 cron 使用 UTC 时间，格式为 `分 时 日 月 周`。

### 故障排查要点

1. **所有源都返回"未发现政策变动"** → 检查网络连通性，确认 Google News RSS URL 未被墙
2. **DeepSeek 返回空内容** → 检查 API 余额和密钥有效性
3. **Notion 写入失败** → 确认 Integration 已连接到目标数据库，且数据库属性名称完全匹配
4. **钉钉推送失败** → 检查 Webhook URL 是否过期（钉钉机器人有安全设置限制）

---

## 项目设计哲学

1. **管道式无状态架构**：每次运行独立，适合 Cron 调度，不引入数据库复杂度
2. **容错先行**：HTML 抓取有降级策略，RSS 解析有异常保护，Notion/钉钉均为可选模块
3. **Schema 驱动**：AI 输出结构与分发逻辑由 `policy_schema.json` 统一定义，修改 Schema 即可改变系统行为
4. **双通道分发**：Notion 做知识沉淀（长尾价值），钉钉做实时触达（时效价值），通过熔断机制避免信息轰炸
5. **静态+动态双层情报网**：权威官方源保证准确性，Google News 聚合网保证覆盖面