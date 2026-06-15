"""情绪识别：对一条记录的正文，用小模型打情绪标签。"""
from __future__ import annotations

import json
import re

from .llm import LLM

# 可按需扩展的情绪词表；约束模型只从中选，保证标签一致
EMOTION_VOCAB = [
    "喜悦", "平静", "感激", "期待", "满足",
    "焦虑", "悲伤", "愤怒", "孤独", "疲惫",
    "失望", "恐惧", "愧疚", "迷茫", "烦躁",
]

_SYSTEM = (
    "你是情绪标注助手。阅读用户的一段心情记录，从给定情绪词表中选出最贴切的 1-3 个标签，"
    "并为这条记录拟一个不超过 15 字、贴合内容的简短标题。"
    "只输出 JSON，格式为 {\"emotions\": [\"标签1\", ...], \"title\": \"标题\"}，不要解释。"
)


def classify(llm: LLM, text: str) -> tuple[list[str], str]:
    """返回 (情绪标签列表, 自动标题)。"""
    if not text.strip():
        return [], ""
    user = f"情绪词表：{', '.join(EMOTION_VOCAB)}\n\n记录内容：\n{text}"
    raw = llm.complete(_SYSTEM, user, max_tokens=300)
    emotions, title = _parse(raw)
    # 只保留词表内的，去重保序
    seen, out = set(), []
    for e in emotions:
        if e in EMOTION_VOCAB and e not in seen:
            seen.add(e)
            out.append(e)
    return out[:3], title


def _parse(raw: str) -> tuple[list[str], str]:
    # 容错：模型可能包裹 ```json 或加杂言
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return [], ""
    try:
        data = json.loads(m.group(0))
        val = data.get("emotions", [])
        emotions = [str(x).strip() for x in val] if isinstance(val, list) else []
        title = str(data.get("title", "")).strip()
        return emotions, title
    except json.JSONDecodeError:
        return [], ""
