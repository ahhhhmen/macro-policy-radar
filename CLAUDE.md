# macro-policy-radar 项目说明

## 项目定位
全球关键矿产宏观地缘政策情报引擎，自动化采集→分析→过滤→推送流水线。

## 技术栈
- Python 3.10+，函数式管道风格
- 新代码遵循全局约定：type hints、logging 替代 print、pathlib 优先
- OpenAI SDK（DeepSeek 兼容接口）
- BeautifulSoup4 + lxml 网页抓取
- diskcache 三层缓存（内存→磁盘→API）
- GitHub Actions 每周一 09:00 CST 自动运行

## 架构要点
- 单文件主引擎 `main.py`（1743 行），管道式无状态设计
- 配置全外置：`sources.yaml`（情报源）、`policy_schema.json`（LLM Schema）、`knowledge_baselines.yaml`（基线库）、`.env`（凭证）
- 五道防线：杀伤开关→关键词噪音→旧规拦截→时效校验→数字捏造净化

## 命名约定
- 全局常量：大写蛇形 `MAX_AI_CALLS`
- 函数：小写蛇形，私有函数下划线前缀 `_should_push()`
- 映射字典：大写蛇形 `_COUNTRY_ZH_MAP`

## 变更标记
版本变更在注释中以 `v4.0`、`v5.0` 等格式标注
