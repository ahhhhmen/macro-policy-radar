"""
Notion 数据库例行清理与去重工具 (Notion Database Clean-up & Deduplication Tool)
用法: python cleanup_notion.py
安全机制: 仅执行页面归档 (Trash)，并配有年份及缩写冲突防呆隔离，不破坏核心资产
"""
import os
import re
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("NOTION_TOKEN")
DB_ID = os.environ.get("NOTION_DATABASE_ID")

if not TOKEN or not DB_ID or TOKEN == "disabled":
    print("❌ Notion 凭证未配置或已禁用。")
    exit(1)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

def list_all_pages():
    pages = []
    url = f"https://api.notion.com/v1/databases/{DB_ID}/query"
    payload = {"page_size": 100}
    while True:
        res = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        if res.status_code != 200:
            print(f"Error querying Notion: {res.status_code} - {res.text}")
            break
        data = res.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return pages

def archive_page(page_id):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    res = requests.patch(url, headers=HEADERS, json={"archived": True}, timeout=15)
    return res.status_code == 200

def get_text_from_rich_text(prop):
    if not prop or prop.get("type") != "rich_text":
        return ""
    return "".join([t.get("plain_text", "") for t in prop["rich_text"]])

def get_title(page):
    props = page.get("properties", {})
    title_prop = props.get("政策名称", {})
    if title_prop and title_prop.get("title"):
        return title_prop["title"][0].get("plain_text", "").strip()
    return ""

def get_chinese_name(page):
    props = page.get("properties", {})
    cn_prop = props.get("中文名称", {})
    return get_text_from_rich_text(cn_prop).strip()

def _calculate_title_similarity(s1, s2):
    """
    计算两个政策标题的重合度，应用年份/缩写防误判规则。
    """
    if not s1 or not s2:
        return False

    # 1. 年份防冲突
    years1 = set(re.findall(r'(?<!\d)(20[2-3]\d)(?!\d)', s1))
    years2 = set(re.findall(r'(?<!\d)(20[2-3]\d)(?!\d)', s2))
    if years1 and years2 and years1 != years2:
        return False

    # 2. 英文缩写（如 CSDDD, CSRD）防冲突
    acronyms1 = set(re.findall(r'(?<![a-zA-Z])([A-Z]{3,5})(?![a-zA-Z])', s1))
    acronyms2 = set(re.findall(r'(?<![a-zA-Z])([A-Z]{3,5})(?![a-zA-Z])', s2))
    exclude_acr = {"DRC", "EU", "USA", "UK", "G7", "REE", "NPI", "DSI", "RKAB", "BUMN", "ESDM"}
    acronyms1 = acronyms1 - exclude_acr
    acronyms2 = acronyms2 - exclude_acr
    if acronyms1 and acronyms2 and acronyms1 != acronyms2:
        return False

    # 3. 计算重合占比与 Jaccard 相似度
    s1_clean = s1.lower().replace(" ", "").replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    s2_clean = s2.lower().replace(" ", "").replace("(", "").replace(")", "").replace("（", "").replace("）", "")
    
    set1 = set(s1_clean)
    set2 = set(s2_clean)
    common = set1.intersection(set2)
    
    overlap_ratio = len(common) / min(len(set1), len(set2)) if min(len(set1), len(set2)) > 0 else 0.0
    jaccard_ratio = len(common) / len(set1.union(set2)) if len(set1.union(set2)) > 0 else 0.0
    
    return overlap_ratio >= 0.85 or jaccard_ratio >= 0.65

def main():
    pages = list_all_pages()
    print(f"📋 数据库读取到 {len(pages)} 条记录")
    
    archive_ids = set()
    reasons = {}
    
    # 1. 垃圾自媒体/非官方信源监测
    garbage_domains = ["legalinsurrection.com", "blogspot.com", "wordpress.com", "livejournal.com", "tumblr.com"]
    for page in pages:
        props = page.get("properties", {})
        pid = page["id"]
        title = get_title(page)
        
        # 提取相关链接和文本
        url_prop = props.get("原文链接", {})
        url = url_prop.get("url", "") or ""
        factual_text = get_text_from_rich_text(props.get("事实依据", ""))
        summary_text = get_text_from_rich_text(props.get("要点摘要", ""))
        full_text = f"{title} {url} {factual_text} {summary_text}".lower()
        
        is_garbage = False
        matched = ""
        for dom in garbage_domains:
            if dom in full_text:
                is_garbage = True
                matched = dom
                break
                
        if is_garbage:
            archive_ids.add(pid)
            reasons[pid] = f"垃圾自媒体污染 (包含 {matched})"
            print(f"🚨 检出垃圾信源条目: 『{title}』(ID: {pid})")

    # 2. 前缀聚类与语义去重
    prefix_groups = {}
    for page in pages:
        pid = page["id"]
        if pid in archive_ids:
            continue
            
        title = get_title(page)
        cn_name = get_chinese_name(page)
        key_name = cn_name if cn_name else title
        if not key_name:
            continue
            
        # 提取核心词前缀进行聚类
        prefix = ""
        normalized = key_name.replace("政策", "").replace("条例", "").replace("法案", "").replace("体系", "").replace("（现行政策）", "").strip()
        if any('\u4e00' <= char <= '\u9fff' for char in normalized):
            prefix = normalized[:5]
        else:
            prefix = " ".join(normalized.split()[:3]).lower()
            
        if len(prefix) >= 3:
            prefix_groups.setdefault(prefix, []).append(page)

    for prefix, group in prefix_groups.items():
        if len(group) > 1:
            # 内部进行 N^2 匹配，验证是否确实相似（引入年份和英文缩写防呆）
            matched_pairs = []
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    t1 = get_title(group[i])
                    t2 = get_title(group[j])
                    if _calculate_title_similarity(t1, t2):
                        matched_pairs.append((group[i], group[j]))
            
            if matched_pairs:
                # 扁平化所有匹配的页面，按照打分保留 1 个，其余归档
                unique_matched_pages = []
                for p1, p2 in matched_pairs:
                    if p1 not in unique_matched_pages: unique_matched_pages.append(p1)
                    if p2 not in unique_matched_pages: unique_matched_pages.append(p2)
                
                def score_page(p):
                    s = 0
                    props = p.get("properties", {})
                    if props.get("覆盖地区", {}).get("multi_select"): s += 10
                    if props.get("主矿种（可选）", {}).get("select"): s += 10
                    if props.get("当前阶段", {}).get("select"): s += 5
                    return s

                sorted_group = sorted(unique_matched_pages, key=score_page, reverse=True)
                keep_page = sorted_group[0]
                keep_title = get_title(keep_page)
                
                print(f"⚠️ 检出高度相似政策组 (前缀: 『{prefix}』)，保留最完整版本：『{keep_title}』")
                for dup in sorted_group[1:]:
                    dup_id = dup["id"]
                    dup_title = get_title(dup)
                    if dup_id not in archive_ids:
                        archive_ids.add(dup_id)
                        reasons[dup_id] = f"重复项清理 (前缀 '{prefix}' 组内降级)"
                        print(f"   🗑️ 待归档重复项: 『{dup_title}』 (ID: {dup_id})")

    # 执行归档
    if not archive_ids:
        print("✅ 数据库健康，未发现冗余或污染条目。")
        return

    print(f"\n🚀 开始执行清理，共需归档 {len(archive_ids)} 个条目...")
    success = fail = 0
    for pid in archive_ids:
        title = ""
        for p in pages:
            if p["id"] == pid:
                title = get_title(p)
                break
        ok = archive_page(pid)
        if ok:
            success += 1
            print(f"  🗑️ 已归档: 『{title}』 - 原因: {reasons[pid]}")
        else:
            fail += 1
            print(f"  ❌ 归档失败: 『{title}』 (ID: {pid})")
            
    print(f"\n🎉 维护保养完成：成功归档 {success} 条，失败 {fail} 条。")

if __name__ == "__main__":
    main()
