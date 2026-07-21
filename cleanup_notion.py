"""
Notion 数据库例行清理与去重工具 (Notion Database Clean-up & Deduplication Tool)
用法: python cleanup_notion.py
安全机制: 仅执行页面归档 (Trash)，并配有年份及缩写冲突防呆隔离，不破坏核心资产
v5.5: 支持内容合并归档，将重复页面的子报道和评论无损追加到保留页面中
"""
import os
import re
import requests
from dotenv import load_dotenv
from radar_infra.guard import (
    clean_chinese_title_noise,
    clean_title_noise,
    get_tokens,
    calculate_title_similarity,
)

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

def remove_none_values(d):
    """递归地从字典/列表中移出值为 None 的键值对"""
    if isinstance(d, dict):
        return {k: remove_none_values(v) for k, v in d.items() if v is not None}
    elif isinstance(d, list):
        return [remove_none_values(x) for x in d]
    else:
        return d

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
    v6.0: 语义相似度校验：计算两个政策标题的重合度，并应用年份/缩写防误判规则。
    修复 v5.5 括号内英文简称在中文 overlap 分支中误碰大长文导致误判的漏洞。
    直接剔除括号修饰成分，并将中英文相似度计算分支进行汉字/英文分词级强隔离。
    """
    if not s1 or not s2:
        return False

    # 0. 去除所有的括号及其内容，防止非特异性的括号内容（如年份、阶段）干扰相似度计算
    s1_clean_br = re.sub(r'[（\(].*?[）\)]', '', s1).strip()
    s2_clean_br = re.sub(r'[（\(].*?[）\)]', '', s2).strip()

    if not s1_clean_br or not s2_clean_br:
        return False

    # 1. 强力防误判：年份不一致直接判定不相同
    years1 = set(re.findall(r'(?<!\d)(20[2-3]\d)(?!\d)', s1_clean_br))
    years2 = set(re.findall(r'(?<!\d)(20[2-3]\d)(?!\d)', s2_clean_br))
    if years1 and years2 and years1 != years2:
        return False

    # 2. 强力防误判：特有英文缩写（如 CSDDD, CSRD）不一致直接判定不相同
    acronyms1 = set(re.findall(r'(?<![a-zA-Z])([A-Z]{3,5})(?![a-zA-Z])', s1_clean_br))
    acronyms2 = set(re.findall(r'(?<![a-zA-Z])([A-Z]{3,5})(?![a-zA-Z])', s2_clean_br))
    exclude_acr = {"DRC", "EU", "USA", "UK", "G7", "REE", "NPI", "DSI", "RKAB", "BUMN", "ESDM"}
    acronyms1 = acronyms1 - exclude_acr
    acronyms2 = acronyms2 - exclude_acr
    if acronyms1 and acronyms2 and acronyms1 != acronyms2:
        return False

    # 3. 判断是否都包含中文，且中文核部分足够长
    has_zh1 = any('\u4e00' <= char <= '\u9fff' for char in s1_clean_br)
    has_zh2 = any('\u4e00' <= char <= '\u9fff' for char in s2_clean_br)

    if has_zh1 and has_zh2:
        s1_clean = clean_chinese_title_noise(s1_clean_br.lower().replace(" ", ""))
        s2_clean = clean_chinese_title_noise(s2_clean_br.lower().replace(" ", ""))
        
        # 仅保留纯汉字字符，彻底排除英文字母/数字在中文分支的 overlap 碰撞
        s1_zh = "".join(c for c in s1_clean if '\u4e00' <= c <= '\u9fff')
        s2_zh = "".join(c for c in s2_clean if '\u4e00' <= c <= '\u9fff')
        
        if len(s1_zh) >= 3 and len(s2_zh) >= 3:
            set1 = set(s1_zh)
            set2 = set(s2_zh)
            common = set1.intersection(set2)
            overlap_ratio = len(common) / min(len(set1), len(set2)) if min(len(set1), len(set2)) > 0 else 0.0
            jaccard_ratio = len(common) / len(set1.union(set2)) if len(set1.union(set2)) > 0 else 0.0
            return overlap_ratio >= 0.85 or jaccard_ratio >= 0.65

    # 4. 若不满足双中文条件，则剔除所有中文字符后，使用英文 Token 级 Jaccard / Overlap 比对
    s1_en = re.sub(r'[\u4e00-\u9fff]', ' ', s1_clean_br)
    s2_en = re.sub(r'[\u4e00-\u9fff]', ' ', s2_clean_br)
    
    set1 = get_tokens(s1_en)
    set2 = get_tokens(s2_en)
    if not set1 or not set2:
        return False
    common = set1.intersection(set2)
    overlap_ratio = len(common) / min(len(set1), len(set2))
    jaccard_ratio = len(common) / len(set1.union(set2))
    return overlap_ratio >= 0.85 or jaccard_ratio >= 0.60


def merge_notion_pages(keep_id, dup_id):
    """v5.5: 将 dup_id 页面的内容和报道数合并至 keep_id 页面，然后归档 dup_id"""
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    
    # 1. 获取属性
    res_dup = requests.get(f"https://api.notion.com/v1/pages/{dup_id}", headers=headers, timeout=15)
    if res_dup.status_code != 200:
        print(f"  ❌ 无法读取待归档页面 {dup_id} 的属性")
        return False
    dup_data = res_dup.json()
    dup_props = dup_data.get("properties", {})
    dup_count = dup_props.get("关联报道数", {}).get("number") or 0
    dup_url = dup_props.get("原文链接", {}).get("url") or ""
    
    res_keep = requests.get(f"https://api.notion.com/v1/pages/{keep_id}", headers=headers, timeout=15)
    if res_keep.status_code != 200:
        print(f"  ❌ 无法读取保留页面 {keep_id} 的属性")
        return False
    keep_data = res_keep.json()
    keep_props = keep_data.get("properties", {})
    keep_count = keep_props.get("关联报道数", {}).get("number") or 0
    keep_url = keep_props.get("原文链接", {}).get("url") or ""
    
    # 2. 合并属性：累加报道数，回填空 URL
    new_count = keep_count + dup_count
    update_payload = {
        "properties": {
            "关联报道数": {"number": new_count}
        }
    }
    if not keep_url and dup_url:
        update_payload["properties"]["原文链接"] = {"url": dup_url}
        
    requests.patch(f"https://api.notion.com/v1/pages/{keep_id}", headers=headers, json=update_payload, timeout=15)
    print(f"  🔄 属性已同步：关联报道数 {keep_count} -> {new_count}")
    
    # 3. 转移子块内容
    blocks_url = f"https://api.notion.com/v1/blocks/{dup_id}/children?page_size=100"
    res_blocks = requests.get(blocks_url, headers=headers, timeout=20)
    if res_blocks.status_code == 200:
        blocks = res_blocks.json().get("results", [])
        clean_blocks = []
        for b in blocks:
            b_type = b.get("type")
            if b_type:
                type_data = b.get(b_type, {})
                clean_type_data = remove_none_values(type_data)
                clean_b = {
                    "object": "block",
                    "type": b_type,
                    b_type: clean_type_data
                }
                clean_blocks.append(clean_b)
        
        if clean_blocks:
            append_url = f"https://api.notion.com/v1/blocks/{keep_id}/children"
            success_all = True
            for chunk_start in range(0, len(clean_blocks), 50):
                chunk = clean_blocks[chunk_start:chunk_start+50]
                res_append = requests.patch(append_url, headers=headers, json={"children": chunk}, timeout=20)
                if res_append.status_code != 200:
                    print(f"  ⚠️ 追加子块失败: {res_append.status_code} - {res_append.text}")
                    success_all = False
            if success_all:
                print(f"  📎 已成功将 {len(clean_blocks)} 个子块转移至保留页面")
            else:
                print(f"  ❌ 转移子块发生部分或全部失败，暂不归档以保留原始数据")
                return False
            
    # 4. 执行归档
    url_archive = f"https://api.notion.com/v1/pages/{dup_id}"
    res_archive = requests.patch(url_archive, headers=headers, json={"archived": True}, timeout=15)
    return res_archive.status_code == 200


def score_page(p):
    s = 0
    props = p.get("properties", {})
    if props.get("覆盖地区", {}).get("multi_select"): s += 10
    if props.get("主矿种（可选）", {}).get("select"): s += 10
    if props.get("当前阶段", {}).get("select"): s += 5
    return s


def main():
    pages = list_all_pages()
    print(f"📋 数据库读取到 {len(pages)} 条记录")
    
    archive_ids = set()
    merge_targets = {}
    reasons = {}
    
    # 1. 精确 URL 查重
    url_groups = {}
    for page in pages:
        pid = page["id"]
        props = page.get("properties", {})
        url_prop = props.get("原文链接", {})
        url = url_prop.get("url") or ""
        url = url.strip()
        if url and url not in ("disabled", ""):
            url_groups.setdefault(url, []).append(page)
            
    for url, group in url_groups.items():
        if len(group) > 1:
            sorted_group = sorted(group, key=score_page, reverse=True)
            keep_page = sorted_group[0]
            keep_title = get_title(keep_page)
            print(f"⚠️ 检出相同 URL 的重复政策条目，保留最完整版本：『{keep_title}』")
            for dup in sorted_group[1:]:
                dup_id = dup["id"]
                dup_title = get_title(dup)
                if dup_id not in archive_ids:
                    archive_ids.add(dup_id)
                    merge_targets[dup_id] = keep_page["id"]
                    reasons[dup_id] = f"相同 URL 去重合并 ({url[:50]}...)"
                    print(f"   🗑️ 待合并重复项: 『{dup_title}』 (ID: {dup_id}) -> 合并至 『{keep_title}』")

    # 2. 暴力语义查重与相似合并
    # 遍历所有尚未被合并的页面，执行两两语义查重，确保无遗漏
    active_pages = [p for p in pages if p["id"] not in archive_ids]
    
    similar_groups = []
    visited_ids = set()
    for i in range(len(active_pages)):
        p1 = active_pages[i]
        id1 = p1["id"]
        if id1 in visited_ids:
            continue
        title1 = get_title(p1)
        cn1 = get_chinese_name(p1)
        
        group = [p1]
        for j in range(i + 1, len(active_pages)):
            p2 = active_pages[j]
            id2 = p2["id"]
            if id2 in visited_ids:
                continue
            title2 = get_title(p2)
            cn2 = get_chinese_name(p2)
            
            if _calculate_title_similarity(title1, title2) or (cn1 and cn2 and _calculate_title_similarity(cn1, cn2)):
                group.append(p2)
                
        if len(group) > 1:
            similar_groups.append(group)
            for p in group:
                visited_ids.add(p["id"])
                
    for group in similar_groups:
        sorted_group = sorted(group, key=score_page, reverse=True)
        keep_page = sorted_group[0]
        keep_title = get_title(keep_page)
        print(f"⚠️ 检出高度相似政策组，保留最完整版本：『{keep_title}』")
        for dup in sorted_group[1:]:
            dup_id = dup["id"]
            dup_title = get_title(dup)
            if dup_id not in archive_ids:
                archive_ids.add(dup_id)
                merge_targets[dup_id] = keep_page["id"]
                reasons[dup_id] = "相似政策去重合并"
                print(f"   🗑️ 待合并重复项: 『{dup_title}』 (ID: {dup_id}) -> 合并至 『{keep_title}』")

    # 3. 垃圾自媒体/非官方信源与新闻污染监测（仅针对尚未有合并目标的活跃页面）
    garbage_domains = ["legalinsurrection.com", "blogspot.com", "wordpress.com", "livejournal.com", "tumblr.com"]
    news_domains = [
        "reuters.com", "bloomberg.com", "cryptobriefing.com", "spglobal.com", "mining.com", "scmp.com"
    ]
    news_keywords = [
        "报道", "称", "预计", "预计将", "计划", "或将", "宣布", "考虑", "讨论", "拟",
        "Says", "Plans", "Expects", "Considers", "Reports", "Discusses", "Announces",
        "指出", "表示", "声称", "警告", "透露", "项目", "收购", "招标", "招标编号", "Solicitation"
    ]
    exempt_keywords = ["csddd", "csrd", "cbam", "battery regulation", "电池法案", "critical raw materials act"]

    remaining_active = [p for p in pages if p["id"] not in archive_ids]
    for page in remaining_active:
        props = page.get("properties", {})
        pid = page["id"]
        title = get_title(page)
        cn_name = get_chinese_name(page)
        
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
            continue

        url_lower = url.lower()
        is_media_url = any(dom in url_lower for dom in news_domains)
        
        commercial_keywords = ["收购项目", "项目收购", "资产收购", "招标项目", "招标编号", "Solicitation", "Acquisition of Chemaf", "矿山收购"]
        is_commercial = any(kw in title or kw in cn_name for kw in commercial_keywords)
        
        matched_kws = [kw for kw in news_keywords if kw.lower() in title.lower() or kw in cn_name]
        is_rumor_keywords = len(matched_kws) >= 2
        
        is_exempt = any(ekw in title.lower() or ekw in cn_name.lower() for ekw in exempt_keywords)
        
        if (is_commercial or (is_media_url and not is_exempt) or (is_rumor_keywords and not is_exempt)):
            archive_ids.add(pid)
            reasons[pid] = "新闻媒体报道或企业商业行为污染"
            print(f"🚨 检出新闻/商业污染条目: 『{title}』(ID: {pid})")

    # 执行归档与合并
    if not archive_ids:
        print("✅ 数据库健康，未发现冗余或污染条目。")
        return

    print(f"\n🚀 开始执行合并与清理，共需处理 {len(archive_ids)} 个条目...")
    success = fail = 0
    for pid in archive_ids:
        title = ""
        for p in pages:
            if p["id"] == pid:
                title = get_title(p)
                break
                
        keep_id = merge_targets.get(pid)
        if keep_id:
            ok = merge_notion_pages(keep_id, pid)
        else:
            ok = archive_page(pid)
            
        if ok:
            success += 1
            print(f"  🗑️ 已合并/归档: 『{title}』 - 原因: {reasons[pid]}")
        else:
            fail += 1
            print(f"  ❌ 操作失败: 『{title}』 (ID: {pid})")
            
    print(f"\n🎉 维护保养完成：成功处理 {success} 条，失败 {fail} 条。")

if __name__ == "__main__":
    main()
