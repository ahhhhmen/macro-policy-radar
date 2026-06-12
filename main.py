import os
import json
import yaml
import requests
import urllib.parse
from bs4 import BeautifulSoup
from openai import OpenAI

def load_schema(schema_path):
    """加载认知滤网 Schema"""
    with open(schema_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_all_sources(yaml_path):
    """同时加载静态靶向源与动态聚合网"""
    with open(yaml_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
        sources = []
        for src in config.get("macro_sources", []):
            if src.get("enabled", True): sources.append(src)
        for agg in config.get("dynamic_aggregators", []):
            if agg.get("enabled", True) and agg.get("type") == "google_news_rss":
                raw_query = agg["query"]
                if "time_window" in agg: raw_query += f" when:{agg['time_window']}"
                encoded_query = urllib.parse.quote(raw_query)
                rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
                sources.append({
                    "id": agg["id"], "country": "GLOBAL",
                    "agency": f"全球动态天网 [{'/'.join(agg['mineral_focus'])}]",
                    "feed_type": "rss", "url": rss_url
                })
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

def extract_macro_policy(raw_text, schema_dict):
    """调用 DeepSeek 进行高管看板级宏观研判"""
    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com")
    )
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

def insert_to_notion(data):
    """【持久化沉淀】将结构化情报无缝写入 Notion 数据库"""
    notion_token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    
    if not notion_token or not database_id:
        print("ℹ️ 暂未配置 Notion 凭证，跳过数据库沉淀阶段。")
        return False
        
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    # 动态匹配你修订后的 Schema 4 模块结构
    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            "政策名称": {"title": [{"text": {"content": data["policy_dynamics"]["policy_name_zh"]}}]},
            "核心分类": {"select": {"name": data["notion_integration"]["master_tag"]}}, # 强制打上 [宏观地缘与产业政策] 标签
            "颁布国家": {"select": {"name": data["metadata"]["country"]}},
            "当前阶段": {"select": {"name": data["policy_dynamics"]["current_stage"]}},
            "冲击烈度": {"select": {"name": data["strategic_implications"]["supply_chain_impact_level"]}},
            "涉及矿种": {"multi_select": [{"name": m} for m in data["metadata"]["mineral_types"]]}
        },
        "children": [
            {
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "📜 核心条款拆解"}}]}
            },
            {
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": data["policy_dynamics"]["substantive_provisions"]}}]}
            },
            {
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "🔮 资深顾问战略推演（供应链冲击/成本传导）"}}]}
            },
            {
                "object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": data["strategic_implications"]["impact_deduction"]}}]}
            }
        ]
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

def send_dingtalk_alert(data):
    """【高能时效触达】通过钉钉 Webhook 发送具备高管宏观视野的 Markdown 告警"""
    webhook_url = os.environ.get("DINGTALK_WEBHOOK")
    if not webhook_url:
        print("ℹ️ 暂未配置钉钉 Webhook，跳过告警触达阶段。")
        return
        
    # 严格按照 Schema 的告警规则熔断
    if not data["notion_integration"].get("dingtalk_alert_required", False):
        print("ℹ️ 本条政策未达到钉钉实时告警熔断阈值，已通过防打扰机制过滤。")
        return

    headers = {"Content-Type": "application/json"}
    
    # 构建具备极高宏观战略视野的 Markdown 排版
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
        f"*💡 提示：本条目已同步打标存入 Notion 跨国数字化情报网络资产库。*"
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
    PROJECT_DIR = "/Users/xiefang/Projects/macro-policy-radar"
    schema = load_schema(f"{PROJECT_DIR}/policy_schema.json")
    all_active_sources = load_all_sources(f"{PROJECT_DIR}/sources.yaml")
    
    print(f"📡 数字化情报网络就绪。当前天网总线共挂载 {len(all_active_sources)} 个探测节点。")
    
    for source in all_active_sources:
        print(f"\n🚀 [正在扫描] 目标：{source['agency']} ({source['country']})...")
        
        if source.get("feed_type") == "rss":
            fetched_text = fetch_and_parse_rss(source['url'])
        else:
            fetched_text = fetch_and_clean_html(source['url'], source['dom_selector'])
            
        if fetched_text and len(fetched_text) > 100:
            print(f"📥 [成功捕获] 原始线索已入流。正在调动 DeepSeek 进行矩阵式交叉研判...")
            analysis_result = extract_macro_policy(fetched_text, schema)
            
            if analysis_result:
                print(f"🎉 战略情报研判完成。开始启动终端合流分流流控...")
                
                # 终端流控闭环 1：入库 Notion 知识沉淀
                insert_to_notion(analysis_result)
                
                # 终端流控闭环 2：触达钉钉高管群实时告警
                send_dingtalk_alert(analysis_result)
        else:
            print(f"ℹ️ 该节点在当前窗口期内未发现符合布尔条件的宏观政策变动。")