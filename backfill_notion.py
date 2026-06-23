#!/usr/bin/env python3
"""
一次性脚本：回填 Notion 数据库中所有 40 条记录的缺失属性。
- 基线种子记录（关联报道数=0）：从 knowledge_baselines.yaml 推导 发布机构/日期
- 新闻记录（关联报道数≥1）：通过 LLM 从页面标题+正文提取 发布机构

用法：python3 backfill_notion.py [--dry-run]
"""

import os, sys, json, re, yaml, requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from radar_infra.llm import DeepSeekProvider, CachedLLMClient

_llm_client = None

def _get_llm_client():
    global _llm_client
    if _llm_client is None:
        _llm_client = CachedLLMClient(DeepSeekProvider())
    return _llm_client

load_dotenv()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

DRY_RUN = "--dry-run" in sys.argv

# =============================================================================
# 词表：ISO 国家码 → 常见颁布机构（用于基线种子无 YAML 数据时的兜底映射）
# =============================================================================
_COUNTRY_AUTHORITY_FALLBACK = {
    "CN": "中华人民共和国国务院",
    "ID": "印度尼西亚共和国政府",
    "CD": "刚果（金）矿业部 (Ministère des Mines)",
    "CL": "智利矿业部 (Ministerio de Minería)",
    "AU": "澳大利亚工业、科学与资源部",
    "EU": "欧盟委员会 (European Commission)",
    "US": "美国国会 (U.S. Congress)",
    "PH": "菲律宾共和国政府",
    "ZW": "津巴布韦共和国政府",
    "GH": "加纳共和国政府",
    "AR": "阿根廷共和国政府",
    "JP": "日本国政府",
    "GLOBAL": "",
}

# 标准组织 → 颁布机构（无国家属性）
_STANDARD_AUTHORITY = {
    "IRMA": "负责任采矿保障倡议 (Initiative for Responsible Mining Assurance)",
    "RMAP": "负责任矿产倡议 (Responsible Minerals Initiative, RMI)",
    "Nickel Mark": "镍协会 (Nickel Institute)",
    "Copper Mark": "铜标记组织 (Copper Mark)",
}


# =============================================================================
# Step 1: 读取所有 Notion 记录，识别缺失字段
# =============================================================================
def fetch_all_records():
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        headers=HEADERS, json={"page_size": 100},
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def get_prop(props, name, field="plain_text"):
    """安全读取 Notion 属性值"""
    p = props.get(name, {})
    if not p:
        return ""
    ptype = p.get("type", "")
    if ptype == "title":
        items = p.get("title", [])
        return items[0].get("plain_text", "") if items else ""
    elif ptype == "rich_text":
        items = p.get("rich_text", [])
        return items[0].get("plain_text", "") if items else ""
    elif ptype == "select":
        s = p.get("select")
        return s.get("name", "") if s else ""
    elif ptype == "status":
        s = p.get("status")
        return s.get("name", "") if s else ""
    elif ptype == "date":
        d = p.get("date")
        return d.get("start", "") if d else ""
    elif ptype == "number":
        return p.get("number", 0)
    elif ptype == "multi_select":
        return [x["name"] for x in p.get("multi_select", [])]
    return ""


def analyze_records(records):
    """遍历记录，返回需要回填的列表 [{page_id, title, missing_fields, ...}]"""
    needs_fill = []
    for page in records:
        props = page["properties"]
        title = get_prop(props, "政策名称")
        file_type = get_prop(props, "文件类型")
        authority = get_prop(props, "发布机构")
        pub_date = get_prop(props, "发布日期")
        eff_date = get_prop(props, "生效日期")
        country = get_prop(props, "颁布国家")
        doc_count = get_prop(props, "关联报道数")
        page_id = page["id"]

        missing = []
        if not authority:
            missing.append("发布机构")
        if not file_type:
            missing.append("文件类型")
        if not pub_date:
            missing.append("发布日期")
        if not eff_date:
            missing.append("生效日期")

        if missing:
            needs_fill.append({
                "page_id": page_id,
                "title": title,
                "country": country,
                "file_type": file_type,
                "authority": authority,
                "pub_date": pub_date,
                "eff_date": eff_date,
                "doc_count": doc_count,
                "missing": missing,
                "is_baseline": doc_count == 0,
            })

    return needs_fill


# =============================================================================
# Step 2: 加载 YAML 基线库，为基线种子推导缺失属性
# =============================================================================
def load_baseline_yaml(path="knowledge_baselines.yaml"):
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    result = {}
    for cc, entry in config.get("baselines", {}).items():
        if entry and entry.get("documents"):
            for doc in entry["documents"]:
                oname = doc.get("official_name", "").strip()
                if oname:
                    result[oname] = {
                        "country": cc,
                        "type": doc.get("type", ""),
                        "effective_date": doc.get("effective_date", ""),
                        "chinese_name": doc.get("chinese_name", ""),
                        "short_name": doc.get("short_name", ""),
                    }
    return result


def derive_baseline_authority(title, country, file_type, yaml_data):
    """从基线 YAML 推导颁布机构"""
    # 标准组织 → 优先匹配
    for keyword, auth in _STANDARD_AUTHORITY.items():
        if keyword.lower() in title.lower():
            return auth

    # 按国家+文件类型映射
    if country == "EU":
        if "Directive" in title:
            return "欧洲议会与欧盟理事会 (European Parliament and Council)"
        if "Regulation" in title:
            return "欧洲议会与欧盟理事会 (European Parliament and Council)"
        return "欧盟委员会 (European Commission)"

    if country == "CN":
        if "出口管制法" in title or "Export Control Law" in title:
            return "全国人民代表大会常务委员会"
        if "管理条例" in title or "Regulation" in title:
            return "中华人民共和国国务院"
        if "条例" in title:
            return "中华人民共和国国务院"
        return "中华人民共和国国务院"

    if country == "ID":
        if "Undang-Undang" in title or "UU" in title:
            return "印度尼西亚共和国国会 (Dewan Perwakilan Rakyat)"
        if "Peraturan Pemerintah" in title or "PP" in title:
            return "印度尼西亚共和国政府 (Pemerintah Indonesia)"
        if "Peraturan Menteri" in title or "Permen" in title:
            return "印尼能源与矿产资源部 (Kementerian ESDM)"
        if "RKAB" in title:
            return "印尼能源与矿产资源部 (Kementerian ESDM)"
        return "印度尼西亚共和国政府"

    if country == "US":
        if "Inflation Reduction Act" in title or "IRA" in title:
            return "美国国会 (U.S. Congress)"
        if "Presidential" in title:
            return "美国白宫 (The White House)"
        if "Final Rule" in title:
            return "美国财政部/国税局 (U.S. Department of Treasury / IRS)"
        return "美国政府"

    if country == "CD":
        return "刚果（金）矿业部 (Ministère des Mines, RDC)"

    if country == "CL":
        return "智利矿业部 (Ministerio de Minería de Chile)"

    if country == "AU":
        return "澳大利亚工业、科学与资源部 (DISR)"

    if country == "ZW":
        return "津巴布韦矿业与矿业发展部"

    if country == "PH":
        return "菲律宾共和国国会 (Congress of the Philippines)"

    if country == "GH":
        return "加纳共和国政府 (Government of Ghana)"

    if country == "AR":
        return "阿根廷共和国政府 (Gobierno de Argentina)"

    # 兜底
    return _COUNTRY_AUTHORITY_FALLBACK.get(country, "")


# =============================================================================
# Step 3: 新闻记录 → 先尝试规则推导，失败则用 LLM 批量提取发布机构
# =============================================================================

# 新闻标题关键词 → 颁布机构（中文政策信号常见模式）
_NEWS_AUTHORITY_HEURISTICS = [
    # 中国
    ("商务部", "中华人民共和国商务部"),
    ("海关总署", "中华人民共和国海关总署"),
    ("国务院", "中华人民共和国国务院"),
    ("工信部", "中华人民共和国工业和信息化部"),
    ("发改委", "中华人民共和国国家发展和改革委员会"),
    ("推动关键矿产供应链尽责", "中国五矿化工进出口商会 (CCCMC)"),
    ("供应链尽责合规强制令", "中国五矿化工进出口商会 (CCCMC)"),
    ("稀土管理条例", "中华人民共和国国务院"),
    ("暂停稀土出口管制", "中华人民共和国商务部"),
    # 印尼
    ("BUMN Khusus", "印度尼西亚共和国政府 (Pemerintah Indonesia)"),
    ("成立特别国企整治SDA", "印度尼西亚共和国政府 (Pemerintah Indonesia)"),
    ("国家统购统销", "印度尼西亚共和国政府 (Pemerintah Indonesia)"),
    ("加征镍出口关税", "印度尼西亚共和国政府"),
    ("强化采矿RKAB", "印尼能源与矿产资源部 (Kementerian ESDM)"),
    # 美国
    ("Presidential Proclamation", "美国白宫 (The White House)"),
    ("Final Rule on Clean Vehicle", "美国财政部/国税局 (U.S. Treasury / IRS)"),
    ("Inflation Reduction Act", "美国国会 (U.S. Congress)"),
    # 澳大利亚
    ("未来澳大利亚制造", "澳大利亚工业、科学与资源部 (DISR)"),
    # 智利
    ("矿业发展法案", "智利矿业部 (Ministerio de Minería de Chile)"),
    ("PAMMA", "智利矿业部 (Ministerio de Minería de Chile)"),
    # 刚果（金）
    ("钴出口配额", "刚果（金）矿业部 (Ministère des Mines, RDC)"),
    # 欧盟
    ("OECD报告", "OECD (经济合作与发展组织)"),
    # 日本
    ("世界银行集团与日本", "世界银行集团 (World Bank Group)、日本政府"),
    # 全球
    ("稀土出口管制：日本汽车", "中华人民共和国商务部"),
]


def derive_news_authority_heuristic(title, country):
    """从新闻标题用规则推导颁布机构"""
    for keyword, auth in _NEWS_AUTHORITY_HEURISTICS:
        if keyword.lower() in title.lower():
            return auth
    # 国家兜底
    return _COUNTRY_AUTHORITY_FALLBACK.get(country, "")


def batch_extract_authorities(client, records):
    """批量用 LLM 提取多条记录的颁布机构（一次 API 调用）"""
    items_text = ""
    for i, rec in enumerate(records):
        items_text += f"[{i}] 标题: {rec['title'][:120]}\n    国家: {rec['country']}\n\n"

    prompt = f"""为以下每条政策记录提取颁布机构/发起机构的全称。

{items_text}
请为每条记录返回颁布机构，格式严格为 "序号: 机构名称"，每行一条。只返回机构名称，不要解释。"""

    try:
        content = client.chat_completion(
            task_type="policy_extraction",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=500,
        )
        if content:
            content = content.strip()
        results = {}
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            # 匹配 "[0] 机构名称" 或 "0: 机构名称" 或 "0. 机构名称"
            import re
            m = re.match(r'\[?(\d+)\]?\s*[:：.\-]\s*(.+)', line)
            if m:
                idx = int(m.group(1))
                auth = m.group(2).strip().strip('"\'""''')
                results[idx] = auth
        return results
    except Exception as e:
        print(f"    批量 LLM 提取失败: {e}")
        return {}


# =============================================================================
# Step 4: 更新 Notion 记录
# =============================================================================
DOC_TYPE_LABELS = {
    "Law": "法律 Law",
    "Regulation": "法规 Regulation",
    "Policy": "政策 Policy",
    "Standard": "标准 Standard",
    "Administrative_Order": "行政令 Admin_Order",
    "Other": "其他 Other",
}


def patch_record(page_id, updates):
    """PATCH 更新 Notion 记录属性"""
    if DRY_RUN:
        print(f"    [DRY RUN] 将更新: {json.dumps(updates, ensure_ascii=False)[:200]}")
        return True

    properties = {}
    if "发布机构" in updates and updates["发布机构"]:
        properties["发布机构"] = {"rich_text": [{"text": {"content": updates["发布机构"]}}]}
    if "文件类型" in updates and updates["文件类型"]:
        label = DOC_TYPE_LABELS.get(updates["文件类型"], "其他 Other")
        properties["文件类型"] = {"select": {"name": label}}
    if "发布日期" in updates and updates["发布日期"]:
        properties["发布日期"] = {"date": {"start": updates["发布日期"]}}
    if "生效日期" in updates and updates["生效日期"]:
        properties["生效日期"] = {"date": {"start": updates["生效日期"]}}

    if not properties:
        return True

    payload = {"properties": properties}
    try:
        resp = requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=HEADERS, json=payload, timeout=10,
        )
        if resp.status_code == 200:
            return True
        err_text = resp.text[:300]
        # 自动剔除不存在的属性
        if resp.status_code == 400 and "is not a property that exists" in err_text:
            import re
            missing_props = set(re.findall(r'([^\s."]+) is not a property that exists', err_text))
            if missing_props:
                properties = {k: v for k, v in properties.items() if k not in missing_props}
                payload["properties"] = properties
                resp2 = requests.patch(
                    f"https://api.notion.com/v1/pages/{page_id}",
                    headers=HEADERS, json=payload, timeout=10,
                )
                if resp2.status_code == 200:
                    return True
        print(f"    ⚠️ PATCH 失败 [{resp.status_code}]: {err_text}")
        return False
    except Exception as e:
        print(f"    ❌ PATCH 异常: {e}")
        return False


# =============================================================================
# Main
# =============================================================================
def main():
    print("=" * 60)
    print("Notion 数据库属性回填脚本")
    if DRY_RUN:
        print("🔍 DRY RUN 模式：仅检查，不实际更新")
    print("=" * 60)

    # 获取所有记录
    records = fetch_all_records()
    print(f"\n数据库共有 {len(records)} 条记录")

    # 分析缺失字段
    needs_fill = analyze_records(records)
    print(f"其中 {len(needs_fill)} 条记录有缺失字段需要回填")

    if not needs_fill:
        print("✅ 所有记录字段完整，无需回填。")
        return

    # 加载 YAML 基线库
    yaml_data = load_baseline_yaml()
    print(f"从 knowledge_baselines.yaml 加载 {len(yaml_data)} 条基线文档")

    # 分离基线种子和新闻记录
    baseline_records = [r for r in needs_fill if r["is_baseline"]]
    news_records = [r for r in needs_fill if not r["is_baseline"]]
    print(f"  - 基线种子（关联报道数=0）: {len(baseline_records)} 条")
    print(f"  - 新闻创建记录（关联报道数≥1）: {len(news_records)} 条")

    # ---- Pass 1: 基线种子 —— 从 YAML 推导 ----
    if baseline_records:
        print(f"\n{'─' * 40}")
        print("Pass 1: 回填基线种子记录...")
        for i, rec in enumerate(baseline_records, 1):
            updates = {}
            if "发布机构" in rec["missing"]:
                auth = derive_baseline_authority(
                    rec["title"], rec["country"], rec["file_type"], yaml_data
                )
                if auth:
                    updates["发布机构"] = auth

            # 从 YAML 匹配 effective_date
            for oname, ydoc in yaml_data.items():
                if oname[:60] in rec["title"] or rec["title"][:60] in oname:
                    if "发布日期" in rec["missing"] and ydoc.get("effective_date"):
                        updates["发布日期"] = ydoc["effective_date"]
                    if "生效日期" in rec["missing"] and ydoc.get("effective_date"):
                        updates["生效日期"] = ydoc["effective_date"]
                    break

            if updates:
                ok = patch_record(rec["page_id"], updates)
                status = "✅" if ok else "❌"
                print(f"  [{i}/{len(baseline_records)}] {status} {rec['title'][:50]} -> {updates}")
            else:
                print(f"  [{i}/{len(baseline_records)}] ⏭️  {rec['title'][:50]} 无更新")

    # ---- Pass 2: 新闻记录 —— 先规则推导，再 LLM 批量兜底 ----
    if news_records:
        print(f"\n{'─' * 40}")
        print("Pass 2: 回填新闻创建记录...")

        need_llm = []  # 规则无法推导的记录

        for i, rec in enumerate(news_records, 1):
            updates = {}

            if "发布机构" in rec["missing"]:
                auth = derive_news_authority_heuristic(rec["title"], rec["country"])
                if auth:
                    updates["发布机构"] = auth

            # 文件类型：中文标题常用模式推导
            if "文件类型" in rec["missing"]:
                title = rec["title"]
                if any(kw in title for kw in ["管理条例", "出口管制法", "法案", "Regulation", "Directive"]):
                    updates["文件类型"] = "Regulation"
                elif any(kw in title for kw in ["Proclamation", "Final Rule", "行政令"]):
                    updates["文件类型"] = "Administrative_Order"
                elif any(kw in title for kw in ["OECD", "世界银行", "扩大合作"]):
                    updates["文件类型"] = "Policy"
                else:
                    updates["文件类型"] = "Policy"  # 新闻类默认 Policy

            if updates:
                ok = patch_record(rec["page_id"], updates)
                status = "✅" if ok else "❌"
                print(f"  [{i}/{len(news_records)}] {status} {rec['title'][:45]} -> {updates}")
            else:
                print(f"  [{i}/{len(news_records)}] ⏭️  {rec['title'][:45]} 无更新")

            # 记录仍缺发布机构的，加入 LLM 兜底队列
            if "发布机构" in rec["missing"] and "发布机构" not in updates:
                need_llm.append((i - 1, rec))

        # LLM 批量兜底（仅规则无法覆盖的记录）
        if need_llm:
            print(f"\n  🤖 规则无法覆盖 {len(need_llm)} 条，调用 LLM 批量提取...")
            llm_client = _get_llm_client()
            llm_results = batch_extract_authorities(llm_client, [r for _, r in need_llm])

            for orig_idx, rec in need_llm:
                auth = llm_results.get(orig_idx, "")
                if auth:
                    ok = patch_record(rec["page_id"], {"发布机构": auth})
                    status = "✅" if ok else "❌"
                    print(f"    {status} [{rec['title'][:40]}] -> {auth}")
                else:
                    # 最终兜底：国家默认
                    fallback = _COUNTRY_AUTHORITY_FALLBACK.get(rec["country"], "")
                    if fallback:
                        ok = patch_record(rec["page_id"], {"发布机构": fallback})
                        print(f"    ⚠️ [{rec['title'][:40]}] LLM 无结果，使用国家兜底: {fallback}")

    # ---- 汇总 ----
    print(f"\n{'=' * 60}")
    if DRY_RUN:
        print("🔍 DRY RUN 完成。移除 --dry-run 参数以实际更新。")
    else:
        # 再次查询确认
        records2 = fetch_all_records()
        still_missing = analyze_records(records2)
        if still_missing:
            print(f"⚠️ 仍有 {len(still_missing)} 条记录存在缺失字段")
            total_missing = sum(len(r["missing"]) for r in still_missing)
            print(f"   缺失字段总数: {total_missing}")
        else:
            print("✅ 所有记录字段完整！")
    print("=" * 60)


if __name__ == "__main__":
    main()
