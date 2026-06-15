"""@实体解析：从正文中提取 @人物/事物，建实体并关联回记录。

支持两种写法：
  @张三            —— 到空白或标点结束
  @[复杂 名称]     —— 方括号包裹，允许含空格
"""
from __future__ import annotations

import re

# @[...] 优先；否则 @后跟非空白非标点的一串
_PATTERN = re.compile(r"@\[([^\]]+)\]|@([^\s,，。;；!！?？、@]+)")


def extract_mentions(text: str) -> list[str]:
    names: list[str] = []
    seen = set()
    for m in _PATTERN.finditer(text):
        name = (m.group(1) or m.group(2) or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names
