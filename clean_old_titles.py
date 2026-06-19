"""
精准删除 Notion 数据库中所有旧版标题（非情报级格式）的记录
- 情报级格式: [{国家码}] {动作+影响}：{法案核心} ({矿种})
- 旧版格式:  一切不以 [{XX}] 开头的标题
用法: python clean_old_titles.py
"""
import os, re, requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("NOTION_TOKEN")
DB_ID = os.environ.get("NOTION_DATABASE_ID")

if not TOKEN or not DB_ID or TOKEN == "disabled":
    print("❌ Notion 凭证未配置")
    exit(1)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

# ---- 情报级标题检测 ----
_INTEL_TITLE_RE = re.compile(r'^\[[A-Z]{2,6}\]')  # [CN], [ID], [EU], [GLOBAL] 等


def list_all_pages():
    pages = []
    url = f"https://api.notion.com/v1/databases/{DB_ID}/query"
    payload = {"page_size": 100}
    while True:
        res = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        data = res.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return pages


def get_title(page):
    try:
        props = page.get("properties", {})
        title_prop = props.get("政策名称", {})
        title_items = title_prop.get("title", [])
        if title_items:
            return title_items[0].get("plain_text", "")
    except Exception:
        pass
    return ""


def is_intel_title(title):
    """是否是情报级标题 [{国家码}] ..."""
    return bool(_INTEL_TITLE_RE.match(title))


def archive_page(page_id):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    return requests.patch(url, headers=HEADERS, json={"archived": True}, timeout=10)


if __name__ == "__main__":
    pages = list_all_pages()
    print(f"📋 数据库中共有 {len(pages)} 条记录\n")

    new_titles = []
    old_titles = []

    for page in pages:
        title = get_title(page)
        if is_intel_title(title):
            new_titles.append((page, title))
        else:
            old_titles.append((page, title))

    print(f"🆕 情报级标题（保留）: {len(new_titles)} 条")
    for _, t in new_titles:
        print(f"    ✅ {t[:80]}")

    print(f"\n🗑️  旧版标题（待删除）: {len(old_titles)} 条")
    for _, t in old_titles:
        print(f"    ❌ {t[:80]}")

    if not old_titles:
        print("\n✅ 数据库已全部为情报级标题，无需清理。")
        exit(0)

    confirm = input(f"\n⚠️  确认归档以上 {len(old_titles)} 条旧版记录？(y/N): ").strip().lower()
    if confirm != "y":
        print("❌ 已取消。")
        exit(0)

    success = fail = 0
    for i, (page, title) in enumerate(old_titles):
        res = archive_page(page["id"])
        if res.status_code == 200:
            success += 1
            print(f"  🗑️  [{i+1}/{len(old_titles)}] 已归档: {title[:60]}")
        else:
            fail += 1
            print(f"  ❌ [{i+1}/{len(old_titles)}] 归档失败 ({res.status_code}): {title[:60]}")

    print(f"\n✅ 完成：{success} 条已归档, {fail} 条失败, {len(new_titles)} 条情报级标题保留")
