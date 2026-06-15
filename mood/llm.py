"""LLM 调用层：统一封装 anthropic / openai，供分类器和长文生成共用。

约定：所有 prompt 用中文，因为记录和回应都是中文场景。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LLM:
    provider: str          # "anthropic" | "openai"
    model: str
    api_key: str
    base_url: str = ""     # 自定义接口地址（中转站 / DeepSeek 等），留空用官方默认

    def complete(self, system: str, user: str, max_tokens: int = 1024) -> str:
        """单轮补全，返回纯文本。"""
        if self.provider == "anthropic":
            return self._anthropic(system, user, max_tokens)
        if self.provider == "openai":
            return self._openai(system, user, max_tokens)
        raise ValueError(f"未知的 provider: {self.provider}")

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
