"""把 MOOD 需要的属性 schema 同步到现有的四个数据库（幂等，可重复运行）。

为什么需要它：Notion 新版 API（2025-09-03）下，建库时的属性要 PATCH 到 data source 上。
本脚本读取 config.yaml 里的四个 DB ID，确保每个库都有约定的属性，缺啥补啥，不删不改已有数据。

用法：
  NOTION_TOKEN=xxx python sync_schema.py
"""
from __future__ import annotations

from notion_client import Client

from mood.config import load_config

# 每个数据库期望的属性（title 属性已存在，名为 Name，不动它）
SCHEMAS = {
    "entries_db": {
        "情绪": {"multi_select": {}},
        "求回应": {"checkbox": {}},
        "已识别": {"checkbox": {}},
        "已回应": {"checkbox": {}},
        "已查规则": {"checkbox": {}},
        "实体": {"relation": {
            "data_source_id": "__ENTITIES_DS__",   # 运行时替换为实体库的 data source id
            "type": "single_property",
            "single_property": {},
        }},
    },
    "entities_db": {
        "类型": {"select": {"options": [
            {"name": "人物"}, {"name": "事物"}, {"name": "地点"}, {"name": "其他"},
        ]}},
    },
    "letters_db": {
        "日期": {"date": {}},
    },
    "rules_db": {
        "启用": {"checkbox": {}},
        "触发条件": {"rich_text": {}},
        "提醒话术": {"rich_text": {}},
    },
}


def data_source_id(client, db_id):
    db = client.databases.retrieve(database_id=db_id)
    return db["data_sources"][0]["id"]


def main():
    cfg = load_config("config.yaml")["notion"]
    client = Client(auth=cfg["token"])

    # 先拿到实体库的 data source id，供 Entries 的「实体」relation 指向
    entities_ds = data_source_id(client, cfg["entities_db"])

    import copy
    schemas = copy.deepcopy(SCHEMAS)
    rel = schemas["entries_db"]["实体"]["relation"]
    rel["data_source_id"] = entities_ds

    for key, props in schemas.items():
        db_id = cfg[key]
        ds_id = data_source_id(client, db_id)
        existing = client.data_sources.retrieve(data_source_id=ds_id).get("properties", {})
        # 只补缺失的，避免覆盖
        to_add = {name: spec for name, spec in props.items() if name not in existing}
        if not to_add:
            print(f"[{key}] 已齐全，跳过")
            continue
        client.data_sources.update(data_source_id=ds_id, properties=to_add)
        print(f"[{key}] 已补充属性: {list(to_add)}")

    print("\n完成。schema 同步好了。")


if __name__ == "__main__":
    main()
