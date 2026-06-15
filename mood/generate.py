"""长文生成：回应、来信、规则提醒。全部走 responder（你的好模型）。"""
from __future__ import annotations

import json
import re

from .llm import LLM

# ── 1. 对单条记录生成回应 ────────────────────────────────
_RESPOND_SYSTEM = (
    "你是一位真诚、克制、贴近事实的倾听者，也是长期陪伴用户的老朋友。"
    "用户分享了一段新的心情记录，希望得到回应。系统会提供 TA 过去的若干条记录作为记忆背景。"
    "要求：结合背景记忆理解 TA 的处境与情绪脉络（如反复出现的人、长期的困扰、情绪变化），"
    "但回应要聚焦本次这条记录。不灌鸡汤、不空泛安慰、不说教。先准确理解并复述对方的处境，"
    "再给出具体、可落地、贴合事实的观察或建议。可以自然地引用过去（如\"上次你提到…\"），"
    "但不要生硬罗列。语气温和、像朋友。控制在 250-450 字。"
)


def respond(llm: LLM, text: str, memory: str = "") -> str:
    if memory:
        user = f"【过去的记忆，仅作背景参考】\n{memory}\n\n【本次新记录】\n{text}"
    else:
        user = text
    return llm.complete(_RESPOND_SYSTEM, user, max_tokens=1000)


# ── 1b. 每日总结 ─────────────────────────────────────────
_DAILY_SYSTEM = (
    "你是用户贴心的反思伙伴。下面是 TA 今天写下的全部心情记录。"
    "请只根据今天的内容，写一段温暖而有洞察的日总结，可涵盖：情绪起伏、人际关系、"
    "烦恼的开解、欢乐的回顾、值得感恩的事。不必面面俱到，挑当天真正突出的方面写，"
    "紧扣记录里的真实细节，不灌鸡汤、不编造。用第二人称\"你\"，温和真诚。300-500 字。"
)


def daily_summary(llm: LLM, entries_text: str) -> tuple[str, str]:
    """返回 (标题, 正文)。"""
    body = llm.complete(_DAILY_SYSTEM, entries_text, max_tokens=1200)
    from datetime import datetime
    title = f"日总结 · {datetime.now().strftime('%Y-%m-%d')}"
    return title, body


# ── 2. 定期来信 ──────────────────────────────────────────
_LETTER_SYSTEM = (
    "你是用户的一位老朋友，会定期给 TA 写信。下面是 TA 最近一段时间的若干条心情记录。"
    "请综合这些记录，写一封温暖而诚实的信：点出你观察到的情绪走向与值得留意的模式，"
    "肯定具体的进步，也温和提出可能被忽略的地方。不灌鸡汤、不泛泛而谈，紧扣记录里的真实细节。"
    "以「亲爱的你」开头，落款「——一直在的我」。500-800 字。"
)


def write_letter(llm: LLM, entries_text: str) -> tuple[str, str]:
    """返回 (标题, 正文)。"""
    body = llm.complete(_LETTER_SYSTEM, entries_text, max_tokens=2000)
    from datetime import datetime
    title = f"来信 · {datetime.now().strftime('%Y-%m-%d')}"
    return title, body


# ── 3. 规则 agent（如反事实大师）──────────────────────────
_RULE_SYSTEM = (
    "你是一个按规则工作的反思助手。给你一条用户记录、一个规则的触发条件和提醒话术。"
    "判断这条记录是否命中该触发条件。"
    "只输出 JSON：{\"hit\": true/false, \"message\": \"命中时给用户的提醒，未命中留空\"}。"
    "提醒要具体引用记录中的原话，温和、就事论事，不超过 120 字。"
)


def apply_rule(llm: LLM, entry_text: str, condition: str, phrasing: str) -> str | None:
    """命中则返回提醒文本，否则 None。"""
    user = (
        f"【规则触发条件】{condition}\n"
        f"【提醒话术指引】{phrasing or '（无特别要求）'}\n\n"
        f"【用户记录】\n{entry_text}"
    )
    raw = llm.complete(_RULE_SYSTEM, user, max_tokens=300)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if data.get("hit") and data.get("message", "").strip():
        return data["message"].strip()
    return None
