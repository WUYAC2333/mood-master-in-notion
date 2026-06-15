"""诊断脚本：打印某数据库的 data source ID 及其真实属性名，用于排查字段不匹配。

用法：
  NOTION_TOKEN=xxx python inspect_db.py
"""
from __future__ import annotations

import os

from notion_client import Client

# Entries 数据库
ENTRIES_DB = "766d7644-86a4-4e1c-91e5-b5efe205d257"


def main():
    client = Client(auth=os.environ["NOTION_TOKEN"])
    db = client.databases.retrieve(database_id=ENTRIES_DB)
    print("database title:", "".join(t.get("plain_text", "") for t in db.get("title", [])))
    sources = db.get("data_sources", [])
    print("data_sources:", sources)

    for s in sources:
        ds = client.data_sources.retrieve(data_source_id=s["id"])
        print(f"\n=== data source {s['id']} ===")
        props = ds.get("properties", {})
        for name, meta in props.items():
            print(f"  属性名={name!r}  类型={meta.get('type')}")


if __name__ == "__main__":
    main()
