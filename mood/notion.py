"""Notion 访问层。

数据模型约定（字段名为中文，方便在 Notion 界面直接看）：

Entries（记录）数据库属性：
  - 标题        title       —— 一句话标题，可留空
  - 日期        date        —— 记录时间
  - 情绪        multi_select—— AI 识别填入
  - 求回应      checkbox    —— 你勾选 = 想要长文回应
  - 已识别      checkbox    —— 脚本标记，避免重复识别情绪
  - 已回应      checkbox    —— 脚本标记，避免重复回应
  - 实体        relation -> Entities
  正文（心里话）写在页面 body 里；AI 回应作为 callout 追加到 body。

Entities（实体）数据库属性：
  - 名称        title
  - 类型        select      —— 人物 / 事物 ...（可选）

Letters（来信）数据库属性：
  - 标题        title
  - 日期        date

Rules（规则 / agent）数据库属性：
  - 名称        title
  - 启用        checkbox
  - 触发条件    rich_text   —— 自然语言，如"把感受当事实陈述时"
  - 提醒话术    rich_text   —— 命中后如何提醒（给模型的指引，可空）
"""
from __future__ import annotations

from notion_client import Client

# 一次最多取多少条，足够个人使用
_PAGE_SIZE = 50


class Notion:
    def __init__(self, cfg: dict):
        self.client = Client(auth=cfg["token"])
        self.entries_db = cfg["entries_db"]
        self.entities_db = cfg["entities_db"]
        self.letters_db = cfg.get("letters_db")
        self.rules_db = cfg.get("rules_db")
        self._ds_cache: dict[str, str] = {}  # database_id -> data_source_id

    def _data_source_id(self, database_id: str) -> str:
        """API 2025-09-03：查询/建页都针对数据库下的 data source，而非数据库本身。"""
        if database_id not in self._ds_cache:
            db = self.client.databases.retrieve(database_id=database_id)
            sources = db.get("data_sources", [])
            if not sources:
                raise RuntimeError(f"数据库 {database_id} 下没有 data source")
            self._ds_cache[database_id] = sources[0]["id"]
        return self._ds_cache[database_id]

    # ── 查询 ──────────────────────────────────────────────
    def entries_need_classification(self) -> list[dict]:
        return self._query(self.entries_db, {
            "property": "已识别", "checkbox": {"equals": False},
        })

    def entries_need_response(self) -> list[dict]:
        return self._query(self.entries_db, {
            "and": [
                {"property": "求回应", "checkbox": {"equals": True}},
                {"property": "已回应", "checkbox": {"equals": False}},
            ]
        })

    def recent_entries(self, days: int) -> list[dict]:
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        # 用 Notion 内置的"创建时间"，无需手动填日期
        return self._query(self.entries_db, {
            "timestamp": "created_time", "created_time": {"on_or_after": since},
        })

    def latest_entries(self, limit: int) -> list[dict]:
        """按创建时间倒序取最近 limit 条，供"带记忆的回应"使用。"""
        ds_id = self._data_source_id(self.entries_db)
        res = self.client.data_sources.query(
            data_source_id=ds_id,
            page_size=limit,
            sorts=[{"timestamp": "created_time", "direction": "descending"}],
        )
        return res.get("results", [])

    def entries_today(self, tz_offset_hours: int = 8) -> list[dict]:
        """取"今天"（默认北京时间）创建的记录，供日总结使用。"""
        from datetime import datetime, timedelta, timezone
        tz = timezone(timedelta(hours=tz_offset_hours))
        now_local = datetime.now(tz)
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        return self._query(self.entries_db, {
            "timestamp": "created_time",
            "created_time": {"on_or_after": start_local.astimezone(timezone.utc).isoformat()},
        })

    def active_rules(self) -> list[dict]:
        if not self.rules_db:
            return []
        return self._query(self.rules_db, {
            "property": "启用", "checkbox": {"equals": True},
        })

    def entries_need_rule_check(self) -> list[dict]:
        return self._query(self.entries_db, {
            "property": "已查规则", "checkbox": {"equals": False},
        })

    def _query(self, db_id: str, filt: dict | None) -> list[dict]:
        ds_id = self._data_source_id(db_id)
        kwargs = {"data_source_id": ds_id, "page_size": _PAGE_SIZE}
        if filt:
            kwargs["filter"] = filt
        return self.client.data_sources.query(**kwargs).get("results", [])

    # ── 读取页面内容 ──────────────────────────────────────
    def page_body_text(self, page_id: str) -> str:
        """把页面正文里的段落/文本块拼成纯文本，供模型阅读。"""
        blocks = self.client.blocks.children.list(block_id=page_id, page_size=100)
        parts: list[str] = []
        for b in blocks.get("results", []):
            t = b.get("type")
            data = b.get(t, {}) if t else {}
            rich = data.get("rich_text")
            if rich:
                parts.append("".join(r.get("plain_text", "") for r in rich))
        return "\n".join(parts).strip()

    @staticmethod
    def prop_title(page: dict, name: str) -> str:
        p = page.get("properties", {}).get(name, {})
        return "".join(r.get("plain_text", "") for r in p.get("title", [])).strip()

    @staticmethod
    def prop_text(page: dict, name: str) -> str:
        p = page.get("properties", {}).get(name, {})
        return "".join(r.get("plain_text", "") for r in p.get("rich_text", [])).strip()

    # ── 写回 ──────────────────────────────────────────────
    def set_emotions(self, page_id: str, emotions: list[str]) -> None:
        self.client.pages.update(page_id=page_id, properties={
            "情绪": {"multi_select": [{"name": e} for e in emotions]},
            "已识别": {"checkbox": True},
        })

    def set_title_if_empty(self, page: dict, title: str) -> None:
        """仅当标题为空时写入自动生成的标题，不覆盖用户手写的。
        标题用数据库自带的 title 属性 Name。"""
        if self.prop_title(page, "Name"):
            return
        self.client.pages.update(page_id=page["id"], properties={
            "Name": {"title": [{"text": {"content": title[:80]}}]},
        })

    def mark_responded(self, page_id: str) -> None:
        self.client.pages.update(page_id=page_id, properties={
            "已回应": {"checkbox": True},
        })

    def mark_rule_checked(self, page_id: str) -> None:
        self.client.pages.update(page_id=page_id, properties={
            "已查规则": {"checkbox": True},
        })

    def link_entities(self, page_id: str, entity_page_ids: list[str]) -> None:
        self.client.pages.update(page_id=page_id, properties={
            "实体": {"relation": [{"id": pid} for pid in entity_page_ids]},
        })

    def append_callout(self, page_id: str, text: str, emoji: str = "💬") -> None:
        """把 AI 回应作为 callout 块追加到记录页正文，读起来像一封回信。"""
        self.client.blocks.children.append(block_id=page_id, children=[{
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": _chunk_rich_text(text),
                "icon": {"type": "emoji", "emoji": emoji},
            },
        }])

    # ── 实体：按名查找，没有就创建 ────────────────────────
    def find_or_create_entity(self, name: str, kind: str | None = None) -> str:
        hits = self._query(self.entities_db, {
            "property": "Name", "title": {"equals": name},
        })
        if hits:
            return hits[0]["id"]
        props = {"Name": {"title": [{"text": {"content": name}}]}}
        if kind:
            props["类型"] = {"select": {"name": kind}}
        page = self.client.pages.create(
            parent={"type": "data_source_id",
                    "data_source_id": self._data_source_id(self.entities_db)},
            properties=props,
        )
        return page["id"]

    # ── 来信：新建一页 ────────────────────────────────────
    def create_letter(self, title: str, body: str) -> str:
        from datetime import datetime, timezone
        page = self.client.pages.create(
            parent={"type": "data_source_id",
                    "data_source_id": self._data_source_id(self.letters_db)},
            properties={
                "Name": {"title": [{"text": {"content": title}}]},
                "日期": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
            },
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": _chunk_rich_text(body)},
            }],
        )
        return page["id"]


def _chunk_rich_text(text: str) -> list[dict]:
    """Notion 单个 rich_text 内容上限 2000 字符，超长要切块。"""
    limit = 1900
    chunks = [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
    return [{"type": "text", "text": {"content": c}} for c in chunks]
