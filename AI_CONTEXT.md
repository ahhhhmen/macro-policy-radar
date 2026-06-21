项目功能：全球关键矿产宏观政策情报雷达 —— 自动化监控多国政府对锂、钴、镍、铜、稀土、石墨等关键矿产的出口禁令、资源民族主义、关税调整等政策变动，调用 DeepSeek v4-pro 进行智库级结构化研判（事实-基线-定向推演），结果持久化至 Notion 知识库，高冲击力政策通过钉钉推送给决策层。

当前版本：v4.3 — 事实优先重构（智库级基线定锚 + 五重防线防幻觉）

核心防幻觉机制：is_valid_macro_policy 杀伤开关 → noise_patterns 噪音过滤 → Historical_Noise 旧规拦截 → 时效性校验 → 双闸门推送（四重 AND）→ _sanitize_fabricated_numbers 数字净化

智库级分析框架：factual_basis（原文溯源）→ industry_baseline_recall（基线 CoT 常识锚）→ impact_deduction（节点定向推演 🔻上游→🔺中游→🔻下游）→ baseline_shift_detected（范式转移自检）

基线运维：knowledge_baselines.yaml（7 国解耦基线）+ main.py 交叉表 + audit_baselines.py 季度影子审计

技术栈：Python 3.10+ / OpenAI SDK (DeepSeek v4-pro) / BeautifulSoup4 + lxml (HTML/RSS) / PyYAML / requests / Notion API + DingTalk Webhook / GitHub Actions (每周研判 + 每季度基线审计)
