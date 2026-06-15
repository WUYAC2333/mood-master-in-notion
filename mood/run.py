"""MOOD 主流程。每次运行做四件事（可用 --task 单独触发）：

  classify  —— 给未识别的记录打情绪标签 + 解析 @实体
  respond   —— 给"求回应且未回应"的记录生成长文回应
  rules     —— 对新识别的记录跑所有启用的规则，命中则追加提醒
  letter    —— 生成一封来信（通常按更低频率单独调度）

用法：
  python -m mood.run                # 跑 classify+respond+rules（日常轮询）
  python -m mood.run --task letter  # 单独生成来信
"""
from __future__ import annotations

import argparse

from .classify import classify
from .config import load_config
from .entities import extract_mentions
from .generate import apply_rule, daily_summary, respond, write_letter
from .llm import llm_from_cfg
from .notion import Notion


def _entry_text(nz: Notion, page: dict) -> str:
    title = nz.prop_title(page, "Name")
    body = nz.page_body_text(page["id"])
    return f"{title}\n{body}".strip() if title else body


def _build_memory(nz: Notion, exclude_id: str, max_entries: int, max_chars: int) -> str:
    """取最近的若干条记录（不含当前这条）拼成记忆背景，并对总长度封顶。
    这样无论历史多长，每次喂给模型的记忆都是有界的，避免成本与超长问题。"""
    pages = nz.latest_entries(max_entries + 1)  # 多取一条，方便排除当前
    blobs = []
    used = 0
    for p in pages:
        if p["id"] == exclude_id:
            continue
        t = _entry_text(nz, p)
        if not t:
            continue
        # 给每条记录加上日期，帮助模型建立时间感
        when = p.get("created_time", "")[:10]
        chunk = f"[{when}] {t}"
        if used + len(chunk) > max_chars:
            break
        blobs.append(chunk)
        used += len(chunk)
        if len(blobs) >= max_entries:
            break
    return "\n\n---\n\n".join(blobs)


def task_classify(nz: Notion, classifier, entities_on: bool) -> int:
    pages = nz.entries_need_classification()
    for page in pages:
        # 自动标题只看正文，避免把用户标题喂回去
        body = nz.page_body_text(page["id"])
        if not body:
            nz.set_emotions(page["id"], [])  # 空记录也标记已识别，避免反复扫
            continue
        emotions, title = classify(classifier, body)
        nz.set_emotions(page["id"], emotions)
        if title:
            nz.set_title_if_empty(page, title)
        if entities_on:
            names = extract_mentions(body)
            if names:
                ids = [nz.find_or_create_entity(n) for n in names]
                nz.link_entities(page["id"], ids)
        print(f"[classify] {page['id'][:8]} -> {emotions} title={title!r} mentions={extract_mentions(body)}")
    return len(pages)


def task_respond(nz: Notion, responder, mem_entries: int, mem_chars: int) -> int:
    pages = nz.entries_need_response()
    for page in pages:
        text = _entry_text(nz, page)
        if not text:
            nz.mark_responded(page["id"])
            continue
        memory = _build_memory(nz, page["id"], mem_entries, mem_chars)
        reply = respond(responder, text, memory=memory)
        nz.append_callout(page["id"], reply, emoji="💬")
        nz.mark_responded(page["id"])
        print(f"[respond] {page['id'][:8]} ok ({len(reply)} chars, memory={len(memory)} chars)")
    return len(pages)


def task_rules(nz: Notion, responder) -> int:
    rules = nz.active_rules()
    if not rules:
        return 0
    # 只对"还没查过规则"的记录跑一遍，查完打勾，避免每次轮询重复触发
    pages = nz.entries_need_rule_check()
    hits = 0
    for page in pages:
        text = _entry_text(nz, page)
        if text:
            for rule in rules:
                name = nz.prop_title(rule, "Name")
                cond = nz.prop_text(rule, "触发条件")
                phrasing = nz.prop_text(rule, "提醒话术")
                if not cond:
                    continue
                msg = apply_rule(responder, text, cond, phrasing)
                if msg:
                    nz.append_callout(page["id"], f"【{name}】{msg}", emoji="🔔")
                    hits += 1
                    print(f"[rule] {name} hit on {page['id'][:8]}")
        nz.mark_rule_checked(page["id"])
    return hits


def task_letter(nz: Notion, responder, lookback_days: int) -> bool:
    pages = nz.recent_entries(lookback_days)
    if not pages:
        print("[letter] 近期没有记录，跳过")
        return False
    blobs = []
    for p in pages:
        t = _entry_text(nz, p)
        if t:
            blobs.append(t)
    if not blobs:
        return False
    joined = "\n\n---\n\n".join(blobs)
    title, body = write_letter(responder, joined)
    nz.create_letter(title, body)
    print(f"[letter] 已生成《{title}》，综合了 {len(blobs)} 条记录")
    return True


def task_daily(nz: Notion, responder) -> bool:
    pages = nz.entries_today()
    blobs = [t for p in pages if (t := _entry_text(nz, p))]
    if not blobs:
        print("[daily] 今天没有记录，跳过")
        return False
    joined = "\n\n---\n\n".join(blobs)
    title, body = daily_summary(responder, joined)
    nz.create_letter(title, body)   # 复用 Letters 库存放日总结
    print(f"[daily] 已生成《{title}》，综合了 {len(blobs)} 条今日记录")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=["all", "classify", "respond", "rules", "letter", "daily"],
                    default="all")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    nz = Notion(cfg["notion"])
    classifier = llm_from_cfg(cfg["classifier"])
    responder = llm_from_cfg(cfg["responder"])
    entities_on = cfg.get("entities", {}).get("enabled", True)
    mem = cfg.get("memory", {})
    mem_entries = mem.get("max_entries", 20)
    mem_chars = mem.get("max_chars", 6000)

    if args.task in ("all", "classify"):
        task_classify(nz, classifier, entities_on)
    if args.task in ("all", "respond"):
        task_respond(nz, responder, mem_entries, mem_chars)
    if args.task in ("all", "rules"):
        task_rules(nz, responder)
    if args.task == "letter":
        task_letter(nz, responder, cfg.get("letter", {}).get("lookback_days", 7))
    if args.task == "daily":
        task_daily(nz, responder)


if __name__ == "__main__":
    main()
