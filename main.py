import os
import json
import yaml
import requests
import urllib.parse
import urllib3
import concurrent.futures
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from openai import OpenAI

# v3.1: 容忍部分政府网站 SSL 证书问题
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

# ---- 全局配置 ----
# v3.1: NewsAPI 免费套餐仅跑 en（zh/id 由 Google News RSS 多语言兜底覆盖）
NEWSAPI_LANGUAGES = os.environ.get("NEWSAPI_LANGUAGES", "en").split(",")
# v3.1: 每语言最大返回文章数
NEWSAPI_PAGE_SIZE = int(os.environ.get("NEWSAPI_PAGE_SIZE", "5"))
# v3.3 熔断机制：单轮最大 AI 调用次数（控制成本，0=不限）
MAX_AI_CALLS = int(os.environ.get("MAX_AI_CALLS", "8"))
# v3.3 熔断：连续空转上限，达到后跳过后续同 feed_type 源
MAX_CONSECUTIVE_EMPTY = int(os.environ.get("MAX_CONSECUTIVE_EMPTY", "5"))
# v3.3 预筛选：文本最短字符数（低于此值跳过 AI，节省 token）
MIN_TEXT_LENGTH = int(os.environ.get("MIN_TEXT_LENGTH", "300"))
# v3.1: RSS 兜底覆盖的语言（Google News 支持多语言，不受 API 频率限制）
RSS_FALLBACK_LANGUAGES = os.environ.get("RSS_FALLBACK_LANGUAGES", "en,zh,id").split(",")

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
            for mq in generate_queries_from_matrix(matrix_config, max_keywords_per_query=25):
                sources.append({
                    "id": mq["id"],
                    "country": "GLOBAL",
                    "agency": mq["agency"],
                    "feed_type": "newsapi",
                    "query": mq["query"],
                    "days_back": days_back,
                })

        # 第 3 层：自适应热点发现（B 方案 — DeepSeek 推荐当周热点）
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
    """【HTML 轨道】抓取原生网页（v3.1: 容忍 SSL 证书过期/自签名）"""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        response = requests.get(url, headers=headers, timeout=15, verify=False)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        target_zone = soup.select_one(selector)
        if target_zone:
            for noise in target_zone(["script", "style", "nav", "footer", "header"]): noise.extract()
            return target_zone.get_text(separator="\n", strip=True)
        return soup.body.get_text(separator="\n", strip=True) if soup.body else "Empty Body"
    except Exception as e:
        print(f"⚠️ HTML 抓取失败 [{url[:50]}...]: {str(e)[:100]}")
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


# =============================================================================
#  v3.1: 多语言 NewsAPI 查询分叉
#  对每个 query 在 en/zh/id 等多语言轨道上并行查询，消除中文/印尼语盲区
# =============================================================================

def _fetch_newsapi_single_lang(query, days_back, lang, api_key):
    """单语言 NewsAPI 查询（内部辅助函数）"""
    from_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "from": from_date,
        "sortBy": "publishedAt",
        "pageSize": NEWSAPI_PAGE_SIZE,
        "language": lang,
        "apiKey": api_key,
    }
    headers = {"User-Agent": "MacroPolicyRadar/3.1"}
    try:
        res = requests.get(url, params=params, headers=headers, timeout=15)
        res.raise_for_status()
        data = res.json()
        if data.get("status") != "ok":
            print(f"   ⚠️ NewsAPI [{lang}] 返回异常: {data.get('message', '')[:80]}")
            return None
        articles = data.get("articles", [])
        if not articles:
            return None
        combined = []
        first_url = ""
        for idx, art in enumerate(articles[:NEWSAPI_PAGE_SIZE]):
            title = art.get("title", "Untitled")
            desc = art.get("description", "") or ""
            if idx == 0:
                first_url = art.get("url", "")
            combined.append(f"--- [线索 #{idx+1} | lang={lang}] ---\n标题: {title}\n摘要: {desc}")
        return {"text": "\n\n".join(combined), "source_url": first_url, "language": lang}
    except Exception as e:
        print(f"   ⚠️ NewsAPI [{lang}] 请求失败: {e}")
        return None


def _fetch_google_rss_fallback(query, days_back, lang):
    """Google News RSS 兜底（多语言版本）"""
    lang_map = {"en": "en-US", "zh": "zh-CN", "id": "id-ID"}
    hl_gl = lang_map.get(lang, "en-US")
    hl, gl = hl_gl.split("-") if "-" in hl_gl else (hl_gl, "US")
    encoded = urllib.parse.quote(f"{query} when:{days_back}d")
    rss_url = f"https://news.google.com/rss/search?q={encoded}&hl={hl}-{gl}&gl={gl}&ceid={gl}:{hl}"
    rss_text = fetch_and_parse_rss(rss_url, limit=3)
    if rss_text:
        return {"text": rss_text, "source_url": rss_url, "language": lang}
    return None


def fetch_newsapi_multilang(query, days_back=7):
    """
    【NewsAPI + RSS 混合轨道 v3.1】
    - NewsAPI: 仅跑 en（免费套餐 100次/天，节省额度）
    - RSS 兜底: 当 NewsAPI 无结果时，Google News RSS 覆盖 en/zh/id 三语
    """
    api_key = os.environ.get("NEWSAPI_KEY")

    if not api_key or api_key == "disabled":
        print("ℹ️ 未配置 NEWSAPI_KEY，直接走 Google News RSS 多语言管道...")
        all_results = []
        for lang in RSS_FALLBACK_LANGUAGES:
            r = _fetch_google_rss_fallback(query, days_back, lang)
            if r:
                all_results.append(r)
        if all_results:
            merged_text = "\n\n--- 𝕃𝔸ℕ𝔾𝕌𝔸𝔾𝔼 𝕊𝔼ℙ𝔸ℝ𝔸𝕋𝕆ℝ ---\n\n".join(
                f"[轨道: {r['language']}]\n{r['text']}" for r in all_results
            )
            return {"text": merged_text, "source_url": all_results[0]["source_url"]}
        return None

    # NewsAPI 仅查询 en
    en_result = _fetch_newsapi_single_lang(query, days_back, "en", api_key)
    if en_result:
        print(f"   ✅ NewsAPI [en] 轨道命中 {en_result['text'].count('[线索 #')} 条")

    # RSS 多语言补充（始终运行，作为 zh/id 覆盖 + en 补充）
    rss_results = []
    for lang in RSS_FALLBACK_LANGUAGES:
        r = _fetch_google_rss_fallback(query, days_back, lang)
        if r:
            rss_results.append(r)

    # 合并 NewsAPI + RSS 结果
    all_results = []
    if en_result:
        all_results.append(en_result)
    # RSS 结果去重合并（仅补充 NewsAPI 未覆盖的语言）
    rss_langs_seen = set()
    for r in rss_results:
        if r["language"] not in rss_langs_seen:
            rss_langs_seen.add(r["language"])
            all_results.append(r)

    if not all_results:
        return None

    merged_text = "\n\n--- 𝕃𝔸ℕ𝔾𝕌𝔸𝔾𝔼 𝕊𝔼ℙ𝔸ℝ𝔸𝕋𝕆ℝ ---\n\n".join(
        f"[轨道: {r['language']}]\n{r['text']}" for r in all_results
    )
    return {"text": merged_text, "source_url": all_results[0]["source_url"]}


# ---- 向后兼容别名 ----
def fetch_newsapi(query, days_back=7):
    """向后兼容：内部委托给多语言版本"""
    return fetch_newsapi_multilang(query, days_back)


def generate_queries_from_matrix(matrix_config, max_keywords_per_query=8):
    """
    方案 A：从声明式矩阵自动生成全覆盖查询（矿种 × 关键词笛卡尔积）。
    v3.1: 关键词分批次，避免单条查询过长超出 NewsAPI URL 限制。
    """
    minerals = matrix_config.get("minerals", [])
    keywords = matrix_config.get("policy_keywords", [])
    queries = []

    # 将关键词分成多个批次（每批不超过 max_keywords_per_query 个）
    keyword_batches = []
    for i in range(0, len(keywords), max_keywords_per_query):
        batch = keywords[i:i + max_keywords_per_query]
        keyword_batches.append(batch)

    # 每个矿种 × 每批关键词 → 一条查询
    for mineral in minerals:
        m = f'"{mineral}"' if " " in mineral else mineral
        for bi, batch in enumerate(keyword_batches):
            clause = " OR ".join(f'"{kw}"' if " " in kw else kw for kw in batch)
            query_str = f"{m} AND ({clause})"
            queries.append({
                "id": f"matrix_{mineral.lower().replace(' ', '_').replace('-', '_')}_b{bi+1}",
                "query": query_str,
                "agency": f"NewsAPI 矩阵扫描 [{mineral}] 批次 {bi+1}/{len(keyword_batches)}",
            })

    # 兜底宽泛查询（同样分批次）
    broad_minerals = " OR ".join(f'"{m}"' if " " in m else m for m in ["critical minerals", "battery metals", "energy transition minerals"])
    for bi, batch in enumerate(keyword_batches):
        clause = " OR ".join(f'"{kw}"' if " " in kw else kw for kw in batch)
        broad = f"({broad_minerals}) AND ({clause})"
        queries.append({
            "id": f"matrix_broad_catchall_b{bi+1}",
            "query": broad,
            "agency": f"NewsAPI 矩阵扫描 [Broad Catch-All] 批次 {bi+1}/{len(keyword_batches)}",
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
    """方案 B：让 DeepSeek 推荐本周矿产政策热点查询（v3.1: 提示词增强）"""
    client = _get_deepseek_client()
    prompt = (
        "你是一个全球关键矿产政策追踪专家。请基于你对近期地缘政治和产业动态的了解，"
        f"推荐 {max_count} 条本周最值得关注的矿产政策英文搜索查询。"
        "每条查询应组合具体矿种、国家/地区和政策术语。"
        "请特别关注以下近期热点方向：\n"
        "- 国家统购统销/国内供应义务（Domestic Supply Obligation, DSI）\n"
        "- 供应链尽责立法/ESG合规（Supply Chain Due Diligence）\n"
        "- 碳边境调节机制（CBAM）\n"
        "- 关键矿产战略/绿色补贴（Critical Minerals Strategy, IRA）\n"
        "- 价格管制/战略储备（Price Control, Strategic Stockpile）\n"
        f'请以 JSON 格式返回，格式如下：{{"queries": ["query1", "query2", ...]}}'
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


# =============================================================================
#  v3.1: 增强版系统提示词
#  显式覆盖 DSI/供应链尽责/ESG/绿色壁垒等新型政策类型
# =============================================================================

_SYSTEM_PROMPT_V31 = (
    "你是一个深谙全球关键矿产、地缘政治与跨国供应链战略的资深产业顾问。\n"
    "你的任务是阅读输入的全球政策情报，过滤微观摩擦，聚焦于宏观地缘与产业政策。\n\n"
    "【重点监控的政策类型（必须识别，不可遗漏）】\n"
    "1. 传统贸易壁垒：出口禁令/限制、关税调整、配额、外资股权限制、税率矿权变动\n"
    "2. 资源主权措施：国有化、征收、国内供应义务(DSI)/统购统销、国家强制采购、战略储备\n"
    "3. 供应链治理：供应链尽责法/尽职调查、ESG合规强制令、强迫劳动预防法案\n"
    "4. 绿色转型：碳边境调节机制(CBAM)、绿色补贴/IRA、关键矿产战略、产业补贴\n"
    "5. 价格干预：价格管制、暴利税、补贴取消、大宗商品平准基金\n\n"
    "【分析要求】\n"
    "- 对供应链尽责类政策：推演合规成本上升幅度、跨境合规信息穿透风险\n"
    "- 对 DSI/统购统销类政策：推演对矿种出口量的量化影响、替代采购国可行性\n"
    "- 对绿色壁垒类政策：推演碳关税对中下游冶炼成本的传导\n"
    "- 输出必须具备极高的宏观战略视野。推演必须量化到产能波及与冶炼成本传导。\n\n"
    "【特别指令：政策名称必须是极具新闻冲击力、高度概括核心动作的『情报简报标题』，让高管不点开正文也能看懂该政策对跨国供应链的具体限制。】"
)


def extract_macro_policy(raw_text, schema_dict):
    """调用 DeepSeek 进行高管看板级宏观研判（v3.1: 增强提示词）"""
    client = _get_deepseek_client()
    system_prompt = (
        f"{_SYSTEM_PROMPT_V31}\n\n"
        f"【⚠️ 核心硬约束：严格按以下 Schema 规范返回 JSON】\n"
        f"{json.dumps(schema_dict, ensure_ascii=False, indent=2)}"
    )
    try:
        response = client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"请对以下原生文本进行过滤与地缘战略推演：\n\n{raw_text}"}
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            timeout=60,  # v3.3 熔断：单次 AI 调用超时 60s
        )
        content = response.choices[0].message.content
        if content is None:
            print("❌ DeepSeek 返回了空内容")
            return None
        return json.loads(content)
    except Exception as e:
        print(f"❌ DeepSeek 提炼异常: {str(e)}")
        return None


# =============================================================================
#  v3.1: Notion 去重检查
#  在入库前查询同政策名是否已存在，避免重复创建
# =============================================================================

def _notion_search_pages(policy_name_zh, policy_name_original=""):
    """
    在 Notion 数据库中搜索是否已存在同名政策。
    返回 (exists: bool, existing_page_id: str | None)
    """
    notion_token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if not notion_token or not database_id or notion_token == "disabled":
        return False, None

    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    # 按政策名称过滤（Notion 的 title 属性过滤）
    payload = {
        "filter": {
            "or": []
        },
        "page_size": 5,
    }

    if policy_name_zh:
        payload["filter"]["or"].append({
            "property": "政策名称",
            "title": {"contains": policy_name_zh}
        })

    if policy_name_original:
        payload["filter"]["or"].append({
            "property": "原名及出处",
            "rich_text": {"contains": policy_name_original[:100]}
        })

    if not payload["filter"]["or"]:
        return False, None

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            results = res.json().get("results", [])
            if results:
                return True, results[0]["id"]
        return False, None
    except Exception as e:
        print(f"   ⚠️ Notion 去重查询异常: {e}")
        return False, None


def _notion_update_policy(page_id, data, source_url):
    """
    对已有政策执行增量更新：更新当前阶段、条款摘要、冲击烈度、分析结论。
    保留原有的创建时间和政策名称。
    """
    notion_token = os.environ.get("NOTION_TOKEN")
    if not notion_token or notion_token == "disabled":
        return False

    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    pd = data["policy_dynamics"]
    si = data["strategic_implications"]
    md = data["metadata"]

    # 仅更新可能变化的字段
    properties = {
        "当前阶段": {"select": {"name": pd["current_stage"]}},
        "冲击烈度": {"select": {"name": si["supply_chain_impact_level"]}},
        "核心条款摘要": {"rich_text": [{"text": {"content": str(pd["substantive_provisions"])[:2000]}}]},
        "DeepSeek 结构化分析": {"rich_text": [{"text": {"content": si["impact_deduction"]}}]},
    }

    # 动态注入生效日期
    effective_date = (pd.get("effective_date") or "").strip()
    if effective_date and effective_date.lower() != "null":
        properties["生效日期"] = {"date": {"start": effective_date}}

    # v3.1: 更新政策维度标签
    if pd.get("policy_dimension"):
        properties["政策维度"] = {"select": {"name": pd["policy_dimension"]}}

    # 更新核心政策手段
    if pd.get("core_category"):
        properties["核心政策手段"] = {"multi_select": [{"name": c} for c in pd["core_category"]]}

    payload = {"properties": properties}

    try:
        res = requests.patch(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            print("🔄 [Notion] 检测到重复政策，已执行增量更新（阶段/条款/烈度）。")
            return True
        else:
            print(f"   ⚠️ [Notion] 增量更新失败，状态码: {res.status_code}")
            return False
    except Exception as e:
        print(f"   ❌ [Notion] 更新连接异常: {str(e)}")
        return False


# ---- 标题增强：ISO 代码到中文国名映射 ----
_COUNTRY_ZH_MAP = {
    "CN": "中国",
    "ID": "印度尼西亚",
    "CD": "刚果（金）",
    "CL": "智利",
    "AU": "澳大利亚",
    "EU": "欧盟",
    "ZW": "津巴布韦",
    "GLOBAL": "",
}

# ---- 标题增强：政策阶段枚举到中文标签映射 ----
_STAGE_ZH_MAP = {
    "Proposal": "提案阶段",
    "Under_Debate": "审议中",
    "Approved_Not_Effective": "已通过待生效",
    "Fully_Effective": "已生效",
    "Suspended": "暂停",
}

# ---- 标题增强：矿种英文到中文映射 ----
_MINERAL_ZH_MAP = {
    "Lithium": "锂", "Cobalt": "钴", "Nickel": "镍",
    "Copper": "铜", "Rare Earths": "稀土", "Graphite": "石墨",
    "Others": "其他",
}


def _enhance_policy_title(policy_name_zh, country, current_stage, mineral_types=None):
    """
    【v3.3 情报级标题增强 — Action-Oriented Intelligence Title】
    匹配强结构化公式：[{国家码}] {动作+影响}：{法案核心} ({矿种})（{阶段}）
    - 若标题缺 [国家码] 前缀，自动注入
    - 若标题缺矿种后缀，自动注入（中文缩写）
    - 若标题缺阶段，自动注入（兜底）
    - 若标题已符情报级格式，保持原样
    """
    import re

    if not policy_name_zh:
        return policy_name_zh

    enhanced = policy_name_zh

    # 1. 检测并注入 [{国家码}] 前缀
    country_tag = f"[{country}]"
    has_country_tag = bool(re.search(r'\[([A-Z]{2}|EU|GLOBAL)\]', enhanced[:15]))
    if country and country != "GLOBAL" and not has_country_tag:
        enhanced = f"{country_tag} {enhanced}"

    # 2. 检测并注入矿种后缀
    if mineral_types and mineral_types != ["Others"]:
        minerals_zh = "/".join(_MINERAL_ZH_MAP.get(m, m) for m in mineral_types)
        # 检测是否已有 (矿种名) 格式的后缀
        has_mineral_tag = bool(re.search(
            r'\((?:锂|钴|镍|铜|稀土|石墨|Lithium|Cobalt|Nickel|Copper|Rare\s*Earths?|Graphite)',
            enhanced
        ))
        if not has_mineral_tag:
            enhanced = f"{enhanced} ({minerals_zh})"

    # 3. 检测并注入阶段后缀（兜底 — AI 应在标题中体现动作而非阶段，但保留此安全网）
    stage_zh = _STAGE_ZH_MAP.get(current_stage, "")
    has_stage = any(label in enhanced for label in _STAGE_ZH_MAP.values())
    if stage_zh and not has_stage:
        enhanced = f"{enhanced}（{stage_zh}）"

    return enhanced


def insert_to_notion(data, source_url):
    """
    【持久化沉淀 v3.1】将结构化情报写入 Notion 数据库。
    入库前执行去重检查：同名政策存在则增量更新阶段和条款，不存在则新建。
    """
    notion_token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")

    if not notion_token or not database_id or notion_token == "disabled":
        print("ℹ️ 暂未配置 Notion 凭证，跳过数据库沉淀阶段。")
        return False

    pd = data["policy_dynamics"]
    si = data["strategic_implications"]
    md = data["metadata"]

    policy_name_zh = pd.get("policy_name_zh", "")
    policy_name_original = pd.get("policy_name_original", "")

    # ---- v3.1: 去重检查（使用原始标题，确保与旧记录兼容）----
    exists, existing_page_id = _notion_search_pages(policy_name_zh, policy_name_original)
    if exists and existing_page_id:
        return _notion_update_policy(existing_page_id, data, source_url)

    # ---- v3.3: 情报级标题增强（仅用于 Notion 展示，不参与去重）----
    enhanced_title = _enhance_policy_title(
        policy_name_zh,
        md.get("country", ""),
        pd.get("current_stage", ""),
        md.get("mineral_types", []),
    )

    # ---- 新建记录 ----
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    properties = {
        "政策名称":     {"title":       [{"text": {"content": enhanced_title}}]},
        "原名及出处":   {"rich_text":   [{"text": {"content": policy_name_original}}]},
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

    # v3.1: 政策维度标签
    if pd.get("policy_dimension"):
        properties["政策维度"] = {"select": {"name": pd["policy_dimension"]}}

    # 动态注入生效日期（拦截空值防 Notion 报错）
    effective_date = (pd.get("effective_date") or "").strip()
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
            print(f"⚠️ [Notion] 写入失败，状态码: {res.status_code}, 详情: {res.text[:300]}")
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

    pd = data['policy_dynamics']
    si = data['strategic_implications']
    md = data['metadata']

    # v3.1: 在告警中展示政策维度
    dimension_label = pd.get('policy_dimension', '')
    dimension_line = f"\n\n**🏷️ 政策维度**：`{dimension_label}`" if dimension_label else ""

    markdown_text = (
        f"### 🛑 【宏观地缘与产业政策雷达】核心重磅预警\n\n"
        f"**📌 政策法案**：{pd.get('policy_name_zh') or '(未命名)'} ({pd.get('policy_name_original', '')})\n\n"
        f"**🌍 影响范围**：国家: `{md['country']}` | 涉及矿种: `{', '.join(md['mineral_types'])}`"
        f"{dimension_line}\n\n"
        f"**⚖️ 法律阶段**：`{pd['current_stage']}` (生效日: {pd.get('effective_date') or '未定'})\n\n"
        f"**🚨 冲击烈度**：<font color='#FF0000'>**{si['supply_chain_impact_level']}**</font>\n\n"
        f"--- \n\n"
        f"#### 📜 实质条款拆解：\n"
        f"> {pd['substantive_provisions']}\n\n"
        f"#### 🔮 资深顾问研判与纵向供应链成本推演：\n"
        f"{si['impact_deduction']}\n\n"
        f"--- \n"
        f"🔗 [点击此处查看原文来源/溯源检索]({source_url})\n\n"
        f"*💡 提示：本条目已同步打标存入 Notion 数字化情报资产库。*"
    )

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"🚨 重磅地缘政策预警: {md['country']}",
            "text": markdown_text
        }
    }

    try:
        res = requests.post(webhook_url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            print("🔔 [钉钉] 高管重磅战略预警卡片推送成功。")
        else:
            print(f"⚠️ [钉钉] 推送失败，详情: {res.text[:200]}")
    except Exception as e:
        print(f"❌ [钉钉] 推送异常: {str(e)}")


# =============================================================================
#  主流程
# =============================================================================

if __name__ == "__main__":
    PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
    schema = load_schema(os.path.join(PROJECT_DIR, "policy_schema.json"))
    all_active_sources = load_all_sources(os.path.join(PROJECT_DIR, "sources.yaml"))

    print(f"📡 数字化情报网络就绪。当前天网总线共挂载 {len(all_active_sources)} 个探测节点。")
    print(f"🌐 NewsAPI: {', '.join(NEWSAPI_LANGUAGES)} | RSS 兜底: {', '.join(RSS_FALLBACK_LANGUAGES)}")
    print(f"🛡️ 熔断配置: 最大AI调用={MAX_AI_CALLS} | 连续空转上限={MAX_CONSECUTIVE_EMPTY} | 最小文本长度={MIN_TEXT_LENGTH}")

    ai_call_count = 0
    consecutive_empty = 0
    last_empty_feed_type = None

    for source in all_active_sources:
        source_notes = source.get("notes", "")
        notes_suffix = f" [📝 {source_notes}]" if source_notes else ""
        print(f"\n🚀 [正在扫描] 目标：{source['agency']} ({source['country']}){notes_suffix}...")

        # ---- v3.3 熔断：连续空转检测 ----
        if consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
            print(f"🛑 [熔断] 连续 {consecutive_empty} 个节点无有效政策，跳过后续 node。")
            break

        source_url = source.get("url", "")

        if source.get("feed_type") == "newsapi":
            result = fetch_newsapi(source["query"], source.get("days_back", 7))
            if result:
                fetched_text = result["text"]
                source_url = result.get("source_url", source_url)
            else:
                fetched_text = None
        elif source.get("feed_type") == "rss":
            fetched_text = fetch_and_parse_rss(source["url"])
        else:
            fetched_text = fetch_and_clean_html(source["url"], source["dom_selector"])

        if not fetched_text or len(fetched_text) < MIN_TEXT_LENGTH:
            if fetched_text is None:
                print(f"ℹ️ 该节点未捕获到有效内容（网络超时或选择器无匹配）。")
            else:
                print(f"ℹ️ 该节点内容过短（{len(fetched_text)} 字符），跳过 AI 调用。")

            # 连续空转计数
            ft = source.get("feed_type", "")
            if ft == last_empty_feed_type:
                consecutive_empty += 1
            else:
                consecutive_empty = 1
                last_empty_feed_type = ft
            continue

        # ---- v3.3 熔断：AI 调用次数上限 ----
        if MAX_AI_CALLS > 0 and ai_call_count >= MAX_AI_CALLS:
            print(f"🛑 [熔断] 已达本轮 AI 调用上限 ({MAX_AI_CALLS})，跳过后续分析与入库。")
            break

        print(f"📥 [成功捕获] 原始线索已入流 ({len(fetched_text)} 字符)。正在调动 DeepSeek 进行矩阵式交叉研判...")
        analysis_result = extract_macro_policy(fetched_text, schema)
        ai_call_count += 1

        if analysis_result:
            # v3.1: 校验 DeepSeek 返回的 JSON 是否包含所有必需字段，防止 KeyError 崩溃
            required_keys = ["metadata", "policy_dynamics", "strategic_implications", "notion_integration"]
            missing = [k for k in required_keys if k not in analysis_result]
            if missing:
                print(f"⚠️ DeepSeek 返回 JSON 缺少关键字段: {missing}，跳过本条。")
                consecutive_empty += 1
                continue

            # v3.1: 噪音过滤 — 跳过 DeepSeek 返回的"无政策"结果
            pd = analysis_result.get("policy_dynamics", {})
            name_zh = pd.get("policy_name_zh") or ""
            noise_patterns = ["无相关", "无关键矿产", "无宏观", "无相关政策", "无重大政策",
                              "无实质性", "未发现", "暂无", "未检测到", "无有效政策",
                              "无有效信息", "无效政策"]
            if any(p in name_zh for p in noise_patterns):
                print(f"ℹ️ DeepSeek 判定为无宏观政策（{name_zh}），已过滤。")
                consecutive_empty += 1
                continue

            # 有效政策 → 重置空转计数
            consecutive_empty = 0
            last_empty_feed_type = None

            print(f"🎉 战略情报研判完成。开始启动终端合流分流流控...")
            insert_to_notion(analysis_result, source_url)
            send_dingtalk_alert(analysis_result, source_url)
