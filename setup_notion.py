"""一次性脚本：在指定的 Notion 父页面下自动创建四个数据库，并打印它们的 ID。

用法：
  1. 在 Notion 新建一个空白页面（如「MOOD」），把你的 integration 连接到它
     （页面右上 ... -> 连接 -> 选择你的 integration）。
  2. 复制该页面的 ID（页面链接里末尾那串 32 位十六进制）。
  3. 运行：
        NOTION_TOKEN=secret_xxx python setup_notion.py <父页面ID>
  4. 把打印出来的四个 DB ID 填进 config.yaml。
"""
from __future__ import annotations

import os
import sys

from notion_client import Client


def create_db(client, parent_id, title, properties):
    db = client.databases.create(
        parent={"type": "page_id", "page_id": parent_id},
        title=[{"type": "text", "text": {"content": title}}],
        properties=properties,
    )
    print(f"  {title:8s} DB ID = {db['id']}")
    return db["id"]


def main():
    if len(sys.argv) < 2:
        print("用法: python setup_notion.py <父页面ID>")
        sys.exit(1)
    parent_id = sys.argv[1]
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("请先设置环境变量 NOTION_TOKEN")
        sys.exit(1)

    client = Client(auth=token)
    print("正在创建数据库...")

    # Entities 先建，Entries 的 relation 要指向它
    entities = create_db(client, parent_id, "Entities", {
        "名称": {"title": {}},
        "类型": {"select": {"options": [
            {"name": "人物"}, {"name": "事物"}, {"name": "地点"}, {"name": "其他"},
        ]}},
    })

    create_db(client, parent_id, "Entries", {
        "标题": {"title": {}},
        "日期": {"date": {}},
        "情绪": {"multi_select": {}},
        "求回应": {"checkbox": {}},
        "已识别": {"checkbox": {}},
        "已回应": {"checkbox": {}},
        "实体": {"relation": {"database_id": entities, "single_property": {}}},
    })

    create_db(client, parent_id, "Letters", {
        "标题": {"title": {}},
        "日期": {"date": {}},
    })

    create_db(client, parent_id, "Rules", {
        "名称": {"title": {}},
        "启用": {"checkbox": {}},
        "触发条件": {"rich_text": {}},
        "提醒话术": {"rich_text": {}},
    })

    print("\n完成。把上面四个 DB ID 填入 config.yaml 对应字段。")


if __name__ == "__main__":
    main()
