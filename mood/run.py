"""MOOD 主流程。每次运行做四件事（可用 --task 单独触发）：

  classify  —— 给未识别的记录打情绪标签 + 解析 @实体
  respond   —— 给"求回应且未回应"的记录生成长文回应
  rules     —— 对新识别的记录跑所有启用的规则，命中则追加提醒
  letter    —— 生成一封来信（通常按更低频率单独调度）

另有三个按固定时刻单独调度的任务，粒度由细到粗：
  daily     —— 当天的日总结（每晚 22:00）
  weekly    —— 一周的理性复盘卡：走势、反复出现的模式、亮点、下周留意（每周日 21:00）
  monthly   —— 一个月的主线与变化：主题、变化、值得记住、下个月（月末那天 20:00）

用法：
  python -m mood.run                        # 跑 classify+respond+rules（日常轮询）
  python -m mood.run --task letter          # 单独生成来信
  python -m mood.run --task weekly          # 单独生成周回顾
  python -m mood.run --task monthly --force # 立即生成月总结（不等月末）
"""
from __future__ import annotations

import argparse
import sys

from .classify import classify
from .config import load_config
from .bubbles import export_bubble_data
from .entities import extract_mentions
from .generate import (apply_rule, daily_summary, monthly_summary, respond,
                       weekly_summary, write_letter)
from .llm import llm_from_cfg, responder_from_cfg, FALLBACK_REPLY
from .notion import Notion


def _entry_text(nz: Notion, page: dict) -> str:
    title = nz.prop_title(page, "Name")
    body = nz.page_body_text(page["id"])
    return f"{title}\n{body}".strip() if title else body


def _to_local(iso_utc: str, tz_offset_hours: int = 8):
    """把 Notion 的 UTC 时间串解析成北京时间的 datetime；空串或解析失败返回 None。"""
    if not iso_utc:
        return None
    from datetime import datetime, timedelta, timezone
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone(timedelta(hours=tz_offset_hours)))


def _fmt_local(iso_utc: str, tz_offset_hours: int = 8) -> str:
    """把 Notion 的 UTC created_time 格式化成北京时间，带星期与时刻。
    例：'2026-07-08 周二 03:15'。模型据此才能说出『昨天』『上周六』『凌晨3点』。"""
    local = _to_local(iso_utc, tz_offset_hours)
    if local is None:
        return iso_utc[:10] if iso_utc else ""
    week = "一二三四五六日"[local.weekday()]
    return local.strftime(f"%Y-%m-%d 周{week} %H:%M")


def _now_hint() -> str:
    """给模型的『当前时间』锚点，让它能把记录时间换算成相对说法。"""
    from datetime import datetime, timezone
    return _fmt_local(datetime.now(timezone.utc).isoformat())


def _entry_block(nz: Notion, page: dict) -> str:
    """把一条记录格式化成『时间戳 + 正文』，供日总结/来信使用。"""
    text = _entry_text(nz, page)
    if not text:
        return ""
    when = _fmt_local(page.get("created_time", ""))
    return f"【{when}】\n{text}" if when else text


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
        # 给每条记录加上完整时间戳（北京时间+星期+时刻），帮助模型建立时间感
        when = _fmt_local(p.get("created_time", ""))
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
        try:
            # 自动标题只看正文，避免把用户标题喂回去
            body = nz.page_body_text(page["id"])
            if not body:
                nz.set_emotions(page["id"], [])  # 空记录也标记已识别，避免反复扫
                continue
            emotions, title = classify(classifier, body)
            # 正文非空却一个情绪都没识别出来，几乎都是模型抖动/返回异常（而非真的无情绪）。
            # 这种情况不要打勾——留到下次轮询用恢复后的接口重试，避免临时故障被永久锁死。
            if not emotions:
                print(f"[classify] {page['id'][:8]} 情绪为空，疑似接口异常，本次不打勾，下次重试")
                continue
            nz.set_emotions(page["id"], emotions)
            if title:
                nz.set_title_if_empty(page, title)
            if entities_on:
                names = extract_mentions(body)
                if names:
                    ids = [nz.find_or_create_entity(n) for n in names]
                    nz.link_entities(page["id"], ids)
            print(f"[classify] {page['id'][:8]} -> {emotions} title={title!r} mentions={extract_mentions(body)}")
        except Exception as e:  # noqa: BLE001
            # 单条失败不影响其它记录；本条不打勾，下次轮询会重试
            print(f"[classify] {page['id'][:8]} 失败，跳过：{e}")
    return len(pages)


def task_respond(nz: Notion, responder, mem_entries: int, mem_chars: int) -> int:
    pages = nz.entries_need_response()
    for page in pages:
        try:
            text = _entry_text(nz, page)
            if not text:
                nz.mark_responded(page["id"])
                continue
            memory = _build_memory(nz, page["id"], mem_entries, mem_chars)
            reply = respond(responder, text, memory=memory)
            # 所有模型都失败时 responder 会返回固定兜底话术。这种情况不要写进 Notion、
            # 也不要打“已回应”勾——否则临时故障会被当成正式回应永久锁死。留到下次重试。
            if reply == FALLBACK_REPLY:
                print(f"[respond] {page['id'][:8]} 所有模型失败，本次不回应不打勾，下次重试")
                continue
            nz.append_callout(page["id"], reply, emoji="💬")
            nz.mark_responded(page["id"])
            print(f"[respond] {page['id'][:8]} ok ({len(reply)} chars, memory={len(memory)} chars)")
        except Exception as e:  # noqa: BLE001
            print(f"[respond] {page['id'][:8]} 失败，跳过（下次重试）：{e}")
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


def task_letter(nz: Notion, responder, lookback_days: int) -> str:
    """返回状态：'ok' 已生成 / 'empty' 无记录可跳过 / 'failed' 模型全失败。"""
    pages = nz.recent_entries(lookback_days)
    if not pages:
        print("[letter] 近期没有记录，跳过")
        return "empty"
    blobs = [b for p in pages if (b := _entry_block(nz, p))]
    if not blobs:
        return "empty"
    joined = f"【当前时间】{_now_hint()}\n\n" + "\n\n---\n\n".join(blobs)
    title, body = write_letter(responder, joined)
    if body == FALLBACK_REPLY:
        print("[letter] 所有模型失败，不生成来信（避免写入兜底话术）")
        return "failed"
    page_id = nz.create_letter(title, body)
    _notify(nz, page_id, f"你有一封新来信《{title}》，来读读吧 💌")
    print(f"[letter] 已生成《{title}》，综合了 {len(blobs)} 条记录")
    return "ok"


def _notify(nz: Notion, page_id: str, text: str) -> None:
    """在来信/日总结页发一条 @提及评论触发通知。失败绝不影响主流程——
    信已经生成写入了，通知只是锦上添花，出错就打日志跳过。"""
    try:
        mentioned = nz.notify_on_page(page_id, text)
        if mentioned:
            print("[notify] 已发送 @提醒评论")
        else:
            print("[notify] 未识别到唯一真人用户，已发普通评论但未@（不会推送）。"
                  "可在 config 的 notify_user_id 手动指定。")
    except Exception as e:  # noqa: BLE001
        print(f"[notify] 发送提醒失败，已跳过（不影响来信本身）：{e}")


def task_daily(nz: Notion, responder) -> str:
    """返回状态：'ok' 已生成 / 'empty' 今天无记录 / 'failed' 模型全失败。"""
    pages = nz.entries_for_daily()
    blobs = [b for p in pages if (b := _entry_block(nz, p))]
    if not blobs:
        print("[daily] 最近一天没有记录，跳过")
        return "empty"
    joined = f"【当前时间】{_now_hint()}\n\n" + "\n\n---\n\n".join(blobs)
    title, sections = daily_summary(responder, joined)
    if sections.get("body") == FALLBACK_REPLY:
        print("[daily] 所有模型失败，不生成日总结（避免写入兜底话术）")
        return "failed"
    mood = _mood_snapshot(nz, pages)
    page_id = nz.create_daily(title, sections, mood)   # 复用 Letters 库存放日总结
    _notify(nz, page_id, f"今天的日总结《{title}》已经写好啦 🌙")
    print(f"[daily] 已生成《{title}》，综合了 {len(blobs)} 条今日记录")
    return "ok"


def task_weekly(nz: Notion, responder, lookback_days: int, max_chars: int) -> str:
    """周回顾：拉开距离看一整周的模式与趋势，与「来信」的情感陪伴分工。
    返回状态：'ok' 已生成 / 'empty' 本周无记录 / 'failed' 模型全失败。"""
    pages = nz.entries_for_weekly(lookback_days * 24)
    # 按时间正序排，让这一周在模型眼里是从头读到尾的
    pages.sort(key=lambda p: p.get("created_time", ""))
    # 正文只读一次，统计与拼接共用，避免对同一页重复请求 Notion
    pairs = [(p, t) for p in pages if (t := _entry_text(nz, p))]
    if not pairs:
        print("[weekly] 最近一周没有记录，跳过")
        return "empty"
    blobs = [_entry_block_from(p, t) for p, t in pairs]
    stats = _weekly_stats(nz, pairs)
    joined = (f"【当前时间】{_now_hint()}\n\n{stats}\n\n"
              + _join_capped(blobs, max_chars))
    date_range = _week_range([p for p, _ in pairs])  # 只按真正进了回顾的记录算区间
    title, sections = weekly_summary(responder, joined, date_range)
    if sections.get("body") == FALLBACK_REPLY:
        print("[weekly] 所有模型失败，不生成周回顾（避免写入兜底话术）")
        return "failed"
    page_id = nz.create_weekly(title, sections, stats)
    _notify(nz, page_id, f"这一周的周回顾《{title}》写好了，来复盘一下 📅")
    print(f"[weekly] 已生成《{title}》，综合了 {len(blobs)} 条记录")
    return "ok"


def _entry_block_from(page: dict, text: str) -> str:
    """同 _entry_block，但正文由调用方传入（已读过就不再请求一次 Notion）。"""
    when = _fmt_local(page.get("created_time", ""))
    return f"【{when}】\n{text}" if when else text


def _join_capped(blobs: list[str], max_chars: int, tag: str = "weekly") -> str:
    """拼接记录并对总长度封顶。超预算时从最早的开始丢，保住离现在最近的那几天，
    这样无论某周/某月写得多长，喂给模型的量与成本都是有界的。"""
    kept: list[str] = []
    used = 0
    for blob in reversed(blobs):  # 从最近往前收
        if used + len(blob) > max_chars and kept:
            print(f"[{tag}] 记录超出 {max_chars} 字预算，只取最近 {len(kept)}/{len(blobs)} 条")
            break
        kept.append(blob)
        used += len(blob)
    kept.reverse()
    return "\n\n---\n\n".join(kept)


def _week_range(pages: list[dict], tz_offset_hours: int = 8) -> str:
    """按记录的实际覆盖范围生成标题用的日期区间，如 '2026-07-20 ~ 07-26'。
    取不到时间就退回『到今天为止的 7 天』，保证标题永远有个说法。"""
    from datetime import datetime, timedelta, timezone
    days = [d for p in pages if (d := _to_local(p.get("created_time", ""), tz_offset_hours))]
    end = max(days) if days else datetime.now(timezone(timedelta(hours=tz_offset_hours)))
    start = min(days) if days else end - timedelta(days=6)
    return f"{start.strftime('%Y-%m-%d')} ~ {end.strftime('%m-%d')}"


def _is_month_end(tz_offset_hours: int = 8) -> bool:
    """今天（北京时间）是不是当月最后一天。

    标准 cron 无法表达"月末"，所以外部定时器设成每月 28-31 号都触发，
    由这里判断真正的月末——2 月不会漏、大小月也不会重复生成。
    """
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=tz_offset_hours)))
    return (today + timedelta(days=1)).month != today.month


def task_monthly(nz: Notion, responder, max_chars: int, force: bool = False) -> str:
    """月总结：拉到最高处看一个月的主线与变化。
    返回状态：'ok' 已生成 / 'empty' 本月无记录 / 'skipped' 今天不是月末 / 'failed' 模型全失败。"""
    if not force and not _is_month_end():
        print("[monthly] 今天不是当月最后一天，跳过（月末那天才生成）")
        return "skipped"
    pages = nz.entries_for_monthly()
    pages.sort(key=lambda p: p.get("created_time", ""))
    pairs = [(p, t) for p in pages if (t := _entry_text(nz, p))]
    if not pairs:
        print("[monthly] 本月没有记录，跳过")
        return "empty"
    blobs = [_entry_block_from(p, t) for p, t in pairs]
    stats = _monthly_stats(nz, pairs)
    joined = (f"【当前时间】{_now_hint()}\n\n{stats}\n\n"
              + _join_capped(blobs, max_chars, tag="monthly"))
    title, sections = monthly_summary(responder, joined, _month_label())
    if sections.get("body") == FALLBACK_REPLY:
        print("[monthly] 所有模型失败，不生成月总结（避免写入兜底话术）")
        return "failed"
    page_id = nz.create_monthly(title, sections, stats)
    _notify(nz, page_id, f"这个月的月总结《{title}》写好了，来回头看看这一个月 🗓️")
    print(f"[monthly] 已生成《{title}》，综合了 {len(blobs)} 条记录")
    return "ok"


def _month_label(tz_offset_hours: int = 8) -> str:
    """标题用的月份，如 '2026 年 7 月'。按北京时间算，避免 UTC runner 在月末跨月错月。"""
    from datetime import datetime, timedelta, timezone
    local = datetime.now(timezone(timedelta(hours=tz_offset_hours)))
    return f"{local.year} 年 {local.month} 月"


def _period_stats(nz: Notion, pairs: list[tuple[dict, str]], bucket_of,
                  labels: tuple[str, str, str], top_mentions: int = 6) -> str:
    """把一段时间的硬统计整理成几行文本，喂给模型也直接渲染进页面。
    全部复用已识别好的「情绪」标签和正文里的 @提及，不额外调模型，所以这部分零成本。

    bucket_of: 把一条记录的北京时间映射成分组键（周回顾按天分组、月总结按周分组）。
    labels:    三个小标题 —— (总数, 分组走势, 人与事)。
    """
    from collections import Counter
    total: Counter[str] = Counter()
    buckets: dict[str, list[str]] = {}
    mentions: Counter[str] = Counter()
    for page, text in pairs:
        emotions = nz.prop_multi_select(page, "情绪")
        total.update(emotions)
        local = _to_local(page.get("created_time", ""))
        if local is not None:
            # 同一分组内多条记录的情绪合并去重，保留首次出现的顺序
            got = buckets.setdefault(bucket_of(local), [])
            got.extend(e for e in emotions if e not in got)
        mentions.update(extract_mentions(text))

    total_label, bucket_label, mention_label = labels
    lines = [f"【{total_label}】{len(pairs)} 条"]
    if total:
        lines.append("【情绪分布】" + " · ".join(
            f"{name}({n})" for name, n in total.most_common()))
    if buckets:
        lines.append(f"【{bucket_label}】")
        lines.extend(f"  {key}：{'、'.join(es) if es else '（无标签）'}"
                     for key, es in buckets.items())
    if mentions:
        lines.append(f"【{mention_label}】" + " · ".join(
            f"{name} {n} 次" for name, n in mentions.most_common(top_mentions)))
    return "\n".join(lines)


def _by_day(local) -> str:
    """周回顾的分组键：'07-20 周一'。"""
    return f"{local.strftime('%m-%d')} 周{'一二三四五六日'[local.weekday()]}"


def _by_week_of_month(local) -> str:
    """月总结的分组键：'第3周 (07-15~07-21)'。按月内第几个 7 天切，简单且各月一致。
    末尾那段按当月最后一天截断，免得 7 月的总结里出现 8 月的日期。"""
    import calendar
    from datetime import timedelta
    idx = (local.day - 1) // 7
    start = local.replace(day=idx * 7 + 1)
    last = calendar.monthrange(local.year, local.month)[1]
    end = min(start + timedelta(days=6), start.replace(day=last))
    return f"第{idx + 1}周 ({start.strftime('%m-%d')}~{end.strftime('%m-%d')})"


def _weekly_stats(nz: Notion, pairs: list[tuple[dict, str]]) -> str:
    return _period_stats(nz, pairs, _by_day,
                         ("本周记录", "逐日情绪", "本周提到的人与事"))


def _monthly_stats(nz: Notion, pairs: list[tuple[dict, str]]) -> str:
    """月总结的统计：多给几个高频人事，一个月的关系网络比一周宽。"""
    return _period_stats(nz, pairs, _by_week_of_month,
                         ("本月记录", "逐周情绪", "本月提到的人与事"),
                         top_mentions=10)


def _mood_snapshot(nz: Notion, pages: list[dict], top: int = 3) -> str:
    """把当天各条记录的情绪标签汇总成一行『心情底色』，复用已识别好的标签，不额外调模型。
    按出现频次取前 top 个，如『笃定 · 平静 · 感激』。全无标签则返回空串。"""
    from collections import Counter
    counter: Counter[str] = Counter()
    for p in pages:
        for e in nz.prop_multi_select(p, "情绪"):
            counter[e] += 1
    return " · ".join(name for name, _ in counter.most_common(top))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task",
                    choices=["all", "classify", "respond", "rules", "letter",
                             "daily", "weekly", "monthly", "bubbles"],
                    default="all")
    ap.add_argument("--force", action="store_true",
                    help="monthly 任务：忽略『必须月末那天』的判断，立即生成（手动补跑用）")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default="docs/data.json",
                    help="bubbles 任务的数据输出路径（默认 docs/data.json，供 GitHub Pages 用）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    nz = Notion(cfg["notion"])

    # bubbles 只读 Notion、不调任何模型，单独处理，避免无谓地初始化 LLM。
    if args.task == "bubbles":
        days = cfg.get("letter", {}).get("lookback_days", 7)
        data = export_bubble_data(nz, days, args.out)
        print(f"[bubbles] 近 {days} 天 {data['total_entries']} 条记录 -> "
              f"{len(data['bubbles'])} 种情绪，已写入 {args.out}")
        return

    classifier = llm_from_cfg(cfg["classifier"])
    responder = responder_from_cfg(cfg["responder"])
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
    # daily / letter 是“错过就没了”的一次性任务：模型全失败时用非零退出码，
    # 让 GitHub Actions 显示红叉，方便一眼发现没生成。
    # “今天/近期没记录”(empty) 是正常情况，仍按成功(绿色)处理。
    if args.task == "letter":
        status = task_letter(nz, responder, cfg.get("letter", {}).get("lookback_days", 7))
        if status == "failed":
            sys.exit(1)
    if args.task == "daily":
        status = task_daily(nz, responder)
        if status == "failed":
            sys.exit(1)
    if args.task == "weekly":
        wk = cfg.get("weekly", {})
        status = task_weekly(nz, responder,
                             wk.get("lookback_days", 7),
                             wk.get("max_chars", 20000))
        if status == "failed":
            sys.exit(1)
    if args.task == "monthly":
        mo = cfg.get("monthly", {})
        status = task_monthly(nz, responder,
                             mo.get("max_chars", 60000),
                             force=args.force)
        if status == "failed":
            sys.exit(1)


if __name__ == "__main__":
    main()
