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


def _err_detail(err: Exception) -> str:
    """尽量榨出可诊断的错误信息：HTTP 状态码 + 上游返回的正文。
    OpenAI/Anthropic SDK 的异常对象常带 .status_code 和 .response，
    光打类名（InternalServerError）等于没说，必须把正文挖出来。"""
    parts: list[str] = []
    code = getattr(err, "status_code", None)
    if code is not None:
        parts.append(f"HTTP {code}")
    # SDK 异常通常带 .response（httpx.Response），里面才是上游的真话
    resp = getattr(err, "response", None)
    body = None
    if resp is not None:
        try:
            body = resp.text
        except Exception:  # noqa: BLE001
            body = None
    msg = body or str(err)
    if msg:
        # 截断，避免上游返回一大坨 HTML 把日志刷爆
        msg = msg.replace("\n", " ").strip()
        parts.append(msg[:500])
    return " | ".join(parts) or repr(err)


def _http_status(err: Exception) -> int | None:
    return getattr(err, "status_code", None)


def _is_transient(err: Exception) -> bool:
    # 明确的 HTTP 状态码优先判断：4xx（除 408 超时/429 限流）是客户端错误，重试无意义
    code = _http_status(err)
    if code is not None:
        if code in (408, 429):
            return True
        if 400 <= code < 500:
            return False   # 请求本身有问题（太长 / 参数错 / 鉴权），重试也是白搭
        if code >= 500:
            return True     # 上游错误，值得重试
    # 拿不到状态码时退回关键字匹配
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
                    print(f"[llm] 第 {attempt + 1} 次失败，{delay:.0f}s 后重试… 详情：{_err_detail(e)}")
                    time.sleep(delay)
                    continue
                # 不重试（4xx 等）或已是最后一次：把真实原因打出来再抛
                print(f"[llm] 放弃重试（{type(e).__name__}）：{_err_detail(e)}")
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
            print(f"[llm] 主模型 {self.primary.model} 失败：{_err_detail(e)}")
        # 2) 依次换备用模型名（同 key、同端点）
        for name in self.fallback_models:
            alt = LLM(provider=self.primary.provider, model=name,
                      api_key=self.primary.api_key, base_url=self.primary.base_url)
            try:
                print(f"[llm] 改用备用模型 {name}…")
                return alt.complete(system, user, max_tokens)
            except Exception as e:  # noqa: BLE001
                print(f"[llm] 备用模型 {name} 也失败：{_err_detail(e)}")
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
