"""LLM 调用层：统一封装 anthropic / openai，供分类器和长文生成共用。

约定：所有 prompt 用中文，因为记录和回应都是中文场景。
对中转站/上游的偶发抖动（502/503/429/超时）自动重试，避免一次失败整个任务挂掉。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

# 这些异常通常是临时性的，值得重试
_RETRY_MARKERS = ("502", "503", "429", "500", "overloaded", "timeout",
                  "temporarily", "upstream", "unavailable")
_MAX_ATTEMPTS = 4
_BASE_DELAY = 2.0   # 秒，指数退避：2,4,8...


def _is_transient(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _RETRY_MARKERS)


@dataclass
class LLM:
    provider: str          # "anthropic" | "openai"
    model: str
    api_key: str
    base_url: str = ""     # 自定义接口地址（中转站 / DeepSeek 等），留空用官方默认

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        """单轮补全，返回纯文本。对临时性错误自动重试。"""
        last_err: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                if self.provider == "anthropic":
                    return self._anthropic(system, user, max_tokens)
                if self.provider == "openai":
                    return self._openai(system, user, max_tokens)
                raise ValueError(f"未知的 provider: {self.provider}")
            except ValueError:
                raise  # 配置错误不重试
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < _MAX_ATTEMPTS - 1 and _is_transient(e):
                    delay = _BASE_DELAY * (2 ** attempt)
                    print(f"[llm] 第 {attempt + 1} 次失败（{type(e).__name__}），{delay:.0f}s 后重试…")
                    time.sleep(delay)
                    continue
                raise
        raise last_err  # 理论上到不了这里

    def _anthropic(self, system: str, user: str, max_tokens: int) -> str:
        from anthropic import Anthropic

        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = Anthropic(**kwargs)
        resp = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # 拼接所有 text block
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()

    def _openai(self, system: str, user: str, max_tokens: int) -> str:
        from openai import OpenAI

        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()


def llm_from_cfg(section: dict) -> LLM:
    return LLM(
        provider=section["provider"],
        model=section["model"],
        api_key=section["api_key"],
        base_url=section.get("base_url", ""),
    )


# 全部失败时返回的固定兜底话术
FALLBACK_REPLY = "您的模型开小差啦，可以考虑换个模型来源噢~"


@dataclass
class FallbackLLM:
    """按模型名链式降级：主模型重试失败后，用同一 key/端点换备用模型名再试。
    全部失败则返回固定兜底话术（而不是让整个任务崩溃）。
    接口与 LLM 一致，generate.py 无需改动。"""
    primary: LLM
    fallback_models: list[str]      # 依次尝试的备用模型名
    final_reply: str = FALLBACK_REPLY

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        # 1) 先试主模型（内部已带重试）
        try:
            return self.primary.complete(system, user, max_tokens)
        except Exception as e:  # noqa: BLE001
            print(f"[llm] 主模型 {self.primary.model} 失败（{type(e).__name__}）")
        # 2) 依次换备用模型名（同 key、同端点）
        for name in self.fallback_models:
            alt = LLM(provider=self.primary.provider, model=name,
                      api_key=self.primary.api_key, base_url=self.primary.base_url)
            try:
                print(f"[llm] 改用备用模型 {name}…")
                return alt.complete(system, user, max_tokens)
            except Exception as e:  # noqa: BLE001
                print(f"[llm] 备用模型 {name} 也失败（{type(e).__name__}）")
        # 3) 全失败 → 固定兜底话术
        print("[llm] 所有模型均失败，返回兜底话术")
        return self.final_reply


def responder_from_cfg(section: dict) -> FallbackLLM:
    """构造带降级链的回应模型。fallback_models 来自 config，缺省为空。"""
    primary = llm_from_cfg(section)
    return FallbackLLM(
        primary=primary,
        fallback_models=section.get("fallback_models", []),
    )
