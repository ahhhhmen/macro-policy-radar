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
    v5.5: 语义相似度校验：计算两个政策标题的重合度，并应用年份/缩写防误判规则。
    对中文进行噪声过滤后计算字符重合度，对英文使用单词 Token 级 Jaccard。
    支持括号内简称/全称递归比对。
    """
    if not s1 or not s2:
        return False

    # 1. 强力防误判：年份不一致直接判定不相同
    years1 = set(re.findall(r'(?<!\d)(20[2-3]\d)(?!\d)', s1))
    years2 = set(re.findall(r'(?<!\d)(20[2-3]\d)(?!\d)', s2))
    if years1 and years2 and years1 != years2:
        return False

    # 2. 强力防误判：特有英文缩写（如 CSDDD, CSRD）不一致直接判定不相同
    acronyms1 = set(re.findall(r'(?<![a-zA-Z])([A-Z]{3,5})(?![a-zA-Z])', s1))
    acronyms2 = set(re.findall(r'(?<![a-zA-Z])([A-Z]{3,5})(?![a-zA-Z])', s2))
    exclude_acr = {"DRC", "EU", "USA", "UK", "G7", "REE", "NPI", "DSI", "RKAB", "BUMN", "ESDM"}
    acronyms1 = acronyms1 - exclude_acr
    acronyms2 = acronyms2 - exclude_acr
    if acronyms1 and acronyms2 and acronyms1 != acronyms2:
        return False

    # 3. 递归比对括号内容
    p1 = re.findall(r'[（\(]([^）\)]+)[）\)]', s1)
    p2 = re.findall(r'[（\(]([^）\)]+)[）\)]', s2)
    for part in p1:
        part_clean = part.strip()
        if len(part_clean) >= 3 and part_clean.upper() not in {"EU", "USA", "UK", "G7"}:
            if _calculate_title_similarity(part_clean, re.sub(r'[（\(].*?[）\)]', '', s2).strip()):
                return True
    for part in p2:
        part_clean = part.strip()
        if len(part_clean) >= 3 and part_clean.upper() not in {"EU", "USA", "UK", "G7"}:
            if _calculate_title_similarity(re.sub(r'[（\(].*?[）\)]', '', s1).strip(), part_clean):
                return True

    # 4. 判断是否包含中文
    has_zh1 = any('\u4e00' <= char <= '\u9fff' for char in s1)
    has_zh2 = any('\u4e00' <= char <= '\u9fff' for char in s2)

    if has_zh1 or has_zh2:
        s1_clean = clean_chinese_title_noise(s1.lower().replace(" ", ""))
        s2_clean = clean_chinese_title_noise(s2.lower().replace(" ", ""))
        if not s1_clean or not s2_clean:
            return False
        set1 = set(s1_clean)
        set2 = set(s2_clean)
        common = set1.intersection(set2)
        overlap_ratio = len(common) / min(len(set1), len(set2)) if min(len(set1), len(set2)) > 0 else 0.0
        jaccard_ratio = len(common) / len(set1.union(set2)) if len(set1.union(set2)) > 0 else 0.0
        return overlap_ratio >= 0.85 or jaccard_ratio >= 0.65
    else:
        set1 = get_tokens(s1)
        set2 = get_tokens(s2)
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
    
    # 1. 垃圾自媒体/非官方信源监测
    garbage_domains = ["legalinsurrection.com", "blogspot.com", "wordpress.com", "livejournal.com", "tumblr.com"]
    for page in pages:
        props = page.get("properties", {})
        pid = page["id"]
        title = get_title(page)
        
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

    # 2. 精确 URL 查重
    url_groups = {}
    for page in pages:
        pid = page["id"]
        if pid in archive_ids:
            continue
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

    # 3. 前缀聚类与语义去重
    prefix_groups = {}
    for page in pages:
        pid = page["id"]
        if pid in archive_ids:
            continue
            
        title = get_title(page)
        cn_name = get_chinese_name(page)
        
        # 英文前缀聚类
        if title:
            cleaned_en = clean_title_noise(title)
            normalized_en = re.sub(r'[（\(].*?[）\)]', '', cleaned_en).strip()
            words = normalized_en.split()
            if len(words) >= 3:
                en_prefix = " ".join(words[:3]).lower()
                prefix_groups.setdefault(en_prefix, []).append(page)
            elif len(normalized_en) >= 3:
                prefix_groups.setdefault(normalized_en.lower(), []).append(page)
                
        # 中文前缀聚类
        if cn_name:
            normalized_cn = cn_name.replace("政策", "").replace("条例", "").replace("法案", "").replace("体系", "").replace("（现行政策）", "").strip()
            cn_core = clean_chinese_title_noise(normalized_cn)
            if len(cn_core) >= 3:
                cn_prefix = cn_core[:5]
                prefix_groups.setdefault(cn_prefix, []).append(page)

    for prefix, group in prefix_groups.items():
        if len(group) > 1:
            matched_pairs = []
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    p1 = group[i]
                    p2 = group[j]
                    if p1["id"] in archive_ids or p2["id"] in archive_ids:
                        continue
                    t1 = get_title(p1)
                    t2 = get_title(p2)
                    if _calculate_title_similarity(t1, t2):
                        matched_pairs.append((p1, p2))
            
            if matched_pairs:
                unique_matched_pages = []
                for p1, p2 in matched_pairs:
                    if p1 not in unique_matched_pages: unique_matched_pages.append(p1)
                    if p2 not in unique_matched_pages: unique_matched_pages.append(p2)
                
                sorted_group = sorted(unique_matched_pages, key=score_page, reverse=True)
                keep_page = sorted_group[0]
                keep_title = get_title(keep_page)
                
                print(f"⚠️ 检出高度相似政策组 (前缀: 『{prefix}』)，保留最完整版本：『{keep_title}』")
                for dup in sorted_group[1:]:
                    dup_id = dup["id"]
                    dup_title = get_title(dup)
                    if dup_id not in archive_ids:
                        archive_ids.add(dup_id)
                        merge_targets[dup_id] = keep_page["id"]
                        reasons[dup_id] = f"相似政策去重合并 (前缀 '{prefix}')"
                        print(f"   🗑️ 待合并重复项: 『{dup_title}』 (ID: {dup_id}) -> 合并至 『{keep_title}』")

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
