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
    "但不要生硬罗列。语气温和、像朋友。控制在 250-450 字。\n"
    "每条记忆前的 [方括号] 是它的真实发生时间（北京时间，含星期与时刻）；开头的【当前时间】是现在。"
    "引用过去时请据此换算成自然的相对说法（如\"昨天\"\"上周六\"\"上个月8号凌晨\"），不要照抄时间戳。"
)


def respond(llm: LLM, text: str, memory: str = "") -> str:
    if memory:
        user = f"【过去的记忆，仅作背景参考】\n{memory}\n\n【本次新记录】\n{text}"
    else:
        user = text
    return llm.complete(_RESPOND_SYSTEM, user, max_tokens=1000)


# ── 1b. 每日总结 ─────────────────────────────────────────
_DAILY_SYSTEM = (
    "你是用户贴心的反思伙伴，也是一个真心欣赏 TA 的朋友。下面是 TA 今天写下的全部心情记录。\n"
    "请只根据今天的内容，按以下四部分写日总结，每部分之间空一行：\n"
    "1）正文：一段流动、温暖而有洞察的回顾，可涵盖情绪起伏、人际关系、烦恼的开解、欢乐的回顾、"
    "值得感恩的事。挑当天真正突出的方面写，紧扣真实细节，不灌鸡汤、不编造。用第二人称\"你\"，温和真诚，300-500 字。\n"
    "2）另起一行，以「亮点：」开头，真诚而具体地表扬 TA 今天做得好的 1-3 件事——可以是某个行动、"
    "一种心态、对自己的照顾或一个微小的坚持。要点名具体的事，让肯定落到实处，不空泛吹捧。这部分是重点，多给一些暖意。\n"
    "3）另起一行，以「小提醒：」开头，温柔地指出至多 1-2 件也许能用更好方式处理的事，"
    "用\"也许可以试试…\"这样的商量口吻，就事论事、不说教、不指责。今天若实在没有需要提醒的，"
    "就只写「小提醒：今天没什么要改的，好好为自己高兴。」\n"
    "4）另起一行，以「一句话：」开头，写一句 20-40 字、温柔而有力的话，像给这一天轻轻盖上一个章，"
    "能独立成句、经得起单独回味。\n"
    "每条记录前的【方括号】是它的真实发生时间（北京时间，含星期与时刻），开头的【当前时间】是现在。"
    "提到某件事时可自然点出时间（如\"今早\"\"下午三点多\"），让回顾更有画面感，但不要生硬罗列时间戳。"
)

# 正文之外三个结构化板块的标记，供 _parse_daily 按实际位置切分
_DAILY_MARKS = [("praise", "亮点："), ("suggestion", "小提醒："), ("closing", "一句话：")]


def daily_summary(llm: LLM, entries_text: str) -> tuple[str, dict]:
    """返回 (标题, sections)。sections 含 body/praise/suggestion/closing 四段，
    后三段可能为空（模型未按格式输出或全失败时）。"""
    raw = llm.complete(_DAILY_SYSTEM, entries_text, max_tokens=1500)
    sections = _parse_daily(raw)
    from datetime import datetime, timedelta, timezone
    local = datetime.now(timezone(timedelta(hours=8)))  # 北京时间，避免 UTC runner 算错日期
    week = "一二三四五六日"[local.weekday()]
    title = f"日总结 · {local.strftime('%Y-%m-%d')} 周{week}"
    return title, sections


def _parse_daily(raw: str) -> dict:
    """按「亮点：」「小提醒：」「一句话：」的实际出现位置切分模型输出。
    找不到任何标记就整段当正文，其余留空——保证退化时不丢内容。
    返回 {'body', 'praise', 'suggestion', 'closing'}，四键恒在。"""
    # 找出每个标记在文本中的位置（按出现顺序，容忍模型漏写某些板块）
    found = [(name, mark, idx)
             for name, mark in _DAILY_MARKS
             if (idx := raw.find(mark)) != -1]
    found.sort(key=lambda x: x[2])
    out = {"body": "", "praise": "", "suggestion": "", "closing": ""}
    if not found:
        out["body"] = raw.strip()
        return out
    out["body"] = raw[:found[0][2]].strip()
    for i, (name, mark, idx) in enumerate(found):
        start = idx + len(mark)
        end = found[i + 1][2] if i + 1 < len(found) else len(raw)
        out[name] = raw[start:end].strip().strip("「」\"'）)")
    # 万一模型把正文也漏了标记导致 body 空，退回整段
    if not out["body"]:
        out["body"] = raw.strip()
    return out


# ── 2. 定期来信 ──────────────────────────────────────────
_LETTER_SYSTEM = (
    "你是用户的一位老朋友，会定期给 TA 写信。用户的昵称是「秃秃」。"
    "下面是 TA 最近一段时间的若干条心情记录。"
    "请综合这些记录，写一封温暖而诚实的信：点出你观察到的情绪走向与值得留意的模式，"
    "肯定具体的进步，也温和提出可能被忽略的地方。不灌鸡汤、不泛泛而谈，紧扣记录里的真实细节。"
    "以「亲爱的秃秃」开头。全程称呼对方为「秃秃」，不要用「你」。"
    "你自称时别只用干巴巴的「我」，而是给自己起一个亲切俏皮、贯穿全信的自称"
    "（比如「你的老树洞」「一直蹲在信箱旁的我」这类有点性格的说法，你可自由发挥，但整封信保持同一个自称）。"
    "落款也用这个身份收尾，温暖而有趣。500-800 字。\n"
    "每条记录前的【方括号】是它的真实发生时间（北京时间，含星期与时刻），开头的【当前时间】是现在。"
    "回顾往事时请据此换算成具体而自然的相对说法（如\"上周三傍晚\"\"这个月8号凌晨\"），"
    "而不是含糊的\"前几天\"；但也别机械罗列时间戳，让时间自然融进叙述。"
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
