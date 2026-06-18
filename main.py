import os
import json
import yaml
import requests
import urllib.parse
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from openai import OpenAI

load_dotenv()

def load_schema(schema_path):
    """加载认知滤网 Schema"""
    with open(schema_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_all_sources(yaml_path):
    """三层寻源架构：静态靶向源 + 声明式查询矩阵 + 自适应热点发现"""
    with open(yaml_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
        sources = []

        # 第 1 层：静态靶向源（官方网页 HTML 抓取，一锤定音）
        for src in config.get("macro_sources", []):
            if src.get("enabled", True):
                sources.append(src)

        # 第 2 层：声明式查询矩阵（A 方案 — 矿种 × 关键词自动全覆盖）
        matrix_config = config.get("query_matrix")
        days_back = 7
        if matrix_config:
            days_back = matrix_config.get("days_back", 7)
            for mq in generate_queries_from_matrix(matrix_config):
                sources.append({
                    "id": mq["id"],
                    "country": "GLOBAL",
                    "agency": mq["agency"],
                    "feed_type": "newsapi",
                    "query": mq["query"],
                    "days_back": days_back,
                })

        # 第 3 层：自适应热点发现（B 方案 — DeepSeek 推荐当周热点，可选开关）
        adaptive_cfg = config.get("adaptive_discovery", {})
        if adaptive_cfg.get("enabled", False):
            hotspots = discover_hotspots(adaptive_cfg.get("max_hotspots", 5))
            for i, hq in enumerate(hotspots):
                sources.append({
                    "id": f"hotspot_{i+1}",
                    "country": "GLOBAL",
                    "agency": f"自适应热点探测 [{i+1}]",
                    "feed_type": "newsapi",
                    "query": hq,
                    "days_back": days_back,
                })
            if hotspots:
                print(f"🔥 自适应热点发现已激活，本周期新增 {len(hotspots)} 条动态查询。")

        return sources

def fetch_and_clean_html(url, selector):
    """【HTML 轨道】抓取原生网页"""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        target_zone = soup.select_one(selector)
        if target_zone:
            for noise in target_zone(["script", "style", "nav", "footer", "header"]): noise.extract()
            return target_zone.get_text(separator="\n", strip=True)
        return soup.body.get_text(separator="\n", strip=True) if soup.body else "Empty Body"
    except Exception as e:
        print(f"⚠️ HTML 抓取失败: {str(e)}")
        return None

def fetch_and_parse_rss(url, limit=3):
    """【RSS 轨道】通用 XML 解析引擎"""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'xml')
        items = soup.find_all('item')
        if not items: return None
        combined_policies = []
        for idx, item in enumerate(items[:limit]):
            title = item.find('title').get_text(strip=True) if item.find('title') else "Untitled"
            desc_node = item.find('description') or item.find('summary')
            raw_desc = desc_node.get_text(strip=True) if desc_node else ""
            clean_desc = BeautifulSoup(raw_desc, 'html.parser').get_text(separator=" ", strip=True)
            combined_policies.append(f"--- [情报线索 #{idx+1}] ---\n标题: {title}\n详情摘要: {clean_desc}")
        return "\n\n".join(combined_policies)
    except Exception as e:
        print(f"⚠️ RSS 接口请求失败: {str(e)}")
        return None

def fetch_newsapi(query, days_back=7):
    """【NewsAPI 轨道】通过正规新闻 API 获取文章（不可用时自动回退 Google News RSS）"""
    api_key = os.environ.get("NEWSAPI_KEY")
    if not api_key or api_key == "disabled":
        print("ℹ️ 未配置 NEWSAPI_KEY，回退至 Google News RSS 管道...")
        encoded = urllib.parse.quote(f"{query} when:{days_back}d")
        rss_url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        rss_text = fetch_and_parse_rss(rss_url, limit=3)
        if rss_text:
            return {"text": rss_text, "source_url": rss_url}
        return None

    from_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "from": from_date,
        "sortBy": "publishedAt",
        "pageSize": 5,
        "language": "en",
        "apiKey": api_key,
    }
    headers = {"User-Agent": "MacroPolicyRadar/3.0"}
    try:
        res = requests.get(url, params=params, headers=headers, timeout=15)
        res.raise_for_status()
        data = res.json()
        if data.get("status") != "ok":
            print(f"⚠️ NewsAPI 返回异常状态: {data.get('message', data)}")
            return None
        articles = data.get("articles", [])
        if not articles:
            return None
        combined = []
        first_url = ""
        for idx, art in enumerate(articles[:5]):
            title = art.get("title", "Untitled")
            desc = art.get("description", "") or ""
            if idx == 0:
                first_url = art.get("url", "")
            combined.append(f"--- [线索 #{idx+1}] ---\n标题: {title}\n摘要: {desc}")
        return {"text": "\n\n".join(combined), "source_url": first_url}
    except Exception as e:
        print(f"⚠️ NewsAPI 请求失败，回退至 Google News RSS: {e}")
        encoded = urllib.parse.quote(f"{query} when:{days_back}d")
        rss_url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        rss_text = fetch_and_parse_rss(rss_url, limit=3)
        if rss_text:
            return {"text": rss_text, "source_url": rss_url}
        return None

def generate_queries_from_matrix(matrix_config):
    """方案 A：从声明式矩阵自动生成全覆盖查询（矿种 × 关键词笛卡尔积）"""
    minerals = matrix_config.get("minerals", [])
    keywords = matrix_config.get("policy_keywords", [])
    queries = []

    keyword_clause = " OR ".join(f'"{kw}"' if " " in kw else kw for kw in keywords)

    # 每个矿种一条精准查询
    for mineral in minerals:
        m = f'"{mineral}"' if " " in mineral else mineral
        query_str = f"{m} AND ({keyword_clause})"
        queries.append({
            "id": f"matrix_{mineral.lower().replace(' ', '_').replace('-', '_')}",
            "query": query_str,
            "agency": f"NewsAPI 矩阵扫描 [{mineral}]",
        })

    # 兜底宽泛查询（捕获矩阵未覆盖的跨矿种政策）
    broad = f'("critical minerals" OR "battery metals" OR "energy transition minerals") AND ({keyword_clause})'
    queries.append({
        "id": "matrix_broad_catchall",
        "query": broad,
        "agency": "NewsAPI 矩阵扫描 [Broad Catch-All]",
    })

    return queries

_deepseek_client = None

def _get_deepseek_client():
    """模块级单例：复用 OpenAI Client，避免每次调用重复初始化"""
    global _deepseek_client
    if _deepseek_client is None:
        _deepseek_client = OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
            max_retries=2,
        )
    return _deepseek_client

def discover_hotspots(max_count=5):
    """方案 B（可选）：让 DeepSeek 推荐本周矿产政策热点查询"""
    client = _get_deepseek_client()
    prompt = (
        "你是一个全球关键矿产政策追踪专家。请基于你对近期地缘政治和产业动态的了解，"
        f"推荐 {max_count} 条本周最值得关注的矿产政策英文搜索查询。"
        "每条查询应组合具体矿种、国家/地区和政策术语。"
        f'返回格式：{{"queries": ["query1", "query2", ...]}}'
    )
    try:
        response = client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.7,
        )
        content = response.choices[0].message.content
        if content:
            data = json.loads(content)
            queries = data.get("queries", []) if isinstance(data, dict) else []
            return [q for q in queries[:max_count] if isinstance(q, str) and len(q) > 5]
    except Exception as e:
        print(f"⚠️ 自适应热点发现失败，已回退纯矩阵模式: {e}")
    return []

def extract_macro_policy(raw_text, schema_dict):
    """调用 DeepSeek 进行高管看板级宏观研判"""
    client = _get_deepseek_client()
    base_system_prompt = (
        "你是一个深谙全球关键矿产、地缘政治与跨国供应链战略的资深产业顾问。\n"
        "你的任务是阅读输入的全球政策情报，过滤微观摩擦，聚焦于宏观地缘与产业政策。\n"
        "输出必须具备极高的宏观战略视野。推演必须量化到产能波及与冶炼成本传导。"
    )
    system_prompt = f"{base_system_prompt}\n\n【⚠️ 核心硬约束：严格按以下 Schema 规范返回 JSON】\n{json.dumps(schema_dict, ensure_ascii=False, indent=2)}"
    try:
        response = client.chat.completions.create(
            model="deepseek-v4-pro",  
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"请对以下原生文本进行过滤与地缘战略推演：\n\n{raw_text}"}
            ],
            response_format={"type": "json_object"},
            temperature=0.2
        )
        content = response.choices[0].message.content
        if content is None:
            print("❌ DeepSeek 返回了空内容")
            return None
        return json.loads(content)
    except Exception as e:
        print(f"❌ DeepSeek 提炼异常: {str(e)}")
        return None

def insert_to_notion(data, source_url):
    """【持久化沉淀】将结构化情报写入 Notion 数据库"""
    notion_token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")

    if not notion_token or not database_id or notion_token == "disabled":
        print("ℹ️ 暂未配置 Notion 凭证，跳过数据库沉淀阶段。")
        return False

    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    pd = data["policy_dynamics"]
    si = data["strategic_implications"]
    md = data["metadata"]

    properties = {
        "政策名称":     {"title":       [{"text": {"content": pd["policy_name_zh"]}}]},
        "原名及出处":   {"rich_text":   [{"text": {"content": pd.get("policy_name_original", "")}}]},
        "核心分类":     {"select":      {"name": data["notion_integration"]["master_tag"]}},
        "颁布国家":     {"select":      {"name": md["country"]}},
        "当前阶段":     {"select":      {"name": pd["current_stage"]}},
        "冲击烈度":     {"select":      {"name": si["supply_chain_impact_level"]}},
        "涉及矿种":     {"multi_select":[{"name": m} for m in md["mineral_types"]]},
        "核心政策手段": {"multi_select":[{"name": c} for c in pd["core_category"]]},
        "核心条款摘要": {"rich_text":   [{"text": {"content": str(pd["substantive_provisions"])[:2000]}}]},
        "原文链接":     {"url":          source_url},
        "DeepSeek 结构化分析": {"rich_text": [{"text": {"content": si["impact_deduction"]}}]},
    }

    # 动态注入生效日期（拦截空值防 Notion 报错）
    effective_date = pd.get("effective_date", "").strip()
    if effective_date and effective_date.lower() != "null":
        properties["生效日期"] = {"date": {"start": effective_date}}

    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            print("🚀 [Notion] 成功打标并持久化沉淀至高管数据库看板。")
            return True
        else:
            print(f"⚠️ [Notion] 写入失败，状态码: {res.status_code}, 详情: {res.text}")
            return False
    except Exception as e:
        print(f"❌ [Notion] 连接异常: {str(e)}")
        return False

def send_dingtalk_alert(data, source_url):
    """【高能时效触达】通过钉钉 Webhook 发送高管宏观视野告警"""
    webhook_url = os.environ.get("DINGTALK_WEBHOOK")
    if not webhook_url or webhook_url == "disabled":
        print("ℹ️ 暂未配置钉钉 Webhook，跳过告警触达阶段。")
        return
        
    if not data["notion_integration"].get("dingtalk_alert_required", False):
        print("ℹ️ 本条政策未达到钉钉实时告警熔断阈值，已通过防打扰机制过滤。")
        return

    headers = {"Content-Type": "application/json"}
    
    markdown_text = (
        f"### 🛑 【宏观地缘与产业政策雷达】核心重磅预警\n\n"
        f"**📌 政策法案**：{data['policy_dynamics']['policy_name_zh']} ({data['policy_dynamics']['policy_name_original']})\n\n"
        f"**🌍 影响范围**：国家: `{data['metadata']['country']}` | 涉及矿种: `{', '.join(data['metadata']['mineral_types'])}`\n\n"
        f"**⚖️ 法律阶段**：`{data['policy_dynamics']['current_stage']}` (生效日: {data['policy_dynamics'].get('effective_date') or '未定'})\n\n"
        f"**🚨 冲击烈度**：<font color='#FF0000'>**{data['strategic_implications']['supply_chain_impact_level']}**</font>\n\n"
        f"--- \n\n"
        f"#### 📜 实质条款拆解：\n"
        f"> {data['policy_dynamics']['substantive_provisions']}\n\n"
        f"#### 🔮 资深顾问研判与纵向供应链成本推演：\n"
        f"{data['strategic_implications']['impact_deduction']}\n\n"
        f"--- \n"
        f"🔗 [点击此处查看原文来源/溯源检索]({source_url})\n\n"
        f"*💡 提示：本条目已同步打标存入 Notion 数字化情报资产库。*"
    )
    
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"🚨 重磅地缘政策预警: {data['metadata']['country']}",
            "text": markdown_text
        }
    }
    
    try:
        res = requests.post(webhook_url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            print("🔔 [钉钉] 高管重磅战略预警卡片推送成功。")
        else:
            print(f"⚠️ [钉钉] 推送失败，详情: {res.text}")
    except Exception as e:
        print(f"❌ [钉钉] 推送异常: {str(e)}")

if __name__ == "__main__":
    PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
    schema = load_schema(os.path.join(PROJECT_DIR, "policy_schema.json"))
    all_active_sources = load_all_sources(os.path.join(PROJECT_DIR, "sources.yaml"))
    
    print(f"📡 数字化情报网络就绪。当前天网总线共挂载 {len(all_active_sources)} 个探测节点。")
    
    for source in all_active_sources:
        print(f"\n🚀 [正在扫描] 目标：{source['agency']} ({source['country']})...")

        source_url = source.get("url", "")  # 静态源默认使用自身 url

        if source.get("feed_type") == "newsapi":
            result = fetch_newsapi(source["query"], source.get("days_back", 7))
            if result:
                fetched_text = result["text"]
                source_url = result.get("source_url", "")
            else:
                fetched_text = None
        elif source.get("feed_type") == "rss":
            fetched_text = fetch_and_parse_rss(source["url"])
        else:
            fetched_text = fetch_and_clean_html(source["url"], source["dom_selector"])

        if fetched_text and len(fetched_text) > 100:
            print(f"📥 [成功捕获] 原始线索已入流。正在调动 DeepSeek 进行矩阵式交叉研判...")
            analysis_result = extract_macro_policy(fetched_text, schema)

            if analysis_result:
                print(f"🎉 战略情报研判完成。开始启动终端合流分流流控...")
                insert_to_notion(analysis_result, source_url)
                send_dingtalk_alert(analysis_result, source_url)
        else:
            print(f"ℹ️ 该节点在当前窗口期内未发现符合布尔条件的宏观政策变动。")
