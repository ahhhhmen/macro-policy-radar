"""
一键清空 Notion 数据库所有记录（批量归档）
用法: python clear_notion.py
安全: 仅归档（trash），可手动恢复
"""
import os, requests
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

def list_all_pages():
    """分页拉取数据库全部页面"""
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

def archive_page(page_id):
    """将页面归档（放入回收站）"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    return requests.patch(url, headers=HEADERS, json={"archived": True}, timeout=10)

if __name__ == "__main__":
    pages = list_all_pages()
    print(f"📋 数据库中共有 {len(pages)} 条记录")

    if not pages:
        print("✅ 数据库已为空，无需清空。")
        exit(0)

    confirm = input(f"⚠️  确认将 {len(pages)} 条记录全部归档？(y/N): ").strip().lower()
    if confirm != "y":
        print("❌ 已取消。")
        exit(0)

    success = fail = 0
    for i, page in enumerate(pages):
        title = "无标题"
        try:
            props = page.get("properties", {})
            title_prop = props.get("政策名称", {})
            title_items = title_prop.get("title", [])
            if title_items:
                title = title_items[0].get("plain_text", "无标题")
        except Exception:
            pass

        res = archive_page(page["id"])
        if res.status_code == 200:
            success += 1
            print(f"  🗑️  [{i+1}/{len(pages)}] 已归档: {title[:40]}")
        else:
            fail += 1
            print(f"  ❌ [{i+1}/{len(pages)}] 归档失败 ({res.status_code}): {title[:40]}")

    print(f"\n✅ 完成：{success} 条已归档, {fail} 条失败")
    if fail == 0:
        print("🎉 Notion 数据库已清空，可以开始全新测试。")
