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
        self.notify_user_id = cfg.get("notify_user_id")  # 可选：手动指定要@提醒的人
        self._ds_cache: dict[str, str] = {}  # database_id -> data_source_id
        self._person_id_cache: str | None = None  # 自动识别的真人 user id（""=识别失败）

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

    def entries_for_daily(self, lookback_hours: int = 24) -> list[dict]:
        """取最近 lookback_hours 小时内创建的记录，供日总结使用。

        为什么用"滚动 24h 窗口"而不是"自然日 00:00 起"：日总结固定每晚 22:00 触发。
        若按自然日切，22:00~次日 00:00 写的记录既赶不上当晚总结（那时它还没被创建），
        又不属于次日"00:00 起"的窗口，会两头落空、永久漏掉。改成"过去 24h"后，
        每晚 22:00 的总结覆盖 [前一晚 22:00, 今晚 22:00)，相邻两天首尾相接，不漏不重。
        """
        return self._entries_since_hours(lookback_hours)

    def entries_for_weekly(self, lookback_hours: int = 168) -> list[dict]:
        """取最近 lookback_hours 小时（默认 7×24）内创建的记录，供周回顾使用。

        同样用滚动窗口而非自然周，理由与 entries_for_daily 一致：触发时刻固定，
        滚动窗口能让相邻两周首尾相接，不漏不重。一周的记录条数可能超过单页上限，
        所以这里走分页版查询，避免记得多的那周被静默截断。
        """
        return self._entries_since_hours(lookback_hours, paginate=True)

    def entries_for_monthly(self, tz_offset_hours: int = 8) -> list[dict]:
        """取「北京时间本月 1 号 00:00 起」到现在的所有记录，供月总结使用。

        这里刻意用自然月而不是滚动 30 天：月总结的语义就是"这个月"，
        跨月边界的滚动窗口会把上个月的尾巴混进来，而且各月天数不同（28~31），
        滚动窗口会让 2 月多算、大月少算。按自然月切，每月首尾正好接上。
        条数按一个月算可能不少，所以走分页版查询，避免写得多的月份被静默截断。
        """
        from datetime import datetime, timedelta, timezone
        tz = timezone(timedelta(hours=tz_offset_hours))
        month_start = datetime.now(tz).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        # Notion 按 UTC 比较，带时区的 isoformat 会被正确换算
        return self._query_all(self.entries_db, {
            "timestamp": "created_time",
            "created_time": {"on_or_after": month_start.isoformat()},
        })

    def _entries_since_hours(self, hours: int, paginate: bool = False) -> list[dict]:
        """按「创建时间在最近 hours 小时内」查 Entries，日总结与周回顾共用。"""
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        filt = {"timestamp": "created_time", "created_time": {"on_or_after": since}}
        return self._query_all(self.entries_db, filt) if paginate \
            else self._query(self.entries_db, filt)

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

    def _query_all(self, db_id: str, filt: dict | None) -> list[dict]:
        """同 _query，但翻完所有页。给「窗口较长、条数可能超过单页」的查询用。"""
        ds_id = self._data_source_id(db_id)
        out: list[dict] = []
        cursor: str | None = None
        while True:
            kwargs = {"data_source_id": ds_id, "page_size": _PAGE_SIZE}
            if filt:
                kwargs["filter"] = filt
            if cursor:
                kwargs["start_cursor"] = cursor
            res = self.client.data_sources.query(**kwargs)
            out.extend(res.get("results", []))
            if not res.get("has_more"):
                return out
            cursor = res.get("next_cursor")

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

    @staticmethod
    def prop_multi_select(page: dict, name: str) -> list[str]:
        """读取 multi_select 属性的所有选项名，如「情绪」里的标签列表。"""
        p = page.get("properties", {}).get(name, {})
        return [opt.get("name", "") for opt in p.get("multi_select", []) if opt.get("name")]

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

    # ── 来信 / 日总结：新建一页 ───────────────────────────
    def _create_letter_page(self, title: str, children: list[dict],
                            icon: str | None = None) -> str:
        from datetime import datetime, timezone
        kwargs = {
            "parent": {"type": "data_source_id",
                       "data_source_id": self._data_source_id(self.letters_db)},
            "properties": {
                "Name": {"title": [{"text": {"content": title}}]},
                "日期": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
            },
            "children": children,
        }
        if icon:
            kwargs["icon"] = {"type": "emoji", "emoji": icon}
        page = self.client.pages.create(**kwargs)
        return page["id"]

    def create_letter(self, title: str, body: str) -> str:
        """来信渲染成信笺样式：日期小抬头 + 分隔线 + 逐段落正文。内容仍是自然段落的信。"""
        from datetime import datetime, timedelta, timezone
        local = datetime.now(timezone(timedelta(hours=8)))
        week = "一二三四五六日"[local.weekday()]
        date_line = local.strftime(f"%Y 年 %m 月 %d 日 · 周{week}")
        blocks: list[dict] = [
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [
                 {"type": "text", "text": {"content": date_line},
                  "annotations": {"italic": True, "color": "gray"}}]}},
            _divider(),
        ]
        # 按空行切成一段段，让信读起来有段落的呼吸感（内容不变）
        paras = [p for p in body.split("\n") if p.strip()]
        for para in (paras or [body]):
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _chunk_rich_text(para)}})
        return self._create_letter_page(title, blocks, icon="💌")

    def create_daily(self, title: str, sections: dict, mood: str = "") -> str:
        """把日总结渲染成结构化卡片：心情底色 + 正文 + 亮点 + 小提醒 + 结尾寄语。
        sections 含 body/praise/suggestion/closing，缺项自动跳过。"""
        blocks: list[dict] = []
        if mood:
            blocks.append(_callout(f"心情底色  {mood}", "🌙", "gray_background"))
        # 正文按空行拆成多段，读起来有呼吸感
        for para in [p for p in sections.get("body", "").split("\n") if p.strip()]:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _chunk_rich_text(para)}})
        if sections.get("praise"):
            blocks.append(_divider())
            blocks.append(_callout(sections["praise"], "✨", "green_background",
                                   label="今天你做得好的"))
        if sections.get("suggestion"):
            blocks.append(_callout(sections["suggestion"], "🌱", "blue_background",
                                   label="也许可以更好的"))
        if sections.get("closing"):
            blocks.append(_divider())
            blocks.append({"object": "block", "type": "quote",
                           "quote": {"rich_text": _chunk_rich_text("💭 " + sections["closing"])}})
        if not blocks:  # 极端兜底：什么都没解析出来也别建空页
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _chunk_rich_text(sections.get("body", ""))}})
        return self._create_letter_page(title, blocks, icon="🌙")

    def create_weekly(self, title: str, sections: dict, stats: str = "") -> str:
        """把周回顾渲染成复盘卡：本周概览（代码统计的硬数据）+ 情绪走势正文
        + 反复出现 + 本周亮点 + 下周留意。sections 含 body/pattern/praise/focus，缺项跳过。"""
        blocks: list[dict] = []
        if stats:
            blocks.append(_callout(stats, "📊", "gray_background", label="本周概览"))
        for para in [p for p in sections.get("body", "").split("\n") if p.strip()]:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _chunk_rich_text(para)}})
        if sections.get("pattern"):
            blocks.append(_divider())
            blocks.append(_callout(sections["pattern"], "🔁", "yellow_background",
                                   label="这周反复出现的"))
        if sections.get("praise"):
            blocks.append(_callout(sections["praise"], "✨", "green_background",
                                   label="本周你做成的"))
        if sections.get("focus"):
            blocks.append(_divider())
            blocks.append(_callout(sections["focus"], "🎯", "blue_background",
                                   label="下周想留意的一件事"))
        if not blocks:  # 极端兜底：什么都没解析出来也别建空页
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _chunk_rich_text(sections.get("body", ""))}})
        return self._create_letter_page(title, blocks, icon="📅")

    def create_monthly(self, title: str, sections: dict, stats: str = "") -> str:
        """把月总结渲染成月报：本月概览（代码统计的硬数据）+ 主线叙事 + 本月主题
        + 变化 + 值得记住 + 下个月。sections 含 body/theme/change/memorable/next，缺项跳过。"""
        blocks: list[dict] = []
        if stats:
            blocks.append(_callout(stats, "📊", "gray_background", label="本月概览"))
        if sections.get("theme"):
            # 主题放在正文之前当"题眼"，一眼看到这个月是关于什么的
            blocks.append(_callout(sections["theme"], "🧭", "purple_background",
                                   label="本月主题"))
        for para in [p for p in sections.get("body", "").split("\n") if p.strip()]:
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _chunk_rich_text(para)}})
        if sections.get("change"):
            blocks.append(_divider())
            blocks.append(_callout(sections["change"], "📈", "orange_background",
                                   label="和月初比，变了什么"))
        if sections.get("memorable"):
            blocks.append(_callout(sections["memorable"], "🌟", "green_background",
                                   label="值得记住的时刻"))
        if sections.get("next"):
            blocks.append(_divider())
            blocks.append(_callout(sections["next"], "🌱", "blue_background",
                                   label="下个月的方向"))
        if not blocks:  # 极端兜底：什么都没解析出来也别建空页
            blocks.append({"object": "block", "type": "paragraph",
                           "paragraph": {"rich_text": _chunk_rich_text(sections.get("body", ""))}})
        return self._create_letter_page(title, blocks, icon="🗓️")

    # ── 通知：在页面发一条 @提及的评论，触发 Notion 收件箱提醒 ──
    def _resolve_person_id(self) -> str:
        """确定要@提醒谁：优先配置里的 notify_user_id；否则自动挑工作区里唯一的真人。
        识别不到（多个真人 / 无权限）返回 ""，调用方据此跳过提醒。结果缓存，避免重复请求。"""
        if self.notify_user_id:
            return self.notify_user_id
        if self._person_id_cache is not None:
            return self._person_id_cache
        try:
            persons = []
            cursor = None
            while True:
                res = self.client.users.list(start_cursor=cursor) if cursor \
                    else self.client.users.list()
                for u in res.get("results", []):
                    if u.get("type") == "person":  # 排除集成机器人(bot)
                        persons.append(u["id"])
                if not res.get("has_more"):
                    break
                cursor = res.get("next_cursor")
            self._person_id_cache = persons[0] if len(persons) == 1 else ""
        except Exception:  # noqa: BLE001 权限不足等，静默降级为不提醒
            self._person_id_cache = ""
        return self._person_id_cache

    def notify_on_page(self, page_id: str, text: str) -> bool:
        """在指定页面发一条 @提及的评论。返回是否成功发出（含提及）。"""
        uid = self._resolve_person_id()
        rich: list[dict] = []
        if uid:
            rich.append({"type": "mention", "mention": {"user": {"id": uid}}})
            rich.append({"type": "text", "text": {"content": " " + text}})
        else:
            # 识别不到人也照发评论（至少页面上有痕迹），只是不@、可能不推送
            rich.append({"type": "text", "text": {"content": text}})
        self.client.comments.create(
            parent={"page_id": page_id},
            rich_text=rich,
        )
        return bool(uid)


def _chunk_rich_text(text: str) -> list[dict]:
    """Notion 单个 rich_text 内容上限 2000 字符，超长要切块。"""
    limit = 1900
    chunks = [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _callout(text: str, emoji: str, color: str, label: str = "") -> dict:
    """一个带图标与底色的 callout。label 非空时作为加粗小标题起头、正文另起一行。"""
    rich: list[dict] = []
    if label:
        rich.append({"type": "text", "text": {"content": label + "\n"},
                     "annotations": {"bold": True}})
    rich.extend(_chunk_rich_text(text))
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": rich, "icon": {"type": "emoji", "emoji": emoji},
                        "color": color}}
