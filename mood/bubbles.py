"""情绪泡泡图数据：统计最近 N 天的情绪关键词频率，导出成 data.json，
供 docs/index.html（D3 力导向气泡图）在浏览器端渲染。

只读 Entries 里已识别好的「情绪」multi_select 属性 —— 不调模型、不读正文，
所以这个任务极快、几乎零成本，可以高频跑。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .notion import Notion

# 给 classify.py 的 15 个情绪词表各配一个固定颜色，保证每次渲染颜色一致。
# 一套柔和、协调的现代色板：暖色/亮色偏正向，冷色/暗色偏负向，
# 整体降低饱和度、统一明度，避免刺眼，凑在一起更高级耐看。
EMOTION_COLORS: dict[str, str] = {
    # ── 积极 / 暖色 ──
    "喜悦": "#F6C667",  # 柔金
    "感激": "#F4A26B",  # 暖橘
    "期待": "#7FB7E8",  # 晴空蓝（偏暖的亮蓝）
    "满足": "#9CD3A2",  # 豆绿
    "平静": "#8FCFC4",  # 雾青
    # ── 消极 / 冷色 ──
    "焦虑": "#B49AD6",  # 雾紫
    "悲伤": "#7E9BD4",  # 灰蓝
    "愤怒": "#E08585",  # 砖红（降饱和）
    "孤独": "#8A9BB3",  # 石板蓝
    "疲惫": "#AAB0B8",  # 暖灰
    "失望": "#B7A9C0",  # 灰紫
    "恐惧": "#7C6FA6",  # 暗紫
    "愧疚": "#C9A48E",  # 陶土
    "迷茫": "#9AA7BC",  # 雾蓝灰
    "烦躁": "#E0A176",  # 焦糖橙
}
_DEFAULT_COLOR = "#C7CCD4"  # 词表外的情绪（理论上不会有）兜底用柔灰


def build_bubble_data(nz: Notion, days: int) -> dict:
    """统计最近 days 天每个情绪出现的次数，返回可直接 json.dump 的结构。"""
    pages = nz.recent_entries(days)
    counter: Counter[str] = Counter()
    for page in pages:
        for name in nz.prop_multi_select(page, "情绪"):
            counter[name] += 1

    bubbles = [
        {
            "emotion": emotion,
            "count": count,
            "color": EMOTION_COLORS.get(emotion, _DEFAULT_COLOR),
        }
        for emotion, count in counter.most_common()
    ]
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "total_entries": len(pages),
        "bubbles": bubbles,
    }


def export_bubble_data(nz: Notion, days: int, out_path: str) -> dict:
    """统计并写入 out_path，返回数据本身便于打印日志。"""
    data = build_bubble_data(nz, days)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
