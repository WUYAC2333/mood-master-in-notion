"""配置加载：从 config.yaml 读取，${VAR} 占位符用环境变量替换。

本地运行：把 config.example.yaml 复制成 config.yaml 直接填值。
GitHub Actions：config.yaml 里保留 ${NOTION_TOKEN} 这种占位符，
                由 Secrets 注入的环境变量替换。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value):
    """递归地把字符串里的 ${VAR} 替换成环境变量值。"""
    if isinstance(value, str):
        def repl(m):
            name = m.group(1)
            env = os.environ.get(name)
            if env is None:
                # 没设环境变量就原样保留，方便本地直接写明文
                return m.group(0)
            return env
        return _VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: str | None = None) -> dict:
    cfg_path = Path(path or os.environ.get("MOOD_CONFIG", "config.yaml"))
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"找不到配置文件 {cfg_path}。请先把 config.example.yaml 复制为 config.yaml 并填写。"
        )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    return _expand(raw)
