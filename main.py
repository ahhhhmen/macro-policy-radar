import os
import re
import json
import yaml
import time
import hmac
import hashlib
import base64
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
# v3.5: RSS 文章发布日期硬过滤窗口（天）—— 超过此天数的旧文直接丢弃
RSS_MAX_AGE_DAYS = int(os.environ.get("RSS_MAX_AGE_DAYS", "14"))

def load_schema(schema_path):
    """加载认知滤网 Schema"""
    with open(schema_path, 'r', encoding='utf-8') as f:
        return json.load(f)


# =============================================================================
#  v4.2: 活体基线知识库 —— 解耦代码与业务基线，YAML 文件可独立维护
# =============================================================================

def load_knowledge_baselines(yaml_path):
    """
    加载基线知识库。
    返回 (dict{country_code: [rule_strings]}, last_updated: str)
    示例：{'ID': ['镍原矿自2020年起禁出口', ...], ...}, '2026-06-22'
    """
    if not os.path.isfile(yaml_path):
        print(f"ℹ️ 基线知识库文件不存在 ({yaml_path})，跳过注入。")
        return {}, "unknown"
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        result = {}
        for code, entry in config.get("baselines", {}).items():
            if entry and entry.get("rules"):
                result[code] = entry["rules"]
        return result, config.get("last_updated", "unknown")
    except Exception as e:
        print(f"⚠️ 基线知识库加载失败: {str(e)[:100]}")
        return {}, "unknown"


def _inject_baseline(country_code, baselines, last_updated):
    """
    【v4.2 活体基线注入】按国家匹配基线文本，供注入 system prompt。
    返回注入文本块（空字符串如果该国无基线）。
    """
    if not country_code or country_code not in baselines:
        return ""
    rules = baselines[country_code]
    if not rules:
        return ""
    joined = "；".join(rules)
    return (
        f"\n\n【当前历史基线（knowledge_baselines.yaml · 最后人工审核：{last_updated}）】\n"
        f"该国矿种现行政策现状：{joined}\n"
        f"【⚠️ 基线覆写规则】上述基线是分析师推演的起点锚点。若本次情报明确宣告了对旧基线的废除、逆转或重大修改（如官方宣布解除出口禁令）：\n"
        f"  1. 在 industry_baseline_recall 中声明『⚠️ 历史基线已被打破』并解释新变化\n"
        f"  2. 绝对服从最新官方情报进行推演，不得固守旧基线\n"
        f"  3. 将 baseline_shift_detected 设为 true\n"
    )

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


# v4.0: 二级抓取最小正文长度门槛 —— 低于此值视为抓取失败，降级为摘要
MIN_FULLTEXT_LENGTH = int(os.environ.get("MIN_FULLTEXT_LENGTH", "300"))


def fetch_article_full_text(article_url):
    """
    【v4.0 二级抓取】解析单篇文章 URL，抽取正文。
    用于 RSS/NewsAPI 线索的深度补全 —— 把"标题+一句话摘要"升级为"原文全文"。

    优先级：
      1. <article> / <main> 语义标签
      2. 常见 CMS 正文选择器（.post-content, .article-body, .entry-content, .content-body）
      3. 兜底：所有 <p> 文本拼接

    返回 (full_text | None, depth: "full" | "shallow")
      - full_text 长度 ≥ MIN_FULLTEXT_LENGTH → ("...", "full")
      - 抓取失败或过短 → (None, "shallow")
    """
    if not article_url or not article_url.startswith("http"):
        return None, "shallow"

    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        res = requests.get(article_url, headers=headers, timeout=10, verify=False, allow_redirects=True)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')

        # 剥离噪声标签
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]):
            tag.extract()

        # 优先级 1-2：语义标签 + 常见 CMS 正文选择器
        candidates = [
            soup.find('article'),
            soup.find('main'),
            soup.select_one('.post-content'),
            soup.select_one('.article-body'),
            soup.select_one('.article-content'),
            soup.select_one('.entry-content'),
            soup.select_one('.content-body'),
            soup.select_one('.news-content'),
            soup.select_one('[role="main"]'),
        ]
        target = next((c for c in candidates if c and len(c.get_text(strip=True)) >= MIN_FULLTEXT_LENGTH), None)

        if target:
            text = target.get_text(separator="\n", strip=True)
        else:
            # 优先级 3：兜底拼所有 <p>
            paragraphs = soup.find_all('p')
            text = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

        if len(text) >= MIN_FULLTEXT_LENGTH:
            return text, "full"
        # 过短 —— 可能是 paywall / JS 渲染 / 登录墙，如实降级
        return None, "shallow"
    except Exception as e:
        print(f"   ⚠️ [二级抓取] 正文解析失败 [{article_url[:60]}...]: {str(e)[:80]}")
        return None, "shallow"

def _parse_rss_date(pub_date_str):
    """解析 RSS <pubDate>（RFC 822 格式），返回 timezone-aware datetime；失败返回 None"""
    if not pub_date_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub_date_str.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_within_age(pub_date_str, max_days=RSS_MAX_AGE_DAYS):
    """
    判断 RSS 文章发布日期是否在 max_days 天内。
    日期为空或解析失败时保守放行（返回 True），避免误杀无 pubDate 的优质源。
    """
    if not pub_date_str:
        return True
    dt = _parse_rss_date(pub_date_str)
    if dt is None:
        return True
    age = datetime.now(timezone.utc) - dt
    return age <= timedelta(days=max_days)


def resolve_google_news_url(google_url):
    """
    解码 Google News 跳转链（news.google.com/rss/articles/...），跟随重定向拿到真实源 URL。
    非 Google News 链接或解码失败时原样返回，保证不 worse than before。
    """
    if not google_url or "news.google.com" not in google_url:
        return google_url
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        res = requests.get(google_url, headers=headers, timeout=10,
                           allow_redirects=True, verify=False)
        final_url = res.url
        if final_url and "news.google.com" not in final_url and final_url.startswith("http"):
            return final_url
        return google_url
    except Exception as e:
        print(f"⚠️ Google News 链接解码失败 [{google_url[:60]}...]: {str(e)[:80]}")
        return google_url


def fetch_and_parse_rss(url, limit=3):
    """
    【RSS 轨道】通用 XML 解析引擎
    v3.4: 提取每条 item 的原文链接
    v3.5: 解析 <pubDate> 做发布日期硬过滤（超 RSS_MAX_AGE_DAYS 丢弃）+ Google News 链接解码
    v4.0: 二级抓取 —— 对每个 item link 抓取原文正文，升级信息深度（失败降级为摘要）
    """
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'xml')
        items = soup.find_all('item')
        if not items: return None
        combined_policies = []
        links = []
        skipped_old = 0
        depth_max = "shallow"  # 记录本源最高信息深度
        for idx, item in enumerate(items[:limit]):
            title = item.find('title').get_text(strip=True) if item.find('title') else "Untitled"
            desc_node = item.find('description') or item.find('summary')
            raw_desc = desc_node.get_text(strip=True) if desc_node else ""
            clean_desc = BeautifulSoup(raw_desc, 'html.parser').get_text(separator=" ", strip=True)

            # v3.5: 发布日期硬过滤 —— 超 RSS_MAX_AGE_DAYS 的旧文直接丢弃
            pub_node = item.find('pubDate') or item.find('published') or item.find('{http://purl.org/dc/elements/1.1/}date')
            pub_date_str = pub_node.get_text(strip=True) if pub_node else ""
            if not _is_within_age(pub_date_str):
                skipped_old += 1
                continue

            # v3.4: 提取原文链接；v3.5: Google News 跳转链解码
            item_link = ""
            link_node = item.find('link')
            if link_node:
                item_link = link_node.get_text(strip=True)

            # v4.0: 二级抓取 —— 对原文链接解码并抓正文
            fulltext_block = ""
            item_depth = "shallow"
            if item_link:
                item_link = resolve_google_news_url(item_link)
                links.append(item_link)
                full_text, item_depth = fetch_article_full_text(item_link)
                if full_text:
                    # 截断超长正文，控制 token 成本
                    full_text_trimmed = full_text[:4000]
                    fulltext_block = f"\n【原文正文（v4.0 二级抓取）】\n{full_text_trimmed}"
                    depth_max = "full"
                    print(f"   📥 [二级抓取] 成功补全正文 {len(full_text)} 字符 [{item_link[:50]}...]")
                else:
                    fulltext_block = f"\n【⚠️ 原文正文抓取失败，仅基于标题+摘要研判，信息深度: shallow】"

            pub_line = f"\n发布日期: {pub_date_str}" if pub_date_str else ""
            link_line = f"\n原文链接: {item_link}" if item_link else ""
            combined_policies.append(
                f"--- [情报线索 #{idx+1} | depth={item_depth}] ---\n"
                f"标题: {title}\n详情摘要: {clean_desc}{fulltext_block}{pub_line}{link_line}"
            )
        if skipped_old:
            print(f"   ⏰ [RSS] 过滤 {skipped_old} 条超过 {RSS_MAX_AGE_DAYS} 天的旧文。")
        if not combined_policies:
            print(f"   ⏰ [RSS] 本源全部 item 超过 {RSS_MAX_AGE_DAYS} 天，整源跳过。")
            return None
        return {"text": "\n\n".join(combined_policies), "links": links, "source_depth": depth_max}
    except Exception as e:
        print(f"⚠️ RSS 接口请求失败: {str(e)}")
        return None



# =============================================================================
#  v3.1: 多语言 NewsAPI 查询分叉
#  对每个 query 在 en/zh/id 等多语言轨道上并行查询，消除中文/印尼语盲区
# =============================================================================

def _fetch_newsapi_single_lang(query, days_back, lang, api_key):
    """单语言 NewsAPI 查询（内部辅助函数，v4.0: 头条线索二级抓取补全正文）"""
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
        depth_max = "shallow"
        for idx, art in enumerate(articles[:NEWSAPI_PAGE_SIZE]):
            title = art.get("title", "Untitled")
            desc = art.get("description", "") or ""
            art_url = resolve_google_news_url(art.get("url", ""))
            if idx == 0:
                first_url = art_url
                # v4.0: 头条线索二级抓取补全正文
                full_text, item_depth = fetch_article_full_text(art_url)
                if full_text:
                    full_text_trimmed = full_text[:4000]
                    combined.append(
                        f"--- [线索 #{idx+1} | lang={lang} | depth=full] ---\n"
                        f"标题: {title}\n摘要: {desc}\n【原文正文（v4.0 二级抓取）】\n{full_text_trimmed}"
                    )
                    depth_max = "full"
                    print(f"   📥 [二级抓取] NewsAPI 头条正文补全 {len(full_text)} 字符 [{art_url[:50]}...]")
                    continue
            combined.append(f"--- [线索 #{idx+1} | lang={lang} | depth=shallow] ---\n标题: {title}\n摘要: {desc}")
        return {"text": "\n\n".join(combined), "source_url": first_url, "language": lang, "source_depth": depth_max}
    except Exception as e:
        print(f"   ⚠️ NewsAPI [{lang}] 请求失败: {e}")
        return None


def _fetch_google_rss_fallback(query, days_back, lang):
    """Google News RSS 兜底（多语言版本，v4.0: 透传 source_depth）"""
    lang_map = {"en": "en-US", "zh": "zh-CN", "id": "id-ID"}
    hl_gl = lang_map.get(lang, "en-US")
    hl, gl = hl_gl.split("-") if "-" in hl_gl else (hl_gl, "US")
    encoded = urllib.parse.quote(f"{query} when:{days_back}d")
    rss_url = f"https://news.google.com/rss/search?q={encoded}&hl={hl}-{gl}&gl={gl}&ceid={gl}:{hl}"
    rss_result = fetch_and_parse_rss(rss_url, limit=3)
    if rss_result:
        # v3.4: 优先使用 RSS item 中提取的原文链接，fallback 到 RSS 订阅地址
        best_url = rss_result["links"][0] if rss_result.get("links") else rss_url
        return {
            "text": rss_result["text"],
            "source_url": best_url,
            "language": lang,
            "source_depth": rss_result.get("source_depth", "shallow"),
        }
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
            merged_depth = "full" if any(r.get("source_depth") == "full" for r in all_results) else "shallow"
            return {"text": merged_text, "source_url": all_results[0]["source_url"], "source_depth": merged_depth}
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
    merged_depth = "full" if any(r.get("source_depth") == "full" for r in all_results) else "shallow"
    return {"text": merged_text, "source_url": all_results[0]["source_url"], "source_depth": merged_depth}


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
#  v4.0: 事实优先系统提示词（对标 Rhodium / 情报界 ICD 203）
#  核心改动：删除"量化强制"幻觉指令，引入事实-分析分层 + 置信度自评
# =============================================================================

_SYSTEM_PROMPT_V40 = (
    "你是一位遵循 Wood Mackenzie / BloombergNEF 分析范式的高级产业顾问。\n"
    "你的分析必须符合顶尖智库标准：事实隔离 → 基线定锚 → 节点定向推演。\n"
    "你的任务是阅读输入的全球政策情报，严格区分『原文事实』与『分析师推演』。\n\n"
    "【第一原则：事实准确是底线，高于时效、重要性、相关性】\n"
    "- 实质条款（substantive_provisions）必须忠实于原文，不得增加原文未出现的事实、数字、机构动作、政策手段。\n"
    "- 原文未提及的内容，禁止补全。宁可写『原文未披露』，不可编造。\n"
    "- 任何数字（百分比、金额、产能、税率）必须来自原文；推演性估算必须显式标注『分析师估算』。\n"
    "- 严禁编造『出口量缩减 X%』『产能波及 X 万吨』『成本上升 X%』等原文没有的量化结论。\n\n"
    "【智库产业基线定锚（CoT · 防止常识性时空错乱）】\n"
    "- 在撰写 impact_deduction 之前，你必须先在 industry_baseline_recall 中回答：\n"
    "  该国在该矿种上的现行政策现状是什么？核心产能格局如何？\n"
    "- 这是强制性的自我检查，防止犯低级常识错误。例如：\n"
    "  ❌ 你不能说『印尼镍矿出口量缩减 X%』——因为印尼自 2020 年起已全面禁止未加工镍矿出口\n"
    "  ❌ 你不能说『智利铜矿国有化冲击冶炼产能』——因为智利从未国有化铜矿\n"
    "  ✅ 正确做法：先声明基线，再基于基线性推演方向（如：审批收紧→国内矿权人供给受限→内贸矿溢价）\n\n"
    "【事实-分析分层（对标 Rhodium 每行标注 Source / 情报界 the source says vs we assess）】\n"
    "- 事实层（substantive_provisions + factual_basis）：只写原文有的，逐字或近逐字。factual_basis 必须摘录原文关键语句供回溯。\n"
    "- 基线层（industry_baseline_recall）：基于你的行业知识，声明该国该矿种的现行产业政策与产能现状。这是 CoT 自我检查，防止时空错乱。\n"
    "- 分析层（impact_deduction）：以事实层+基线层为基础，推演必须用『分析师预期/可能/或/预计』等措辞，与事实层物理分开，且不得引入新的数字。\n\n"
    "【节点定向推演格式（impact_deduction）】\n"
    "- 必须按 🔻上游采矿端 → 🔺中游冶炼端 → 🔻下游终端 逐节点分析，每个节点只描述方向（收紧/溢价/转移/挤压）。\n"
    "- 严禁编造具体百分比。若缺乏信息支撑，写『该节点缺乏信息，无法推演』。\n\n"
    "【置信度自评（analytic_confidence · 决定是否推送）】\n"
    "- High：原文明确披露量化条款、生效机制、具体约束对象，可直接据原文推演。\n"
    "- Medium：政策方向明确但量化细节待补，推演有部分原文支撑。\n"
    "- Low：仅基于行业背景推演，或原文信息稀薄（只有标题+一句话摘要/仅为程序性说明）。选 Low 时必须在 impact_deduction 中明示『原文信息不足』。Low 默认不推送。\n\n"
    "【程序性说明识别（关键 — 防止把办事流程误判为重磅政策）】\n"
    "- 若原文是部门常规程序说明、工作通讯、流程指引（如许可证申请流程、RKAB 提交说明、办事指南），且无实质法规约束力、无生效日、无量化指标：\n"
    "  → current_stage 必须填 Procedural_Statement（不可填 Fully_Effective）\n"
    "  → effective_date 必须留空\n"
    "  → supply_chain_impact_level 不得评为 High_Disruption\n"
    "  → 标题应体现『发布xx说明/指引/流程』而非『收紧/管控/冲击』\n\n"
    "【重点监控的政策类型（识别但不夸大）】\n"
    "1. 传统贸易壁垒：出口禁令/限制、关税调整、配额、外资股权限制、税率矿权变动\n"
    "2. 资源主权措施：国有化、征收、国内供应义务(DSI)/统购统销、国家强制采购、战略储备\n"
    "3. 供应链治理：供应链尽责法/尽职调查、ESG合规强制令、强迫劳动预防法案\n"
    "4. 绿色转型：碳边境调节机制(CBAM)、绿色补贴/IRA、关键矿产战略、产业补贴\n"
    "5. 价格干预：价格管制、暴利税、补贴取消、大宗商品平准基金\n\n"
    "【标题原则：准确 > 冲击力】\n"
    "- 标题概括核心动作即可，禁止使用『重磅/颠覆/史无前例/全面收紧/铁腕/雷霆』等渲染词。\n"
    "- 不得在标题中编造原文未出现的数字。\n\n"
    "【输出纪律】宁可漏判一条边缘政策，不可错推一条编造的『重磅预警』。信息不足时，如实标注，让下游人工复核。"
)

# 向后兼容别名（extract_macro_policy 内部已切到 V40）
_SYSTEM_PROMPT_V31 = _SYSTEM_PROMPT_V40


def extract_macro_policy(raw_text, schema_dict):
    """调用 DeepSeek 进行高管看板级宏观研判（v4.0: 事实优先 + 事实-分析分层 + 置信度）"""
    client = _get_deepseek_client()
    system_prompt = (
        f"{_SYSTEM_PROMPT_V40}\n\n"
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
#  v4.0: 双闸门推送机制 + 法规编号白名单 + 数字捏造净化
#  目标：事实准确优先，宁可漏推不可错推
# =============================================================================

# 推送白名单：只有官方一手源才有资格触发钉钉推送
PUSH_REQUIRED_SOURCE_TYPES = {
    "Official_Gazette",
    "Ministry_Website",
    "Customs_Announcement",
}

# 法规编号正则白名单：即使 LLM 标的 source_type 是 Tier1_Commodity_Media，
# 若原文文本本身含有法规编号（说明确实引用了一手法规），也视为可推送。
# 覆盖印尼（Permen ESDM / Keputusan Menteri / UU）、中国（国务院令/商务部公告/发改委令）、
# 欧美（Regulation No / Decree No / Act）等。
REGULATION_PATTERNS = [
    r"Permen\s*ESDM",
    r"Peraturan\s*Menteri",
    r"Keputusan\s*Menteri",
    r"UU\s*(No|Nomor|\.)",
    r"Peraturan\s*Pemerintah",
    r"国务院\s*令",
    r"商务部\s*公告",
    r"发改委\s*令",
    r"海关总署\s*公告",
    r"Regulation\s*\(EU\)\s*No",
    r"Regulation\s*No\.?\s*\d",
    r"Decree\s*No\.?\s*\d",
    r"Executive\s*Order\s*No",
    r"Act\s*of\s*\d{4}",
    r"Public\s*Law\s*No",
]

# 数字提取正则：百分比、金额、产能、税率
# 用于在原文中建立"已知数字集合"，LLM 输出的数字若不在集合内 → 视为捏造
_NUMBER_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|亿|万|美元|USD|RMB|人民币|吨|千吨|百万吨|Mt|kt|吨/年)",
    flags=re.IGNORECASE,
)


def _has_regulation_reference(text):
    """检查文本中是否出现法规编号（用于推送闸门 2 的兜底判定）"""
    if not text:
        return False
    for pattern in REGULATION_PATTERNS:
        if re.search(pattern, text):
            return True
    return False


def _should_push(data, fetched_text="", source_depth="shallow"):
    """
    【v4.0 双闸门 · 四重拦截】判定一条情报是否够格推送钉钉。
    返回 (should_push: bool, reason: str)

    闸门 1：烈度 ∈ {High_Disruption, Moderate_Adjustment}
    闸门 2：来源类型官方一手 OR 文本含法规编号
    闸门 3：非程序性说明（Procedural_Statement 不推）
    闸门 4：置信度非 Low（信息不足时宁可漏推）

    本案 ESDM 程序说明：Procedural_Statement + 非法规编号 + Low → 三重拦截。
    """
    si = data.get("strategic_implications", {}) or {}
    pd = data.get("policy_dynamics", {}) or {}
    md = data.get("metadata", {}) or {}

    impact = si.get("supply_chain_impact_level", "")
    source_type = md.get("policy_source_type", "")
    stage = pd.get("current_stage", "")
    confidence = si.get("analytic_confidence", "Low")

    # 闸门 1：烈度
    if impact not in ("High_Disruption", "Moderate_Adjustment"):
        return False, f"烈度 {impact} 未达推送阈值"

    # 闸门 2：来源类型官方一手 OR 文本含法规编号
    is_official_source = source_type in PUSH_REQUIRED_SOURCE_TYPES
    has_regulation = _has_regulation_reference(fetched_text) or _has_regulation_reference(
        pd.get("substantive_provisions", "") + pd.get("factual_basis", "")
    )
    if not is_official_source and not has_regulation:
        return False, f"非官方一手源（source_type={source_type}，无法规编号）"

    # 闸门 3：程序性说明降级（不推）
    if stage == "Procedural_Statement":
        return False, "程序性说明，仅入库待复核"

    # 闸门 4：低置信度不推（信息不足时宁可漏推）
    if confidence == "Low":
        return False, f"置信度 Low（信息源深度={source_depth}），仅入库待复核"

    # 闸门 5（未核实阶段也不推）
    if stage == "Unverified":
        return False, "阶段未核实，仅入库待复核"

    return True, "通过双闸门"


def _sanitize_fabricated_numbers(data, fetched_text):
    """
    【v4.0 数字净化 · 兜底防线】在 LLM 返回后、入库前调用。
    扫描 substantive_provisions / impact_deduction 中的数字，若原文未出现 → 标记 ⚠️(待核)。

    返回净化后的 data（原地修改 + 返回引用）。同时记录日志便于复盘。
    """
    if not fetched_text:
        return data

    # 原文中实际出现的数字集合
    source_numbers = set(m.group(0).strip() for m in _NUMBER_PATTERN.finditer(fetched_text))

    def _scrub(text):
        if not text:
            return text, 0
        flagged = 0

        def _check(m):
            nonlocal flagged
            token = m.group(0).strip()
            if token in source_numbers:
                return m.group(0)
            # 原文没有这个数字 → 标记
            flagged += 1
            return f"⚠️{m.group(0)}(待核)"

        cleaned = _NUMBER_PATTERN.sub(_check, text)
        return cleaned, flagged

    pd = data.get("policy_dynamics", {}) or {}
    si = data.get("strategic_implications", {}) or {}

    provisions_flagged = 0
    deduction_flagged = 0

    if pd.get("substantive_provisions"):
        cleaned, provisions_flagged = _scrub(pd["substantive_provisions"])
        if provisions_flagged:
            pd["substantive_provisions"] = cleaned

    if si.get("impact_deduction"):
        cleaned, deduction_flagged = _scrub(si["impact_deduction"])
        if deduction_flagged:
            si["impact_deduction"] = cleaned

    total = provisions_flagged + deduction_flagged
    if total > 0:
        print(
            f"🛡️ [数字净化] 检出 {total} 处疑似捏造数字 "
            f"(条款 {provisions_flagged} / 研判 {deduction_flagged})，已标记 ⚠️待核。"
        )
        # 在研判末尾追加溯源警示
        warning = "\n\n⚠️【系统警示】以上含(待核)标记的数字未在原文中出现，系分析师估算或模型生成，请人工核实。"
        si["impact_deduction"] = (si.get("impact_deduction") or "") + warning
        data["_numbers_flagged"] = total

    return data


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
        "DeepSeek 结构化分析": {"rich_text": [{"text": {"content": si["impact_deduction"][:4000]}}]},
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

    # v4.0: 增量更新新字段
    if pd.get("factual_basis"):
        properties["事实依据"] = {"rich_text": [{"text": {"content": str(pd["factual_basis"])[:2000]}}]}
    if si.get("analytic_confidence"):
        properties["置信度"] = {"select": {"name": si["analytic_confidence"]}}
    # v4.1: 增量更新产业基线
    if si.get("industry_baseline_recall"):
        properties["产业基线"] = {"rich_text": [{"text": {"content": str(si["industry_baseline_recall"])[:4000]}}]}

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
    "Procedural_Statement": "程序性说明",
    "Unverified": "待核实",
    "Historical_Noise": "历史旧规复述",
}

# ---- 推送显示：置信度 → 中文 ----（v4.0）
_CONFIDENCE_ZH_MAP = {
    "High": "高",
    "Medium": "中",
    "Low": "低",
}

# ---- 推送显示：来源类型 → 中文 ----（v4.0）
_SOURCE_TYPE_ZH_MAP = {
    "Official_Gazette": "官方公报",
    "Ministry_Website": "部委官网",
    "Customs_Announcement": "海关公告",
    "Tier1_Commodity_Media": "一级商品媒体",
}

# ---- 标题增强：矿种英文到中文映射 ----
_MINERAL_ZH_MAP = {
    "Lithium": "锂", "Cobalt": "钴", "Nickel": "镍",
    "Copper": "铜", "Rare Earths": "稀土", "Graphite": "石墨",
    "Others": "其他",
}


# ---- 推送显示：国家代码 → 国旗 + 中文名 ----
_COUNTRY_DISPLAY = {
    "CN": "🇨🇳 中国", "ID": "🇮🇩 印尼", "EU": "🇪🇺 欧盟",
    "CD": "🇨🇩 刚果(金)", "CL": "🇨🇱 智利", "AU": "🇦🇺 澳大利亚",
    "US": "🇺🇸 美国", "JP": "🇯🇵 日本", "KR": "🇰🇷 韩国",
    "GLOBAL": "🌐 全球",
}

# ---- 推送显示：冲击烈度 → 中文 ----
_IMPACT_ZH_MAP = {
    "High_Disruption": "重大冲击",
    "Moderate_Adjustment": "中度调整",
    "Low_Monitoring": "低度监测",
}

# ---- 推送显示：政策维度 → 中文 ----
_DIMENSION_ZH_MAP = {
    "Trade_Restriction": "贸易限制",
    "Resource_Sovereignty": "资源主权",
    "Supply_Chain_Governance": "供应链治理",
    "ESG_Compliance": "ESG合规",
    "Industrial_Policy": "产业政策",
    "Green_Transition": "绿色转型",
}


# ---- 推送格式化辅助函数（避免 send_dingtalk_alert / send_dingtalk_digest 重复代码）----

def _fmt_country(code):
    return _COUNTRY_DISPLAY.get(code, code)

def _fmt_impact(level):
    return _IMPACT_ZH_MAP.get(level, level)

def _fmt_stage(stage):
    return _STAGE_ZH_MAP.get(stage, stage)

def _fmt_minerals(types):
    if not types:
        return "—"
    return "、".join(_MINERAL_ZH_MAP.get(m, m) for m in types)

def _fmt_dimension(dim):
    return _DIMENSION_ZH_MAP.get(dim, dim)


def _enhance_policy_title(policy_name_zh, country, current_stage, mineral_types=None, issuing_authority=""):
    """
    【v3.3 情报级标题增强 — Action-Oriented Intelligence Title】
    匹配强结构化公式：[{国家码}] {颁布主体}{动作+影响}：{法案核心} ({矿种})（{阶段}）
    - 若标题缺 [国家码] 前缀，自动注入
    - v3.5: 若标题缺颁布主体（[国家码] 后紧接动词），自动注入 issuing_authority
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
        has_country_tag = True

    # v3.5: 检测并注入颁布主体 —— [国家码] 之后若紧接动词（推动/加征/通过/禁止…），说明缺主体
    if issuing_authority and has_country_tag:
        m = re.match(r'^(\[[A-Z]{2}|EU|GLOBAL\]\s*)([\u4e00-\u9fa5])', enhanced)
        if m:
            after_tag = m.group(2)
            _ACTION_VERBS = "推动加征通过禁止发布出台征收实施签署批准收紧限制要求强制启动宣布计划拟议"
            if after_tag in _ACTION_VERBS:
                enhanced = f"{m.group(1)}{issuing_authority}{enhanced[m.end():]}"

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
        md.get("issuing_authority", ""),
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
        "DeepSeek 结构化分析": {"rich_text": [{"text": {"content": si["impact_deduction"][:4000]}}]},
    }

    # v3.1: 政策维度标签
    if pd.get("policy_dimension"):
        properties["政策维度"] = {"select": {"name": pd["policy_dimension"]}}

    # v4.0: 事实依据（factual_basis）—— 写入 Notion 新列
    factual_basis = pd.get("factual_basis", "")
    if factual_basis:
        properties["事实依据"] = {"rich_text": [{"text": {"content": str(factual_basis)[:2000]}}]}

    # v4.0: 置信度（analytic_confidence）—— 写入 Notion 新列
    confidence = si.get("analytic_confidence", "Low")
    if confidence:
        properties["置信度"] = {"select": {"name": confidence}}

    # v4.1: 产业基线（industry_baseline_recall）—— 写入 Notion 新列
    baseline = si.get("industry_baseline_recall", "")
    if baseline:
        properties["产业基线"] = {"rich_text": [{"text": {"content": str(baseline)[:4000]}}]}

    # v3.5: 颁布机构（需 Notion 数据库先手动添加"颁布机构"列，否则设为 false 跳过）
    if os.environ.get("NOTION_HAS_AUTHORITY_FIELD", "").lower() == "true":
        issuing_authority = md.get("issuing_authority", "")
        if issuing_authority:
            properties["颁布机构"] = {"rich_text": [{"text": {"content": issuing_authority}}]}

    # v3.5: 时效性熔断标签 —— 若被标记为旧闻回顾，标题前缀加 ⏳，冲击烈度覆写为 Low_Monitoring
    if data.get("_stale_flag"):
        properties["政策名称"] = {"title": [{"text": {"content": f"⏳ {enhanced_title}"}}]}
        properties["冲击烈度"] = {"select": {"name": "Low_Monitoring"}}

    # v4.0: 待核标记 —— 低置信度/程序性说明/数字净化检出时，加 📝 待核前缀提醒人工复核
    if data.get("_review_flag") and not data.get("_stale_flag"):
        properties["政策名称"] = {"title": [{"text": {"content": f"📝 {enhanced_title}"}}]}
        print("📝 [待核] 标题已加 📝 前缀，入库后需人工复核。")

    # 动态注入生效日期（拦截空值防 Notion 报错）
    effective_date = (pd.get("effective_date") or "").strip()
    if effective_date and effective_date.lower() != "null":
        properties["生效日期"] = {"date": {"start": effective_date}}

    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
        # v4.2: 智库级页面 body 排版（基线+推演写入页面正文）
        "children": [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "🏛️ 客观事实（What Happened）"}}]}
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": str(pd["substantive_provisions"])[:2000]}}]}
            },
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "⚓ 产业基线对照（Status Quo）"}}]}
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": si.get("industry_baseline_recall", "暂无基线信息")[:2000]}}]}
            },
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "🔮 节点级战略推演（Directional Impact）"}}]}
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": si.get("impact_deduction", "")[:4000]}}]}
            },
        ],
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


def _fmt_provisions(text):
    """将 AI 返回的条款长文本拆解为要点列表，精简信息密度"""
    if not text:
        return "> （暂无条款细节）"

    # 尝试按中文分号拆解（AI 最常见输出格式：「1. xxx；2. xxx；3. xxx」）
    parts = [p.strip() for p in text.split("；") if p.strip()]
    if len(parts) <= 1:
        # 尝试按英文分号或换行拆解
        parts = [p.strip() for p in text.replace(";", "\n").split("\n") if p.strip()]

    if len(parts) <= 1:
        # 无法拆解，保持原文缩进引用
        return f"> {text}"

    import re
    bullets = []
    for p in parts:
        # 移除前导编号如 "1." "1、" "(1)" "①"
        cleaned = re.sub(r'^[\s]*[\(\（]?\d+[\.\、\-\)\）\)]\s*', '', p).strip()
        if cleaned:
            bullets.append(f"- {cleaned}")
    return "\n".join(bullets)


def send_dingtalk_alert(data, source_url):
    """【高能时效触达】通过钉钉 Webhook 发送高管宏观视野告警"""
    webhook_url = os.environ.get("DINGTALK_WEBHOOK")
    if not webhook_url or webhook_url == "disabled":
        print("ℹ️ 暂未配置钉钉 Webhook，跳过告警触达阶段。")
        return

    # 加签模式：若配置了 DINGTALK_SECRET，则计算签名并拼接到 URL
    dingtalk_secret = os.environ.get("DINGTALK_SECRET")
    if dingtalk_secret:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{dingtalk_secret}"
        hmac_code = hmac.new(
            dingtalk_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        webhook_url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"

    impact = data["strategic_implications"].get("supply_chain_impact_level", "")
    alert_required = data["notion_integration"].get("dingtalk_alert_required", False)

    # 🛑 硬锁 1：物理静默 — Low_Monitoring 永不推送（不管 LLM 怎么填 dingtalk_alert_required）
    if impact == "Low_Monitoring":
        print(f"🤫 [静默入库] 政策真实但烈度为 Low_Monitoring，仅入库 Notion 不推钉钉。")
        return

    # 🛑 硬锁 2：LLM 判定门控
    if not alert_required:
        print("ℹ️ LLM 判定本条政策未达到告警阈值，防打扰过滤。")
        return

    headers = {"Content-Type": "application/json"}

    pd = data['policy_dynamics']
    si = data['strategic_implications']
    md = data['metadata']

    # v3.1: 在告警中展示政策维度
    dimension_label = pd.get('policy_dimension', '')
    dimension_line = f"\n\n**🏷️ 政策维度**：`{_fmt_dimension(dimension_label)}`" if dimension_label else ""

    # 冲击烈度 emoji 映射（替代不生效的 <font> 标签）
    impact_emoji = {
        "High_Disruption": "🔴🔴",
        "Moderate_Adjustment": "🟡",
        "Low_Monitoring": "🟢",
    }
    impact_level = si.get('supply_chain_impact_level', '')
    impact_badge = f"{impact_emoji.get(impact_level, '⚪')} **{_fmt_impact(impact_level)}**"

    # v4.0: 置信度展示
    confidence = si.get("analytic_confidence", "Low")
    confidence_emoji_map = {"High": "🟢", "Medium": "🟡", "Low": "🔴"}
    confidence_zh = _CONFIDENCE_ZH_MAP.get(confidence, confidence)

    # 条款拆解 → 要点列表（精简信息密度）
    provisions_raw = pd.get('substantive_provisions', '')
    provisions_bullets = _fmt_provisions(provisions_raw)

    # v4.0: 事实依据（factual_basis）—— 溯源锚点
    factual_basis = pd.get("factual_basis", "")
    factual_block = f"\n> 📎 原文依据：{factual_basis}" if factual_basis else ""

    # v4.1: 产业基线（industry_baseline_recall）—— 常识锚点
    baseline = si.get("industry_baseline_recall", "")
    # v4.2: 范式转移特殊渲染
    if data.get("_paradigm_shift"):
        baseline_block = (
            f"\n\n**⚓ 产业基线** ｜ ⚠️【历史基线已被打破】\n\n"
            f"> {baseline[:300]}\n"
            f"> 🚨 本次情报揭示了对旧基线的颠覆性变化，请优先参考上方分析层。"
        ) if baseline else ""
    else:
        baseline_block = (
            f"\n\n**⚓ 产业基线**（行业共识 · CoT 常识锚）\n\n"
            f"> {baseline[:300]}"
        ) if baseline else ""

    # v3.5: 颁布机构（双保险 —— 即使 LLM 标题里缺主语，卡片上也能看到）
    authority = md.get('issuing_authority', '')
    authority_line = f"\n   🏛️ 颁布机构：{authority}" if authority else ""

    # v4.0: 数字净化警示
    numbers_flagged = data.get("_numbers_flagged", 0)
    sanitize_warning = ""
    if numbers_flagged:
        sanitize_warning = f"\n⚠️ 系统已标记 {numbers_flagged} 处疑似捏造数字（见(待核)标记），请人工核实。"

    markdown_text = (
        f"### 📡 宏观政策雷达 · 政策预警\n\n"
        f"**📌 政策法案**：{pd.get('policy_name_zh') or '(未命名)'} ({pd.get('policy_name_original', '')}){authority_line}\n\n"
        f"**🌍 影响范围**：{_fmt_country(md['country'])} ｜ 矿种 {_fmt_minerals(md['mineral_types'])}"
        f"{dimension_line}\n\n"
        f"**⚖️ 法律阶段**：{_fmt_stage(pd['current_stage'])} ｜ 生效日 {pd.get('effective_date') or '未定'}\n\n"
        f"**🚨 冲击烈度**：{impact_badge} ｜ {confidence_emoji_map.get(confidence, '⚪')} 置信度：{confidence_zh}\n\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"#### 📜 事实层（原文可核）\n\n"
        f"{provisions_bullets}{factual_block}{baseline_block}\n\n"
        f"#### 🔮 分析层 ｜ 置信度：{confidence_zh}\n\n"
        f"> {si.get('impact_deduction', '')}{sanitize_warning}\n\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"🔗 [查看原文]({source_url})\n\n"
        f"📋 本条目已同步存入 Notion。📜 事实层=原文可核 · ⚓ 基线=行业共识 · 🔮 分析层=分析师推演。"
    )

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"📡 宏观政策雷达: {_fmt_country(md['country'])} 重磅预警",
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


def send_dingtalk_digest(policies):
    """
    【v3.5 汇总简报】将本轮所有通过门控的政策合并为单条钉钉摘要卡片。
    按冲击烈度排序（High_Disruption 优先），区块间用分隔线隔开。
    单条超过 4500 字时分片推送，每片标注 (第 x/N 片)。
    """
    webhook_url = os.environ.get("DINGTALK_WEBHOOK")
    if not webhook_url or webhook_url == "disabled":
        print("ℹ️ 暂未配置钉钉 Webhook，跳过告警触达阶段。")
        return

    # 加签模式
    dingtalk_secret = os.environ.get("DINGTALK_SECRET")
    if dingtalk_secret:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{dingtalk_secret}"
        hmac_code = hmac.new(
            dingtalk_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        webhook_url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"

    # 按冲击烈度排序：High_Disruption 在前
    impact_order = {"High_Disruption": 0, "Moderate_Adjustment": 1, "Low_Monitoring": 2}
    policies_sorted = sorted(
        policies,
        key=lambda p: impact_order.get(
            p[0].get("strategic_implications", {}).get("supply_chain_impact_level", ""), 2
        )
    )

    n = len(policies_sorted)
    impact_emoji = {
        "High_Disruption": "🔴🔴",
        "Moderate_Adjustment": "🟡",
        "Low_Monitoring": "🟢",
    }

    # 构建每条政策的 markdown 区块（v4.0 三层结构：事实层 / 分析层 / 溯源）
    blocks = []
    for i, (data, source_url) in enumerate(policies_sorted, 1):
        pd_data = data.get("policy_dynamics", {})
        si_data = data.get("strategic_implications", {})
        md_data = data.get("metadata", {})

        dimension_label = pd_data.get("policy_dimension", "")
        dimension_line = f" ｜ 🏷️ {_fmt_dimension(dimension_label)}" if dimension_label else ""

        impact_level = si_data.get("supply_chain_impact_level", "")
        impact_badge = f"{impact_emoji.get(impact_level, '⚪')} **{_fmt_impact(impact_level)}**"

        # v4.0: 置信度 + 来源类型展示
        confidence = si_data.get("analytic_confidence", "Low")
        confidence_emoji = {"High": "🟢", "Medium": "🟡", "Low": "🔴"}.get(confidence, "⚪")
        confidence_zh = _CONFIDENCE_ZH_MAP.get(confidence, confidence)
        source_type = md_data.get("policy_source_type", "")
        source_type_zh = _SOURCE_TYPE_ZH_MAP.get(source_type, source_type)
        confidence_line = f" ｜ {confidence_emoji} 置信度：{confidence_zh} ｜ 📰 {source_type_zh or '未分类'}"

        provisions_raw = pd_data.get("substantive_provisions", "")
        provisions_bullets = _fmt_provisions(provisions_raw)

        # v4.0: 事实依据（factual_basis）—— 溯源锚点
        factual_basis = pd_data.get("factual_basis", "")
        factual_block = f"\n   > 📎 原文依据：{factual_basis}" if factual_basis else ""

        # v4.1: 产业基线（industry_baseline_recall）—— 常识锚点
        baseline = si_data.get("industry_baseline_recall", "")
        # v4.2: 范式转移特殊渲染
        if data.get("_paradigm_shift"):
            baseline_block = (
                f"   \n"
                f"   **⚓ 产业基线 ｜ ⚠️【历史基线已被打破】**\n"
                f"   > {baseline[:300]}\n"
                f"   > 🚨 本次情报揭示了对旧基线的颠覆性变化，请优先参考上方分析层。\n"
            ) if baseline else ""
        else:
            baseline_block = (
                f"   \n"
                f"   **⚓ 产业基线（行业共识 · CoT 常识锚）**\n"
                f"   > {baseline[:300]}\n"
            ) if baseline else ""

        authority = md_data.get("issuing_authority", "")
        authority_line = f"\n   🏛️ 颁布机构：{authority}" if authority else ""

        # v4.0: 数字净化警示
        numbers_flagged = data.get("_numbers_flagged", 0)
        sanitize_warning = ""
        if numbers_flagged:
            sanitize_warning = f"\n   ⚠️ 系统已标记 {numbers_flagged} 处疑似捏造数字（见下文(待核)），请人工核实。"

        block = (
            f"#### 📌 #{i} {pd_data.get('policy_name_zh') or '(未命名)'}\n"
            f"   📝 {pd_data.get('policy_name_original', '')}{authority_line}\n"
            f"   🌍 {_fmt_country(md_data.get('country', '?'))} ｜ 矿种 {_fmt_minerals(md_data.get('mineral_types', []))}"
            f"{dimension_line}\n"
            f"   ⚖️ {_fmt_stage(pd_data.get('current_stage', '?'))} ｜ 生效 {pd_data.get('effective_date') or '未定'} ｜ {impact_badge}{confidence_line}\n"
            f"   \n"
            f"   **📜 事实层（原文可核）**\n"
            f"   {provisions_bullets}{factual_block}"
            f"{baseline_block}\n"
            f"   **🔮 分析层** ｜ 置信度：{confidence_zh}\n"
            f"   > {si_data.get('impact_deduction', '')[:400]}{sanitize_warning}\n"
            f"   [🔗 查看原文]({source_url})"
        )
        blocks.append(block)

    combined_body = "\n\n---\n\n".join(blocks)
    header = f"### 📡 宏观政策雷达 · 本期共 {n} 条政策预警（已通过事实-分析双闸门）"
    full_text = f"{header}\n\n{combined_body}\n\n━━━━━━━━━━━━━━\n📋 本报告已同步存入 Notion 情报资产库。📜 事实层=原文可核，🔮 分析层=分析师推演，请据此分层决策。"

    # 分片保护：钉钉 markdown 单条约 5000 字，保守按 4500 截断
    MAX_CHUNK = 4500
    headers_common = {"Content-Type": "application/json"}

    def post_markdown(text, chunk_idx, total_chunks):
        suffix = f"\n\n> 📄 第 {chunk_idx}/{total_chunks} 片" if total_chunks > 1 else ""
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📡 宏观政策雷达 · 第 {chunk_idx} 片",
                "text": text + suffix
            }
        }
        try:
            res = requests.post(webhook_url, headers=headers_common, json=payload, timeout=10)
            if res.status_code == 200:
                print(f"🔔 [钉钉] 摘要推送成功（第 {chunk_idx}/{total_chunks} 片）。")
            else:
                print(f"⚠️ [钉钉] 推送失败（第 {chunk_idx}/{total_chunks} 片），详情: {res.text[:200]}")
        except Exception as e:
            print(f"❌ [钉钉] 推送异常（第 {chunk_idx}/{total_chunks} 片）: {str(e)}")

    if len(full_text) <= MAX_CHUNK:
        post_markdown(full_text, 1, 1)
    else:
        total_chunks = (len(full_text) + MAX_CHUNK - 1) // MAX_CHUNK
        for ci in range(total_chunks):
            chunk = full_text[ci * MAX_CHUNK : (ci + 1) * MAX_CHUNK]
            post_markdown(chunk, ci + 1, total_chunks)


# =============================================================================
#  主流程
# =============================================================================

if __name__ == "__main__":
    PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
    schema = load_schema(os.path.join(PROJECT_DIR, "policy_schema.json"))
    all_active_sources = load_all_sources(os.path.join(PROJECT_DIR, "sources.yaml"))

    # v4.2: 加载活体基线知识库
    baselines, baseline_updated = load_knowledge_baselines(
        os.path.join(PROJECT_DIR, "knowledge_baselines.yaml")
    )

    print(f"📡 数字化情报网络就绪。当前天网总线共挂载 {len(all_active_sources)} 个探测节点。")
    print(f"🌐 NewsAPI: {', '.join(NEWSAPI_LANGUAGES)} | RSS 兜底: {', '.join(RSS_FALLBACK_LANGUAGES)}")
    print(f"🛡️ 熔断配置: 最大AI调用={MAX_AI_CALLS} | 连续空转上限={MAX_CONSECUTIVE_EMPTY} | 最小文本长度={MIN_TEXT_LENGTH}")
    print(f"📚 基线知识库已加载，覆盖 {len(baselines)} 个国家，最后更新: {baseline_updated}")

    # v4.3: 基线覆盖率交叉校验
    source_countries = {s["country"] for s in all_active_sources if s.get("country") != "GLOBAL"}
    baseline_countries = set(baselines.keys())
    missing_baseline = source_countries - baseline_countries
    unused_baseline = baseline_countries - source_countries
    if missing_baseline:
        print(f"⚠️ [基线缺口] {len(missing_baseline)} 国有情报源但无基线: {sorted(missing_baseline)}")
        print(f"   → 编辑 knowledge_baselines.yaml 补全，否则这些国家的研判将无常识锚。")
    if unused_baseline:
        print(f"ℹ️ [待接源] {len(unused_baseline)} 个基线国家暂无情报源: {sorted(unused_baseline)}")

    ai_call_count = 0
    consecutive_empty = 0
    last_empty_feed_type = None

    # v3.5: 收集本轮所有通过门控的政策，循环结束后合并为单条摘要推送
    pending_alerts = []

    for source in all_active_sources:
        source_notes = source.get("notes", "")
        notes_suffix = f" [📝 {source_notes}]" if source_notes else ""
        print(f"\n🚀 [正在扫描] 目标：{source['agency']} ({source['country']}){notes_suffix}...")

        # ---- v3.3 熔断：连续空转检测 ----
        if consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
            print(f"🛑 [熔断] 连续 {consecutive_empty} 个节点无有效政策，跳过后续 node。")
            break

        source_url = source.get("url", "")
        source_depth = "shallow"  # v4.0: 默认浅层，RSS/NewsAPI 二级抓取成功后升级

        if source.get("feed_type") == "newsapi":
            result = fetch_newsapi(source["query"], source.get("days_back", 7))
            if result:
                fetched_text = result["text"]
                source_url = result.get("source_url", source_url)
                source_depth = result.get("source_depth", "shallow")
            else:
                fetched_text = None
        elif source.get("feed_type") == "rss":
            rss_result = fetch_and_parse_rss(source["url"])
            if rss_result:
                fetched_text = rss_result["text"]
                # v3.4: 优先使用 RSS item 中提取的原文链接
                if rss_result.get("links"):
                    source_url = rss_result["links"][0]
                # v4.0: 透传二级抓取深度
                source_depth = rss_result.get("source_depth", "shallow")
            else:
                fetched_text = None
        else:
            fetched_text = fetch_and_clean_html(source["url"], source["dom_selector"])
            if fetched_text:
                source_depth = "full"  # HTML 靶向抓取本身是全文

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

        # v3.5: 前置年份预扫 —— 轻量正则提取文中所有四位年份，标记疑似旧文
        _years_in_text = [int(y) for y in re.findall(r'\b(19\d{2}|20[0-2]\d)\b', fetched_text)]
        _max_year = max(_years_in_text) if _years_in_text else None
        _current_year = datetime.now().year
        is_likely_old = (_max_year is not None and _max_year <= _current_year - 1)
        if is_likely_old:
            print(f"   ⚠️ [预扫] 文本中最高年份为 {_max_year} → 疑似历史回顾文章，后续将交叉校验。")

        # v4.2: 按 source country 注入基线知识到 prompt（让 LLM 融合 YAML 基线 + 自身训练数据）
        source_country = source.get("country", "GLOBAL")
        baseline_injection = _inject_baseline(source_country, baselines, baseline_updated)
        if baseline_injection:
            fetched_text = fetched_text + baseline_injection

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

            # ---- v3.5 噪音粉碎：最高优先级杀伤开关 ----
            # LLM 明确判定为无效输入（网页导航/无关新闻/纯内政），直接丢弃
            if analysis_result.get("is_valid_macro_policy") is False:
                print("🗑️ [噪音粉碎] 网页导航/无关新闻/纯内政措施，已直接丢弃。")
                consecutive_empty += 1
                continue

            # v3.1: 噪音过滤 — 跳过 DeepSeek 返回的"无政策"结果
            pd = analysis_result.get("policy_dynamics", {})
            name_zh = pd.get("policy_name_zh") or ""
            noise_patterns = [
                # 明确否定
                "无相关", "无关键矿产", "无宏观", "无相关政策", "无重大政策",
                "无实质性", "无有效政策", "无有效信息", "无效政策",
                # 变体否定（v3.3 扩充）
                "无涉矿", "无关政策", "无矿产", "无矿产政策",
                "非关键矿产", "非矿产", "不涉及关键矿产", "不涉及矿产",
                # 未发现/未检测到
                "未发现", "暂无", "未检测到", "未监测到", "暂无相关",
                "未能提取", "无法识别", "无新增政策",
                # 纯噪音
                "无明确", "无可提取", "不适用",
            ]
            if any(p in name_zh for p in noise_patterns):
                print(f"ℹ️ DeepSeek 判定为无宏观政策（{name_zh}），已过滤。")
                consecutive_empty += 1
                continue

            # ---- v4.2: Historical_Noise 拦截 —— 历史旧规复述直接丢弃 ----
            if pd.get("current_stage") == "Historical_Noise":
                print(f"🛡️ [防噪熔断] 识别到历史旧规复述（无增量情报），直接丢弃。")
                consecutive_empty += 1
                continue

            # 有效政策 → 重置空转计数
            consecutive_empty = 0
            last_empty_feed_type = None

            # ---- v3.5: 第二道防线 — LLM 时效性校验 + 年份交叉验证 ----
            nrv = analysis_result.get("news_recency_verification", {})
            is_recent = nrv.get("is_recent_policy_action", True)  # 缺字段保守放行
            declared_year = nrv.get("declared_publish_year")
            # 交叉校验：LLM 说 recent=true 但年份 ≤ 2024 → 矛盾
            year_conflict = (is_recent and isinstance(declared_year, int) and declared_year <= 2024)
            is_stale = (not is_recent) or year_conflict or is_likely_old

            if is_stale:
                analysis_result["strategic_implications"]["supply_chain_impact_level"] = "Low_Monitoring"
                analysis_result["notion_integration"]["dingtalk_alert_required"] = False
                analysis_result["_stale_flag"] = True
                reason = []
                if not is_recent: reason.append("LLM判定非近期政策")
                if year_conflict: reason.append(f"年份矛盾(声称={declared_year})")
                if is_likely_old: reason.append(f"预扫旧年份(max={_max_year})")
                print(f"🛡️ [时效性熔断] {', '.join(reason)} → 降为 Low_Monitoring，仅入库不推送。")

            # v3.4: 优先使用 AI 从原文中提取的政策全文链接，fallback 到源地址
            # v3.5: AI 吐出的链接若仍是 Google News 跳转链，也解码一遍
            ai_source_url = analysis_result.get("notion_integration", {}).get("source_article_url", "")
            if ai_source_url and ai_source_url.startswith("http"):
                source_url = resolve_google_news_url(ai_source_url)

            print(f"🎉 战略情报研判完成。")

            # ---- v4.0: 数字净化兜底（LLM 返回后、入库前） ----
            analysis_result = _sanitize_fabricated_numbers(analysis_result, fetched_text)

            # ---- v4.2: 范式转移检测（基线失效雷达） ----
            si_check = analysis_result.get("strategic_implications", {}) or {}
            if si_check.get("baseline_shift_detected") is True:
                print("🚨🚨🚨 [范式转移侦测] DeepSeek 研判最新情报已打破历史基线！")
                print("   请维护人员核实 knowledge_baselines.yaml 是否需要更新。")
                print("   若确认为范式转移，请编辑 YAML 并更新 last_updated 时间戳。")
                analysis_result["_paradigm_shift"] = True

            # ---- v4.0: 双闸门推送判定（替代原有简单烈度判断） ----
            should_push, push_reason = _should_push(
                analysis_result, fetched_text=fetched_text, source_depth=source_depth
            )

            # ---- v4.0: 待核标记（非推送但有信息不足的情况，入库时加前缀提醒人工复核）----
            pd_check = analysis_result.get("policy_dynamics", {}) or {}
            needs_review = (
                si_check.get("analytic_confidence") == "Low"
                or pd_check.get("current_stage") in ("Procedural_Statement", "Unverified")
                or analysis_result.get("_numbers_flagged", 0) > 0
            )
            if needs_review and not analysis_result.get("_stale_flag"):
                analysis_result["_review_flag"] = True

            insert_to_notion(analysis_result, source_url)

            # v4.0: 推送队列（双闸门替代简单烈度判断）
            if should_push:
                pending_alerts.append((analysis_result, source_url))
                print(f"🚨 [双闸门] 通过推送判定：{push_reason}")
            else:
                print(f"🤫 [静默入库] 未通过双闸门：{push_reason}。仅入库 Notion。")

    # ---- v3.5: 汇总推送 ----
    if pending_alerts:
        print(f"\n📬 本轮共 {len(pending_alerts)} 条政策通过研判，正在汇总为单条摘要推送...")
        send_dingtalk_digest(pending_alerts)
    else:
        print("\nℹ️ 本轮无政策达到钉钉推送阈值。")
