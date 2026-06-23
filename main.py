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
from llm_cache import get_cached_client

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
    加载基线知识库。兼容 v2.0 (rules 字符串列表) 和 v3.0 (documents 结构化列表)。
    返回 (dict{country_code: [rule_strings]}, last_updated: str)
    """
    if not os.path.isfile(yaml_path):
        print(f"ℹ️ 基线知识库文件不存在 ({yaml_path})，跳过注入。")
        return {}, "unknown"
    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        result = {}
        for code, entry in config.get("baselines", {}).items():
            if not entry:
                continue
            # v3.0: 从 documents 提取 baseline 文本
            if entry.get("documents"):
                result[code] = [d["baseline"] for d in entry["documents"] if d.get("baseline")]
            # v2.0 向后兼容
            elif entry.get("rules"):
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

# v5.0: 统一缓存客户端（替代原始 OpenAI Client）
# 内置进程内存去重 + 磁盘缓存，提升 DeepSeek API 缓存命中率
_llm_client = None

def _get_llm_client():
    """获取全局 CachedLLMClient 单例"""
    global _llm_client
    if _llm_client is None:
        _llm_client = get_cached_client()
    return _llm_client

def discover_hotspots(max_count=5):
    """方案 B：让 DeepSeek 推荐本周矿产政策热点查询（v5.0: 缓存命中优化）"""
    client = _get_llm_client()
    prompt = (
        "你是一个全球关键矿产政策追踪专家。请基于你对近期地缘政治和产业动态的了解，"
        f"推荐 {max_count} 条本周最值得关注的矿产政策搜索查询。"
        "其中英文4条（覆盖全球政策源）、中文3条（覆盖中国国务院/商务部/省级政府/新华社等）、"
        "西班牙语2条（覆盖智利/阿根廷/秘鲁/玻利维亚等拉美矿产政策）、"
        "法语1条（覆盖刚果(金)/几内亚/马达加斯加等非洲矿产政策）。"
        "每条查询应组合具体矿种、国家/地区和政策术语。"
        "西语查询示例：'litio nacionalización Chile 2026'、'cobre Perú exportación restricciones'。"
        "法语查询示例：'cobalt exportation quota RDC 2026'、'lithium mine Mali fiscalité'。"
        "请特别关注以下近期热点方向：\n"
        "- 国家统购统销/国内供应义务（Domestic Supply Obligation, DSI）\n"
        "- 供应链尽责立法/ESG合规（Supply Chain Due Diligence）\n"
        "- 碳边境调节机制（CBAM）\n"
        "- 关键矿产战略/绿色补贴（Critical Minerals Strategy, IRA）\n"
        "- 价格管制/战略储备（Price Control, Strategic Stockpile）\n"
        f'请以 JSON 格式返回，格式如下：{{"queries": ["query1", "query2", ...]}}'
    )
    try:
        response = client.chat_completion(
            task_type="hotspot_discovery",
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
    "【目标矿种范围（is_valid_macro_policy 杀伤开关）】\n"
    "本系统仅追踪以下 6 种关键矿产的政策动态：锂、钴、镍、铜、稀土、石墨。\n"
    "以下品类不属于目标范围，必须判定 is_valid_macro_policy=false：\n"
    "  - 石油/原油、天然气/LNG、煤炭、铁矿石、铝土矿、金、银、钻石\n"
    "  - 能源安全政策（如天然气保留计划、石油储备、煤炭出口禁令等）\n"
    "  - 农业大宗商品、粮食安全政策\n"
    "若原文仅涉及上述排除品类而未触及 6 种目标矿产，直接判无效。\n\n"
    "【v5.0 实体-事件分离 · 核心架构】\n"
    "- policy_entity：描述『政策文件本身』（稳定的、跨报道不变的标识信息）\n"
    "  · official_name：从原文提取最完整、最官方的法案名称原文（去重主键）。宁可保留原文长全称，不要自己缩写。\n"
    "  · chinese_translation：该名称的精准中文翻译\n"
    "  · current_stage：该法案当前的法律阶段\n"
    "  · document_type：该文件的法律层级\n"
    "- event_update：描述『本次报道带来的增量动态』（每次报道可以不同）\n"
    "  · event_summary：本次报道的核心事实（发生了什么新动态？）\n"
    "  · event_classification（决定推送的三分类）：\n"
    "    - New_Policy_Issuance：全新法案/政策/标准首次出台 → 必推钉钉\n"
    "    - Milestone_Amendment：既有文件发生阶段实质推进/重大细则变更/官方反转/全行业震荡 → 必推钉钉\n"
    "    - Routine_Commentary：既有文件的常规落地/企业合规/行业评论/重复报道 → 绝对静默仅入库\n"
    "  · event_impact_deduction：仅针对本次动态的供应链推演\n"
    "- 同一法案的不同新闻报道 → policy_entity 必须完全一致，event_update 可以不同\n"
    "- policy_dynamics 和 strategic_implications 字段仍需填写（向后兼容），内容从 policy_entity/event_update 自动派生\n\n"
    "【文件 vs 报道 · 核心区分（v4.5）】\n"
    "- 本系统以『政策文件』为知识单元，而非以『新闻报道』为单元。\n"
    "- 若原文是某一具体法律法规/政策文件/标准的初次颁布、修订文本或官方公告全文 → document_type 填对应类型，article_type 填 Official_Announcement\n"
    "- 若原文是对既有文件的新闻报道、分析评论、专家解读 → document_type 填 Other，article_type 填 News_Report/Analysis_Commentary/Expert_Opinion\n"
    "- 若原文报道了某个可识别的政策文件，references_existing_document=true，document_signature 填被引用文件的签名\n\n"
    "【重点监控的政策类型（识别但不夸大）】\n"
    "1. 传统贸易壁垒：出口禁令/限制、关税调整、配额、外资股权限制、税率矿权变动\n"
    "2. 资源主权措施：国有化、征收、国内供应义务(DSI)/统购统销、国家强制采购、战略储备\n"
    "3. 供应链治理：供应链尽责法/尽职调查、ESG合规强制令、强迫劳动预防法案\n"
    "4. 绿色转型：碳边境调节机制(CBAM)、绿色补贴/IRA、关键矿产战略、产业补贴\n"
    "5. 价格干预：价格管制、暴利税、补贴取消、大宗商品平准基金\n\n"
    "【标题原则：准确 > 冲击力】\n"
    "- 标题概括核心动作即可，禁止使用『重磅/颠覆/史无前例/全面收紧/铁腕/雷霆』等渲染词。\n"
    "- 不得在标题中编造原文未出现的数字。\n\n"
    "【事件去重签名（event_signature · 防止同事件多报道重复入库）】\n"
    "- 格式：{国家二字码}-{主矿种或'多矿种'}-{规范动作标签}-{年份季度}\n"
    "- 规范动作标签只能从以下选择：出口管制、出口禁令、关税调整、配额调整、外资限制、国有化、供应链尽责、ESG监管、产业补贴、税收调整、矿权许可、战略储备、价格干预\n"
    "- 关键约束：不同新闻源报道同一政策事件时，必须生成完全相同的 event_signature。\n"
    "  例：'中国暂停对镝/铽等中重稀土的出口许可证管理'和'商务部暂缓稀土出口限制'是同一事件 → 都输出 'CN-稀土-出口管制-2026Q2'\n"
    "- 矿种字段：若涉及矿种多于1个且无明确主次，填'多矿种'；否则填单一主要矿种。\n\n"
    "【输出纪律】宁可漏判一条边缘政策，不可错推一条编造的『重磅预警』。信息不足时，如实标注，让下游人工复核。"
)

# 向后兼容别名（extract_macro_policy 内部已切到 V40）
_SYSTEM_PROMPT_V31 = _SYSTEM_PROMPT_V40


def extract_macro_policy(raw_text, schema_dict, baseline_injection=""):
    """调用 DeepSeek 进行高管看板级宏观研判（v5.1: Context Cache 前缀复用 + 基线独立 system 消息）"""
    client = _get_llm_client()
    system_prompt = (
        f"{_SYSTEM_PROMPT_V40}\n\n"
        f"【⚠️ 核心硬约束：严格按以下 Schema 规范返回 JSON】\n"
        f"{json.dumps(schema_dict, ensure_ascii=False, indent=2)}"
    )
    # v5.1: 三段消息结构 → 最大化 DeepSeek Context Cache 前缀复用
    #   [0] system: 全局静态指令 (~2800 tokens) → 所有调用共享前缀
    #   [1] system: 按国基线注入 (~100 tokens) → 同国连续调用共享前缀
    #   [2] user:   动态情报文本 → 每次不同
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": baseline_injection},
        {"role": "user", "content": f"请对以下原生文本进行过滤与地缘战略推演：\n\n{raw_text}"},
    ]
    try:
        response = client.chat_completion(
            task_type="policy_extraction",
            model="deepseek-v4-pro",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.0,  # v5.0: 确定性输出 → 提升 DeepSeek 服务端 + 本地缓存命中率
            timeout=60,
        )
        content = response.choices[0].message.content
        if content is None:
            print("❌ DeepSeek 返回了空内容")
            return None
        result = json.loads(content)
        return _normalize_v50(result)
    except Exception as e:
        print(f"❌ DeepSeek 提炼异常: {str(e)}")
        return None


def _normalize_v50(data):
    """v5.0 兼容层：将 policy_entity/event_update 自动映射到 policy_dynamics/strategic_implications"""
    entity = data.get("policy_entity") or {}
    event = data.get("event_update") or {}
    pd = data.setdefault("policy_dynamics", {})
    si = data.setdefault("strategic_implications", {})

    # 从 entity 派生 policy_dynamics
    if entity.get("official_name") and not pd.get("policy_name_original"):
        pd["policy_name_original"] = entity["official_name"]
    if entity.get("chinese_translation") and not pd.get("policy_name_zh"):
        pd["policy_name_zh"] = entity["chinese_translation"]
    if entity.get("current_stage") and not pd.get("current_stage"):
        pd["current_stage"] = entity["current_stage"]
    if entity.get("document_type") and not pd.get("document_type"):
        pd["document_type"] = entity["document_type"]

    # v5.1: master_tag 从 policy_entity 映射到 notion_integration
    ni = data.setdefault("notion_integration", {})
    if entity.get("master_tag") and not ni.get("master_tag"):
        # 将 entity 分类映射为中文标签（兼容 Notion 现有 select 选项）
        tag_map = {
            "Resource_Nationalism": "资源民族主义",
            "Trade_Barrier": "贸易壁垒",
            "Compliance_Standard": "合规标准",
            "Supply_Chain_Subsidy": "产业补贴",
            "Others": "宏观地缘与产业政策",
        }
        ni["master_tag"] = tag_map.get(entity["master_tag"], "宏观地缘与产业政策")
    if not ni.get("master_tag"):
        ni["master_tag"] = "宏观地缘与产业政策"

    # 从 event_classification 派生 is_major_milestone（向后兼容）
    ec = event.get("event_classification", "")
    if ec and not event.get("is_major_milestone"):
        event["is_major_milestone"] = ec in ("New_Policy_Issuance", "Milestone_Amendment")

    # 从 event 派生 strategic_implications
    if event.get("event_impact_deduction") and not si.get("impact_deduction"):
        si["impact_deduction"] = event["event_impact_deduction"]

    return data


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


def _should_push(data, fetched_text="", source_depth="shallow", is_new_document=True):
    """
    【v4.5 文档生命周期驱动推送】以政策文件为推送单元，而非新闻报道。
    返回 (should_push: bool, reason: str)

    推送触发条件（满足任一即可）：
      - 新文件出台：article_type=Official_Announcement 且为新文件
      - 既有文件重大更新：confidence>=Medium 且 impact>=Moderate
      - 高冲击力分析：article_type=Analysis/Expert 且 confidence=High
    不再推送：
      - 已知文件的第 N 篇类似报道
      - 程序性说明 / 未核实 / 低置信度
    """
    si = data.get("strategic_implications", {}) or {}
    pd = data.get("policy_dynamics", {}) or {}
    nrv = data.get("news_recency_verification", {}) or {}

    article_type = nrv.get("article_type", "News_Report")
    impact = si.get("supply_chain_impact_level", "")
    stage = pd.get("current_stage", "")
    confidence = si.get("analytic_confidence", "Low")

    # 硬性不推（始终保留的底线）
    if stage == "Procedural_Statement":
        return False, "程序性说明，仅入库"
    if stage == "Unverified":
        return False, "阶段未核实，仅入库"
    if confidence == "Low":
        return False, f"置信度 Low，仅入库待复核"

    # 新文件出台 → 推送（最高优先级）
    if article_type == "Official_Announcement" and is_new_document:
        return True, f"新文件出台：{pd.get('policy_name_zh', '')[:40]}"

    # 既有文件，高冲击或高置信分析 → 推送
    if impact in ("High_Disruption", "Moderate_Adjustment"):
        return True, f"重大更新（烈度={impact}, 置信度={confidence}）"

    # v5.1 第一层：event_classification 三分类（零额外 API 调用）
    event = data.get("event_update", {}) or {}
    ec = event.get("event_classification", "")
    if ec in ("New_Policy_Issuance", "Milestone_Amendment"):
        label = "新政策出台" if ec == "New_Policy_Issuance" else "既有文件里程碑变更"
        return True, f"{label}：{event.get('event_summary', '')[:60]}"
    if ec == "Routine_Commentary":
        return False, f"常规动态（{event.get('event_summary', '')[:40]}），绝对静默"

    # 语义 Diff 检测到质变 → 推送（兜底纠错）
    material_change = data.get("_material_change", {}) or {}
    if material_change.get("has_material_change"):
        return True, f"语义Diff检测到质变：{material_change.get('change_summary', '')[:60]}"

    # 高置信度分析/专家意见 → 推送
    if article_type in ("Analysis_Commentary", "Expert_Opinion") and confidence == "High":
        return True, "高置信度分析/专家意见"

    return False, f"已知文件的常规报道（类型={article_type}, 烈度={impact}），仅入库"


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

def _notion_search_pages(official_name, fallback_name=""):
    """
    v5.0: official_name 精确匹配（Title 列）作为去重主键。
    若 official_name 为空则 fallback 到文档签名或中文名模糊匹配。
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

    payload = {"filter": {"or": []}, "page_size": 5}

    # v5.0: 主路径 — 官方原名精确匹配 Title
    if official_name:
        payload["filter"]["or"].append({
            "property": "政策名称",
            "title": {"equals": official_name}
        })
        # 兜底：原名太长可能被截断，加 contains
        if len(official_name) > 50:
            payload["filter"]["or"].append({
                "property": "政策名称",
                "title": {"contains": official_name[:100]}
            })

    # 兜底 — 中文名/文件签名模糊匹配
    if fallback_name:
        payload["filter"]["or"].append({
            "property": "政策名称",
            "title": {"contains": fallback_name[:80]}
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
    event_signature = pd.get("event_signature", "")

    country_code = md.get("country", "")
    mineral_types = md.get("mineral_types", [])
    core_categories = pd.get("core_category", [])
    impact_level = si.get("supply_chain_impact_level", "Low_Monitoring")

    # 仅更新可能变化的字段
    properties = {
        "当前阶段": {"select": {"name": pd["current_stage"]}},
        "冲击烈度": {"select": {"name": impact_level}},
        "核心条款摘要": {"rich_text": [{"text": {"content": str(pd["substantive_provisions"])[:2000]}}]},
        "DeepSeek 结构化分析": {"rich_text": [{"text": {"content": si["impact_deduction"][:4000]}}]},
    }
    # 事件签名（去重主键，首次入库可能为空，增量时补填）
    if event_signature:
        properties["事件签名"] = {"rich_text": [{"text": {"content": event_signature}}]}

    # v4.5: 增量更新补充字段（首次入库时可能为空，增量时补填）
    region_name = _COUNTRY_TO_REGION.get(country_code)
    if region_name:
        properties["覆盖地区"] = {"multi_select": [{"name": region_name}]}
    if mineral_types:
        primary = _MINERAL_TO_PRIMARY.get(mineral_types[0])
        if primary:
            properties["主矿种（可选）"] = {"select": {"name": primary}}
    status_name = _IMPACT_TO_STATUS.get(impact_level)
    if status_name:
        properties["处理状态"] = {"status": {"name": status_name}}
    for cat in core_categories:
        ptype = _CATEGORY_TO_POLICY_TYPE.get(cat)
        if ptype:
            properties["政策类型（可选）"] = {"select": {"name": ptype}}
            break
    provisions_text = str(pd.get("substantive_provisions", ""))
    if provisions_text:
        properties["要点摘要"] = {"rich_text": [{"text": {"content": provisions_text[:300]}}]}
    issuing_authority = md.get("issuing_authority", "")
    if issuing_authority:
        properties["发布机构"] = {"rich_text": [{"text": {"content": issuing_authority}}]}

    # v4.4: 生效日期 fallback
    effective_date = (pd.get("effective_date") or "").strip()
    if not effective_date or effective_date.lower() == "null":
        declared_year = data.get("news_recency_verification", {}).get("declared_publish_year")
        if declared_year and isinstance(declared_year, int) and 2020 <= declared_year <= 2030:
            effective_date = f"{declared_year}-06-01"
    if effective_date and effective_date.lower() != "null":
        properties["生效日期"] = {"date": {"start": effective_date}}
    # v4.5: 发布日期（同样逻辑）
    publish_date = (pd.get("effective_date") or "").strip()
    if not publish_date or publish_date.lower() == "null":
        declared_year = data.get("news_recency_verification", {}).get("declared_publish_year")
        if declared_year and isinstance(declared_year, int) and 2020 <= declared_year <= 2030:
            publish_date = f"{declared_year}-06-01"
    if publish_date and publish_date.lower() != "null":
        properties["发布日期"] = {"date": {"start": publish_date}}

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

        err_text = res.text[:500]
        # v4.3: 自动剔除 Notion 数据库不存在的属性后重试
        if res.status_code == 400 and "is not a property that exists" in err_text:
            import re
            missing_props = set(re.findall(r'([^\s."]+) is not a property that exists', err_text))
            if missing_props:
                stripped = {k: v for k, v in properties.items() if k not in missing_props}
                payload["properties"] = stripped
                res2 = requests.patch(url, headers=headers, json=payload, timeout=10)
                if res2.status_code == 200:
                    print("🔄 [Notion] 检测到重复政策，已执行增量更新（自动跳过数据库缺失字段）。")
                    return True
                print(f"   ⚠️ [Notion] 重试后仍失败，状态码: {res2.status_code}")
                return False

        print(f"   ⚠️ [Notion] 增量更新失败，状态码: {res.status_code}")
        return False
    except Exception as e:
        print(f"   ❌ [Notion] 更新连接异常: {str(e)}")
        return False


def _notion_append_news(page_id, data, source_url, article_type):
    """
    v4.5: 新闻报道/分析评论 → 追加到已有政策文件下，不创建新记录。
    更新：关联报道数 +1、最后报道日期、在页面 body 追加折叠引用块。
    """
    notion_token = os.environ.get("NOTION_TOKEN")
    if not notion_token or notion_token == "disabled":
        return False

    pd = data.get("policy_dynamics", {})
    si = data.get("strategic_implications", {})
    article_label = {
        "News_Report": "新闻报道", "Analysis_Commentary": "分析评论",
        "Expert_Opinion": "专家解读",
    }.get(article_type, "相关报道")

    policy_name = pd.get("policy_name_zh", "未命名")
    summary_text = str(pd.get("substantive_provisions", "") or si.get("impact_deduction", ""))[:300]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 更新属性：关联报道数递增、最后报道日期
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    # 先读取当前计数
    try:
        r = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=headers, timeout=10)
        current_count = 0
        if r.status_code == 200:
            num_prop = r.json().get("properties", {}).get("关联报道数", {})
            current_count = num_prop.get("number", 0) or 0
    except Exception:
        pass

    props_update = {
        "关联报道数": {"number": current_count + 1},
        "最后报道日期": {"date": {"start": today}},
    }

    # v5.1: 时间轴直排——追加到页面正文末尾
    impact_text = str(si.get("impact_deduction", ""))[:400]
    children = [
        {
            "object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"text": {"content": f"📅 {today} · {article_label}"}}]}
        },
        {
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": f"【事实摘要】{summary_text}"}}]}
        },
    ]
    if impact_text:
        children.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": f"【战略推演】{impact_text}"}}]}
        })
    if source_url:
        children.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": "🔗 溯源原文", "link": {"url": source_url}}}]}
        })
    children.append({"object": "block", "type": "divider", "divider": {}})

    payload = {
        "properties": props_update,
        "children": children,
    }

    try:
        url = f"https://api.notion.com/v1/pages/{page_id}"
        res = requests.patch(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            print(f"📎 [Notion] 已追加{article_label}到已有文件（关联报道数: {current_count + 1}）")
            return True
        else:
            print(f"   ⚠️ [Notion] 追加报道失败，状态码: {res.status_code}")
            return False
    except Exception as e:
        print(f"   ❌ [Notion] 追加报道连接异常: {str(e)}")
        return False


def _semantic_diff(page_id, new_data):
    """
    v4.5: 语义 Diff —— 比较已有文件摘要与新报道，判断是否有质变。
    读取 Notion 页面的 核心条款摘要 + DeepSeek 结构化分析，与最新文本比对，
    调用 LLM 判定：纯粹复述已知信息（MUTE）还是包含实质新进展（ALERT）。
    返回 {"has_material_change": bool, "change_summary": str}
    """
    notion_token = os.environ.get("NOTION_TOKEN")
    if not notion_token or notion_token == "disabled":
        return {"has_material_change": False, "change_summary": ""}

    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    # 读取已有页面的核心摘要
    old_summary = ""
    try:
        r = requests.get(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=headers, timeout=10
        )
        if r.status_code == 200:
            props = r.json().get("properties", {})
            provisions = props.get("核心条款摘要", {}).get("rich_text", [])
            analysis = props.get("DeepSeek 结构化分析", {}).get("rich_text", [])
            old_summary = (
                ''.join(t.get("plain_text", "") for t in provisions)
                + '\n'
                + ''.join(t.get("plain_text", "") for t in analysis)
            )[:3000]
    except Exception:
        pass

    if not old_summary.strip():
        return {"has_material_change": True, "change_summary": "无法读取已有摘要，保守推送"}

    # 构建 Diff prompt
    pd = new_data.get("policy_dynamics", {}) or {}
    si = new_data.get("strategic_implications", {}) or {}
    new_text = (
        f"标题：{pd.get('policy_name_zh', '')}\n"
        f"条款：{str(pd.get('substantive_provisions', ''))[:1500]}\n"
        f"分析：{str(si.get('impact_deduction', ''))[:1500]}"
    )

    client = _get_llm_client()
    messages = [
        {"role": "system", "content": (
            "你是政策情报分析师。你的任务是比较一条已有政策文件的「历史摘要」和「最新报道」，"
            "判断最新报道是否包含实质性的新信息。\n\n"
            "「实质性新信息」的定义（满足任一即可）：\n"
            "1. 政策状态变化（提案→通过→生效→暂停→修订→废止）\n"
            "2. 出现新的量化条款（原文已有的数字不算、新的数字算）\n"
            "3. 新的反对意见/国际反应/法律挑战/修订提案\n"
            "4. 新的实施细节或配套措施（原文未披露过）\n"
            "5. 影响范围扩大（新增受影响矿种、新国家反应等）\n\n"
            "不视为实质性新信息：\n"
            "- 同一事件的重复报道\n"
            "- 不同来源对同一政策的类似描述\n"
            "- 纯背景介绍/历史回顾\n\n"
            "请仅返回 JSON：{\"has_material_change\": true/false, \"change_summary\": \"一句话说明变更（若无变更填空）\"}"
        )},
        {"role": "user", "content": (
            f"【历史摘要】\n{old_summary}\n\n"
            f"【最新报道】\n{new_text}\n\n"
            "请判定最新报道是否包含实质性新信息。"
        )},
    ]

    try:
        response = client.chat_completion(
            task_type="semantic_diff",
            model="deepseek-v4-pro",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.0,
            timeout=30,
        )
        content = response.choices[0].message.content
        if content:
            result = json.loads(content)
            return {
                "has_material_change": result.get("has_material_change", False),
                "change_summary": result.get("change_summary", ""),
            }
    except Exception as e:
        print(f"   ⚠️ 语义 Diff 异常: {e}")

    return {"has_material_change": True, "change_summary": "Diff 失败，保守推送"}


def _sync_baselines_to_notion(yaml_path):
    """
    v5.0: 将 knowledge_baselines.yaml 中的政策文件实体种子同步到 Notion。
    已有的跳过，不存在的创建基础容器。
    返回创建的条目数。
    """
    notion_token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if not notion_token or not database_id or notion_token == "disabled":
        return 0

    if not os.path.isfile(yaml_path):
        return 0

    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
    except Exception:
        return 0

    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    doc_type_map = {
        "Law": "法律 Law", "Regulation": "法规 Regulation",
        "Policy": "政策 Policy", "Standard": "标准 Standard",
        "Administrative_Order": "行政令 Admin_Order",
    }

    created = 0
    baselines = config.get("baselines", {})
    for country_code, entry in baselines.items():
        if not entry or not entry.get("documents"):
            continue

        for doc in entry["documents"]:
            official_name = doc.get("official_name", "").strip()
            if not official_name:
                continue

            # 查重
            exists, _ = _notion_search_pages(official_name)
            if exists:
                continue

            # 构建 Notion 属性
            properties = {
                "政策名称": {"title": [{"text": {"content": official_name[:200]}}]},
                "原名及出处": {"rich_text": [{"text": {"content": f"中文：{doc.get('chinese_name', '')}\n简称：{doc.get('short_name', '')}"}}]},
                "颁布国家": {"select": {"name": country_code}},
                "文件类型": {"select": {"name": doc_type_map.get(doc.get("type", "Other"), "其他 Other")}},
                "处理状态": {"status": {"name": "监测中"}},
                "核心分类": {"select": {"name": "宏观地缘与产业政策"}},
                "关联报道数": {"number": 0},
                "已告警（钉钉）": {"checkbox": False},
                "核心条款摘要": {"rich_text": [{"text": {"content": f"【基线种子 · 待事件填充】\n{doc.get('baseline', '')}"[:2000]}}]},
            }

            if doc.get("effective_date"):
                properties["生效日期"] = {"date": {"start": doc["effective_date"]}}
                properties["发布日期"] = {"date": {"start": doc["effective_date"]}}
            if doc.get("chinese_name"):
                properties["要点摘要"] = {"rich_text": [{"text": {"content": f"{doc['chinese_name']} ({official_name[:50]})"}}]}

            payload = {
                "parent": {"database_id": database_id},
                "properties": properties,
            }

            try:
                res = requests.post(
                    "https://api.notion.com/v1/pages",
                    headers=headers, json=payload, timeout=10
                )
                if res.status_code == 200:
                    print(f"  🌱 [基线种子] {doc.get('short_name', official_name[:50])}")
                    created += 1
                else:
                    err = res.json().get("message", res.text)[:100]
                    print(f"  ⚠️ 种子失败 [{doc.get('short_name', '')}]: {err}")
            except Exception as e:
                print(f"  ⚠️ 种子异常 [{doc.get('short_name', '')}]: {e}")

    return created


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

# ---- v4.5: Notion 补充字段映射 ----
_COUNTRY_TO_REGION = {
    "CN": "中国 China", "ID": "印尼 Indonesia", "CD": "刚果（金）DRC",
    "AU": "澳大利亚 Australia", "EU": "欧盟 EU", "US": "美国 USA",
    "CL": "智利 Chile", "AR": "阿根廷 Argentina", "PH": "菲律宾 Philippines",
    "ZW": "津巴布韦 Zimbabwe", "GH": "加纳 Ghana",
    "GLOBAL": "全球 Global", "JP": "日本 Japan",
}

_MINERAL_TO_PRIMARY = {
    "Lithium": "锂 Li", "Cobalt": "钴 Co", "Nickel": "镍 Ni",
    "Copper": "铜 Cu", "Rare Earths": "稀土 REE", "Manganese": "锰 Mn",
    # 中文别名（兜底）
    "锂": "锂 Li", "钴": "钴 Co", "镍": "镍 Ni",
    "铜": "铜 Cu", "稀土": "稀土 REE", "锰": "锰 Mn",
}

_IMPACT_TO_STATUS = {
    "Low_Monitoring": "低 Low", "Moderate_Adjustment": "中 Medium",
    "Medium_Impact": "中 Medium", "High_Disruption": "高 High",
    "Critical_Disruption": "重大 Critical", "Critical": "重大 Critical",
}

_CATEGORY_TO_POLICY_TYPE = {
    "Export_Ban": "出口/进口", "Export_Restriction": "出口/进口",
    "Tariff": "税费/特许权", "Royalty": "税费/特许权", "Tax": "税费/特许权",
    "Quota": "出口/进口", "Price_Control": "税费/特许权",
    "Nationalization": "许可/矿权", "Foreign_Equity": "许可/矿权",
    "Resource_Nationalism": "许可/矿权", "Supply_Nationalization": "许可/矿权",
    "Beneficiation": "产业政策/补贴", "State_Procurement": "产业政策/补贴",
    "Strategic_Stockpile": "产业政策/补贴", "Critical_Minerals_Strategy": "产业政策/补贴",
    "Green_Subsidy": "产业政策/补贴", "IRA": "产业政策/补贴",
    "Domestic_Supply_Obligation": "贸易/制裁", "Mandatory_Offtake": "贸易/制裁",
    "Bulk_Purchasing": "贸易/制裁",
    "Supply_Chain_Due_Diligence": "环保/ESG监管", "ESG_Regulation": "环保/ESG监管",
    "Forced_Labor_Prevention": "环保/ESG监管", "Carbon_Border_Adjustment": "环保/ESG监管",
    "Cross_Border_Compliance": "环保/ESG监管",
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
        return {"is_new": False, "page_id": None}

    pd = data["policy_dynamics"]
    si = data["strategic_implications"]
    md = data["metadata"]
    entity = data.get("policy_entity", {})

    # v5.0: 去重主键 → official_name（绝对主键）
    official_name = entity.get("official_name") or pd.get("policy_name_original", "")
    chinese_name = entity.get("chinese_translation") or pd.get("policy_name_zh", "")
    event_signature = pd.get("event_signature", "")
    document_signature = pd.get("document_signature", "")
    article_type = data.get("news_recency_verification", {}).get("article_type", "News_Report")
    references_existing = data.get("news_recency_verification", {}).get("references_existing_document", False)
    document_type = entity.get("document_type") or pd.get("document_type", "Other")

    # ---- v5.0: 官方原名精确去重 ----
    exists, existing_page_id = _notion_search_pages(official_name, chinese_name)
    if exists and existing_page_id:
        if article_type in ("News_Report", "Analysis_Commentary", "Expert_Opinion"):
            # 新闻报道：追加到此文件记录下，不创建新记录
            _notion_append_news(existing_page_id, data, source_url, article_type)
        else:
            _notion_update_policy(existing_page_id, data, source_url)
        return {"is_new": False, "page_id": existing_page_id}  # 已有文件，供语义 Diff 使用

    # ---- v5.0: Notion 标题使用官方原名（wiki 风格），增强标题作为展示名存入原名及出处 ----
    wiki_title = official_name if official_name else (
        chinese_name if chinese_name else pd.get("policy_name_zh", "未命名")
    )
    display_title = _enhance_policy_title(
        chinese_name if chinese_name else wiki_title,
        md.get("country", ""),
        entity.get("current_stage") or pd.get("current_stage", ""),
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

    country_code = md.get("country", "")
    mineral_types = md.get("mineral_types", [])
    core_categories = pd.get("core_category", [])
    impact_level = si.get("supply_chain_impact_level", "Low_Monitoring")

    properties = {
        "政策名称":     {"title":       [{"text": {"content": wiki_title}}]},
        "原名及出处":   {"rich_text":   [{"text": {"content": display_title}}]},
        "核心分类":     {"select":      {"name": data["notion_integration"]["master_tag"]}},
        "颁布国家":     {"select":      {"name": country_code}},
        "当前阶段":     {"select":      {"name": pd["current_stage"]}},
        "冲击烈度":     {"select":      {"name": impact_level}},
        "涉及矿种":     {"multi_select":[{"name": m} for m in mineral_types]},
        "核心政策手段": {"multi_select":[{"name": c} for c in core_categories]},
        "核心条款摘要": {"rich_text":   [{"text": {"content": str(pd["substantive_provisions"])[:2000]}}]},
        "原文链接":     {"url":          source_url},
        "DeepSeek 结构化分析": {"rich_text": [{"text": {"content": si["impact_deduction"][:4000]}}]},
    }

    # v3.1: 政策维度标签
    if pd.get("policy_dimension"):
        properties["政策维度"] = {"select": {"name": pd["policy_dimension"]}}

    # ---- v4.5: 补充字段全面填充 ----

    # 覆盖地区（多选）：ISO 国家码 → Notion 选项名
    region_name = _COUNTRY_TO_REGION.get(country_code)
    if region_name:
        properties["覆盖地区"] = {"multi_select": [{"name": region_name}]}

    # 主矿种（单选）：第一个目标矿种
    if mineral_types:
        primary = _MINERAL_TO_PRIMARY.get(mineral_types[0])
        if primary:
            properties["主矿种（可选）"] = {"select": {"name": primary}}

    # 处理状态：冲击烈度 → Notion 状态
    status_name = _IMPACT_TO_STATUS.get(impact_level)
    if status_name:
        properties["处理状态"] = {"status": {"name": status_name}}

    # 政策类型：从核心政策手段的首个匹配项推导
    for cat in core_categories:
        ptype = _CATEGORY_TO_POLICY_TYPE.get(cat)
        if ptype:
            properties["政策类型（可选）"] = {"select": {"name": ptype}}
            break

    # 要点摘要：条款摘要缩至 300 字
    provisions_text = str(pd.get("substantive_provisions", ""))
    if provisions_text:
        properties["要点摘要"] = {"rich_text": [{"text": {"content": provisions_text[:300]}}]}

    # 发布机构（v4.5: 修复字段名 — DB 中是"发布机构"不是"颁布机构"）
    issuing_authority = md.get("issuing_authority", "")
    if issuing_authority:
        properties["发布机构"] = {"rich_text": [{"text": {"content": issuing_authority}}]}

    # 发布日期：同生效日期逻辑，但使用 declared_publish_year 作 fallback
    publish_date = (pd.get("effective_date") or "").strip()
    if not publish_date or publish_date.lower() == "null":
        declared_year = data.get("news_recency_verification", {}).get("declared_publish_year")
        if declared_year and isinstance(declared_year, int) and 2020 <= declared_year <= 2030:
            publish_date = f"{declared_year}-06-01"
    if publish_date and publish_date.lower() != "null":
        properties["发布日期"] = {"date": {"start": publish_date}}

    # v4.5: 文件类型 + 文件签名（去重主键）
    if document_type and document_type != "Other":
        doc_type_label = {
            "Law": "法律 Law", "Regulation": "法规 Regulation",
            "Policy": "政策 Policy", "Standard": "标准 Standard",
            "Administrative_Order": "行政令 Admin_Order",
        }.get(document_type, "其他 Other")
        properties["文件类型"] = {"select": {"name": doc_type_label}}
    if document_signature:
        properties["文件签名"] = {"rich_text": [{"text": {"content": document_signature}}]}
    if event_signature:
        properties["事件签名"] = {"rich_text": [{"text": {"content": event_signature}}]}

    # 关联报道数：新建文件默认为 1（首次发现），报道类递增
    properties["关联报道数"] = {"number": 1}
    properties["最后报道日期"] = {"date": {"start": datetime.now(timezone.utc).strftime("%Y-%m-%d")}}

    # 已告警（钉钉）：新建默认未告警
    properties["已告警（钉钉）"] = {"checkbox": False}

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

    # v4.5: 状态标签（替代 emoji 前缀）—— 通过「处理状态」字段表达，不污染标题
    if data.get("_stale_flag"):
        properties["冲击烈度"] = {"select": {"name": "Low_Monitoring"}}
        properties["处理状态"] = {"status": {"name": "低 Low"}}

    if data.get("_review_flag") and not data.get("_stale_flag"):
        properties["处理状态"] = {"status": {"name": "待评估"}}
        print("📝 [待核] 已标记为「待评估」状态，入库后需人工复核。")

    # v4.4: 生效日期
    effective_date = (pd.get("effective_date") or "").strip()
    if not effective_date or effective_date.lower() == "null":
        declared_year = data.get("news_recency_verification", {}).get("declared_publish_year")
        if declared_year and isinstance(declared_year, int) and 2020 <= declared_year <= 2030:
            effective_date = f"{declared_year}-06-01"
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
        res_page_id = _notion_api_post(url, headers, payload)
        if res_page_id:
            return {"is_new": True, "page_id": res_page_id}
        return {"is_new": False, "page_id": None}
    except Exception as e:
        print(f"❌ [Notion] 连接异常: {str(e)}")
        return {"is_new": False, "page_id": None}


def _notion_api_post(url, headers, payload, retry_stripping=True):
    """发送 Notion API 请求，自动处理字段缺失错误并重试"""
    import re
    res = requests.post(url, headers=headers, json=payload, timeout=10)
    if res.status_code == 200:
        print("🚀 [Notion] 成功打标并持久化沉淀至高管数据库看板。")
        return res.json().get("id", "")

    err_text = res.text[:500]
    # v4.3: 若 Notion 数据库缺少某些属性列，自动剔除后重试
    if retry_stripping and res.status_code == 400 and "is not a property that exists" in err_text:
        missing_props = set(re.findall(r'([^\s."]+) is not a property that exists', err_text))
        if missing_props:
            stripped = {}
            removed = []
            for k, v in payload.get("properties", {}).items():
                if k in missing_props:
                    removed.append(k)
                else:
                    stripped[k] = v
            payload["properties"] = stripped
            # Also strip from children blocks if baseline was removed
            if "产业基线" in missing_props:
                payload["children"] = [
                    b for b in payload.get("children", [])
                    if b.get("type") != "heading_2" or "产业基线" not in str(b.get("heading_2", {}).get("rich_text", [{}])[0].get("text", {}).get("content", ""))
                ]
            print(f"⚠️ [Notion] 数据库缺少字段: {', '.join(removed)}，已自动移除后重试。")
            return _notion_api_post(url, headers, payload, retry_stripping=False)

    print(f"⚠️ [Notion] 写入失败，状态码: {res.status_code}, 详情: {err_text}")
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

    # v5.1: 精简卡片——entity + event_summary + impact + source
    blocks = []
    for i, (data, source_url) in enumerate(policies_sorted, 1):
        entity = data.get("policy_entity", {}) or {}
        event = data.get("event_update", {}) or {}
        md_data = data.get("metadata", {}) or {}
        si_data = data.get("strategic_implications", {}) or {}

        # 分类标签
        ec = event.get("event_classification", "")
        alert_label = "🆕 新政策出台" if ec == "New_Policy_Issuance" else "🔄 法案重大推进"

        block = (
            f"#### {alert_label} #{i}：{entity.get('chinese_translation') or entity.get('official_name') or '(未命名)'}\n"
            f"> 🌍 {_fmt_country(md_data.get('country', '?'))} ｜ 矿种 {_fmt_minerals(md_data.get('mineral_types', []))}\n"
            f"> 📜 {entity.get('official_name', '')[:200]}\n"
            f"---\n"
            f"**🚨 本次动态 (What Happened)**\n"
            f"> {event.get('event_summary', '')[:300]}\n\n"
            f"**🔮 战略推演 (Directional Impact)**\n"
            f"> {event.get('event_impact_deduction', si_data.get('impact_deduction', ''))[:300]}\n\n"
            f"🔗 [溯源原文]({source_url})"
        )
        blocks.append(block)

    combined_body = "\n\n---\n\n".join(blocks)
    header = f"### 📡 宏观政策雷达 · 本期 {n} 条预警"
    full_text = f"{header}\n\n{combined_body}\n\n━━━\n📋 已同步存入 Notion 情报资产库。全文时间轴请查看对应文件页面。"

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

    # v5.0: 同步基线政策文件实体到 Notion（种子容器）
    synced = _sync_baselines_to_notion(os.path.join(PROJECT_DIR, "knowledge_baselines.yaml"))
    if synced:
        print(f"🌱 [基线同步] 本次新创建 {synced} 个政策文件容器。")

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

    # v5.1: 按国家分组排序 → 同国源连续处理 → DeepSeek Context Cache 前缀复用最大化
    all_active_sources.sort(key=lambda s: s.get("country", "GLOBAL"))
    print(f"🔀 [缓存优化] 情报源已按国家分组排序，最大化 Context Cache 前缀复用。")

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

        # v5.1: 基线注入作为独立 system 消息传入（不再拼入 fetched_text）
        # → DeepSeek Context Cache 可跨调用复用基线前缀
        source_country = source.get("country", "GLOBAL")
        baseline_injection = _inject_baseline(source_country, baselines, baseline_updated)

        analysis_result = extract_macro_policy(fetched_text, schema, baseline_injection)
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

            # ---- v4.0: 待核标记 ----
            pd_check = analysis_result.get("policy_dynamics", {}) or {}
            needs_review = (
                si_check.get("analytic_confidence") == "Low"
                or pd_check.get("current_stage") in ("Procedural_Statement", "Unverified")
                or analysis_result.get("_numbers_flagged", 0) > 0
            )
            if needs_review and not analysis_result.get("_stale_flag"):
                analysis_result["_review_flag"] = True

            # v4.5: 先入库 → 语义 Diff → 推送判定
            insert_result = insert_to_notion(analysis_result, source_url)
            is_new_doc = insert_result.get("is_new", False)
            page_id = insert_result.get("page_id")

            # 已有文件的报道 → 语义 Diff 判定是否有质变
            material_change = None
            if not is_new_doc and page_id:
                material_change = _semantic_diff(page_id, analysis_result)
                analysis_result["_material_change"] = material_change
                if material_change.get("has_material_change"):
                    print(f"🔍 [语义Diff] 检测到质变：{material_change.get('change_summary', '')[:80]}")
                else:
                    print(f"🔇 [语义Diff] 无质变，MUTE。")

            # ---- v4.5: 文档生命周期推送判定 ----
            should_push, push_reason = _should_push(
                analysis_result, fetched_text=fetched_text,
                source_depth=source_depth, is_new_document=is_new_doc
            )

            if should_push:
                pending_alerts.append((analysis_result, source_url))
                print(f"🚨 [推送] {push_reason}")
            else:
                print(f"🤫 [静默入库] {push_reason}")

    # ---- v3.5: 汇总推送 ----
    if pending_alerts:
        print(f"\n📬 本轮共 {len(pending_alerts)} 条政策通过研判，正在汇总为单条摘要推送...")
        send_dingtalk_digest(pending_alerts)
    else:
        print("\nℹ️ 本轮无政策达到钉钉推送阈值。")

    # v5.0: 输出缓存命中统计
    _get_llm_client().print_stats()
