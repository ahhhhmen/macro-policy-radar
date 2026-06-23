#!/usr/bin/env python3
"""
影子审计管线 (Baseline Auditor) —— v4.3
独立于生产管线，每季度自动检查 knowledge_baselines.yaml 的时效性。

职责：
  1. 读取基线 YAML
  2. 对每个国家，用 NewsAPI+RSS 采集近期新闻
  3. 调用 DeepSeek 比对：基线规则是否仍有效？
  4. NO_CHANGE → 更新 last_updated 时间戳
  5. CHANGE  → CI 告警 + 打印 Diff 报告

用法：
  python3 audit_baselines.py                     # 标准审计
  python3 audit_baselines.py --country ID         # 单国审计
  python3 audit_baselines.py --update-timestamp   # 仅更新时间戳
"""

import os
import sys
import json
import yaml
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from llm_cache import get_cached_client

load_dotenv()

# ---- 审计专用配置 ----
AUDIT_DAYS_BACK = 30           # 采集最近 N 天的新闻
AUDIT_MAX_ARTICLES = 3         # 每国最多采集新闻条数
AUDIT_MIN_TEXT_LENGTH = 200    # 新闻文本最低字符数

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BASELINE_PATH = os.path.join(PROJECT_DIR, "knowledge_baselines.yaml")


# =============================================================================
#  复用 main.py 的抓取基础设施（最小化导入，避免循环依赖）
# =============================================================================

def _get_llm_client():
    """获取全局 CachedLLMClient 单例（v5.0: 缓存命中优化）"""
    return get_cached_client()


def _resolve_google_news_url(google_url):
    """解码 Google News 跳转链（从 main.py 移植）"""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    if not google_url or "news.google.com" not in google_url:
        return google_url
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        res = requests.get(google_url, headers=headers, timeout=10, allow_redirects=True, verify=False)
        final_url = res.url
        if final_url and "news.google.com" not in final_url and final_url.startswith("http"):
            return final_url
        return google_url
    except Exception:
        return google_url


def _fetch_news_for_country(country_code, country_name, days_back=AUDIT_DAYS_BACK):
    """
    为指定国家采集近期政策新闻。
    返回 (合并文本, 原文链接列表) 或 (None, [])。
    """
    import requests
    import urllib.parse
    import urllib3
    from bs4 import BeautifulSoup
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    newsapi_key = os.environ.get("NEWSAPI_KEY")
    combined_parts = []
    source_urls = []

    # 国家名中英文查询词
    query_variants = [
        f'{country_name} critical minerals mining policy regulation',
        f'{country_code} mining policy export ban tariff',
    ]

    # 方式 1: NewsAPI (en)
    if newsapi_key and newsapi_key != "disabled":
        from_date = (datetime.now(timezone.utc) - __import__('datetime').timedelta(days=days_back)).strftime("%Y-%m-%d")
        url = "https://newsapi.org/v2/everything"
        for query in query_variants:
            params = {
                "q": query,
                "from": from_date,
                "sortBy": "publishedAt",
                "pageSize": 3,
                "language": "en",
                "apiKey": newsapi_key,
            }
            try:
                res = requests.get(url, params=params, timeout=15)
                res.raise_for_status()
                data = res.json()
                for art in data.get("articles", [])[:3]:
                    title = art.get("title", "")
                    desc = art.get("description", "") or ""
                    art_url = _resolve_google_news_url(art.get("url", ""))
                    combined_parts.append(f"[NewsAPI] {title}\n{desc}")
                    if art_url:
                        source_urls.append(art_url)
            except Exception as e:
                print(f"   ⚠️ NewsAPI 查询失败 [{query[:40]}...]: {str(e)[:80]}")

    # 方式 2: Google News RSS 兜底 (en + zh)
    for lang in ("en", "zh"):
        lang_map = {"en": "en-US", "zh": "zh-CN"}
        hl_gl = lang_map.get(lang, "en-US")
        hl, gl = hl_gl.split("-")
        for query in query_variants:
            encoded = urllib.parse.quote(f"{query} when:{days_back}d")
            rss_url = f"https://news.google.com/rss/search?q={encoded}&hl={hl}-{gl}&gl={gl}&ceid={gl}:{hl}"
            try:
                res = requests.get(rss_url, timeout=15)
                soup = BeautifulSoup(res.text, 'xml')
                for item in soup.find_all('item')[:AUDIT_MAX_ARTICLES]:
                    title = item.find('title').get_text(strip=True) if item.find('title') else "Untitled"
                    desc = item.find('description') or item.find('summary')
                    clean = desc.get_text(strip=True) if desc else ""
                    combined_parts.append(f"[RSS-{lang}] {title}\n{clean}")
                    link_node = item.find('link')
                    if link_node:
                        source_urls.append(_resolve_google_news_url(link_node.get_text(strip=True)))
            except Exception:
                pass

    if not combined_parts:
        return None, []
    merged = "\n\n".join(combined_parts)
    if len(merged) < AUDIT_MIN_TEXT_LENGTH:
        return None, []
    return merged, source_urls


# =============================================================================
#  DeepSeek 审计比对引擎
# =============================================================================

_AUDIT_SYSTEM_PROMPT = (
    "你是一位资深矿业合规审计员。\n"
    "下面会给你两段信息：\n"
    "  1.【待审计基线】—— 系统当前记录的该国政策基线\n"
    "  2.【近期情报】—— 联网采编的最新新闻\n\n"
    "你的任务：逐条比对，判断基线中的每条规则是否仍有效。\n\n"
    "【判定标准 · 严格保守】\n"
    "- 「推翻」：仅在有明确官方公告、法规编号、或权威来源报道新法案取代旧法案时，才判定为推翻。\n"
    "- 「实质性修改」：政策方向未变，但关键参数变化（如税率从16%变成20%，禁令时间从2027推迟到2028）。\n"
    "- 「仍有效」：基线规则与近期情报一致，或情报仅为背景介绍/行业评论，无新进展。\n"
    "- 「情报不足」：近期情报与该国该矿种完全无关、或搜不到相关新闻。\n\n"
    "【输出格式 · 严格 JSON】\n"
    '{"changed": false}  或\n'
    '{"changed": true, "broken_rules": ["规则1关键词", "规则2关键词"], "new_facts": "描述新政策事实，限200字", "severity": "high|medium|low"}'
)


def _audit_single_country(country_code, country_name, rules, client):
    """
    审计单个国家的基线时效性。
    返回 {"changed": bool, "report": str, "detail": dict|None}
    """
    print(f"\n🕵️ 审计 {country_code} ({country_name}) ...")
    print(f"   基线规则数: {len(rules)}")

    # 1. 采集近期新闻
    news_text, news_urls = _fetch_news_for_country(country_code, country_name)
    if not news_text:
        print(f"   ⚪ 情报不足（未采集到相关新闻），跳过。")
        return {"changed": False, "report": f"{country_code}: 情报不足，跳过", "detail": None}

    print(f"   采集到 {len(news_urls)} 条相关新闻，共 {len(news_text)} 字符")

    # 2. 调用 DeepSeek 比对
    rules_text = "\n".join(f"  [{i}] {r}" for i, r in enumerate(rules))

    try:
        response = client.chat_completion(
            task_type="baseline_audit",
            model="deepseek-v4-pro",
            messages=[
                {"role": "system", "content": _AUDIT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"【待审计基线】{country_name} ({country_code})：\n{rules_text}\n\n"
                        f"【近期情报】\n{news_text[:4000]}\n\n"
                        f"请按 JSON 格式输出审计结论。"
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.0,  # v5.0: 确定性输出 → 提升缓存命中率
            timeout=60,
        )
        content = response.choices[0].message.content
        result = json.loads(content) if content else {"changed": False}
    except Exception as e:
        print(f"   ❌ DeepSeek 审计调用失败: {str(e)[:100]}")
        return {"changed": False, "report": f"{country_code}: API 调用失败，跳过", "detail": None}

    changed = result.get("changed", False)
    if changed:
        broken = result.get("broken_rules", [])
        new_facts = result.get("new_facts", "")
        severity = result.get("severity", "medium")
        print(f"   🚨 检测到基线异动！严重程度: {severity}")
        if broken:
            print(f"   失效规则: {broken}")
        print(f"   新事实: {new_facts[:200]}")
        return {
            "changed": True,
            "report": f"{country_code}: 🚨 基线可能过期 (severity={severity})",
            "detail": result,
        }
    else:
        print(f"   ✅ 基线仍有效。")
        return {"changed": False, "report": f"{country_code}: 基线仍有效", "detail": None}


# =============================================================================
#  汇总与输出
# =============================================================================

def _update_yaml_timestamp(filepath):
    """仅更新 YAML 文件头部的 last_updated 时间戳"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    import re
    new_content = re.sub(
        r'last_updated:\s*".*"',
        f'last_updated: "{today}"',
        content,
        count=1,
    )
    if new_content != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"\n📅 last_updated 已更新为 {today}")
        return True
    print("\nℹ️ last_updated 无需更新（已是最新日期）。")
    return False


def _print_ci_warning(message):
    """GitHub Actions 兼容的告警输出"""
    print(f"\n::warning::{message}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Baseline Auditor")
    parser.add_argument("--country", help="仅审计指定国家代码（如 ID）")
    parser.add_argument("--update-timestamp", action="store_true", help="仅更新 last_updated 时间戳")
    args = parser.parse_args()

    # 仅更新时间戳模式
    if args.update_timestamp:
        _update_yaml_timestamp(BASELINE_PATH)
        sys.exit(0)

    # 加载基线
    print("=" * 60)
    print("🕵️ 基线时效性影子审计")
    print(f"   时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    if not os.path.isfile(BASELINE_PATH):
        print("❌ knowledge_baselines.yaml 不存在，审计终止。")
        sys.exit(1)

    with open(BASELINE_PATH, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}
    baselines = config.get("baselines", {})
    print(f"📚 已加载 {len(baselines)} 个国家基线: {list(baselines.keys())}")

    # 单国模式
    if args.country:
        code = args.country.upper()
        if code not in baselines:
            print(f"❌ 国家代码 {code} 不在基线库中。")
            sys.exit(1)
        entry = baselines[code]
        client = _get_llm_client()
        result = _audit_single_country(code, entry["country"], entry["rules"], client)
        if result["changed"]:
            _print_ci_warning(f"基线异动: {result['report']}")
            print(json.dumps(result["detail"], ensure_ascii=False, indent=2))
        sys.exit(0)

    # 全量审计模式
    client = _get_llm_client()
    all_ok = True
    changes_found = []

    for code, entry in baselines.items():
        if not entry.get("rules"):
            print(f"\nℹ️ {code} ({entry.get('country', '')}) 无规则定义，跳过。")
            continue
        result = _audit_single_country(code, entry["country"], entry["rules"], client)
        if result["changed"]:
            all_ok = False
            changes_found.append(result)
        time.sleep(1)  # 礼貌性间隔，避免 API 限流

    # 汇总
    print("\n" + "=" * 60)
    if all_ok:
        print("✅ 全部基线审计通过，无时效性问题。")
        _update_yaml_timestamp(BASELINE_PATH)
    else:
        print(f"🚨 发现 {len(changes_found)} 个国家基线可能过期：")
        for r in changes_found:
            print(f"   - {r['report']}")
            if r.get("detail"):
                broken = r["detail"].get("broken_rules", [])
                if broken:
                    print(f"     失效规则: {broken}")
                new_facts = r["detail"].get("new_facts", "")
                if new_facts:
                    print(f"     新事实: {new_facts[:200]}")
        _print_ci_warning(
            f"{len(changes_found)} 个国家基线可能过期，请人工核实后编辑 knowledge_baselines.yaml。"
        )
    print("=" * 60)
    
    # v5.0: 输出缓存命中统计
    _get_llm_client().print_stats()
