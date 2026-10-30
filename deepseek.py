"""
DeepSeek 客户端封装（OpenAI 兼容协议）。

对外只提供 `generate()` 一个方法，且不依赖项目内其他模块。

用法：
    from deepseek import DeepSeekClient, DeepSeekError
    client = DeepSeekClient("sk-xxxx")
    text = client.generate("你是助手", "用一句话介绍你自己")
"""

from __future__ import annotations

import random
import time
from typing import Optional

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

# 可重试的 HTTP 状态码：超时 / 限流 / 服务端临时错误
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class DeepSeekError(RuntimeError):
    """模型调用失败（不可恢复，或重试次数已耗尽）。"""


def _error_status(exc: BaseException) -> Optional[int]:
    """尽量从各家 SDK 的异常对象里抠出 HTTP 状态码。"""
    for attr in ("status_code", "status", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    return None


class DeepSeekClient:
    """极薄的 DeepSeek 封装：一次 `generate` = 一轮 system + user 对话。"""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = 0.2,
        max_retries: int = 3,
    ) -> None:
        api_key = (api_key or "").strip()
        if not api_key:
            raise DeepSeekError("未提供 DeepSeek API Key")

        try:
            from openai import OpenAI  # 延迟导入：没装 openai 时也能 import 本模块
        except ImportError as exc:  # pragma: no cover
            raise DeepSeekError("缺少依赖 openai，请先执行：pip install openai") from exc

        self.model = model or DEFAULT_MODEL
        self.temperature = temperature
        self.max_retries = max(1, int(max_retries))
        try:
            self.client = OpenAI(api_key=api_key, base_url=base_url or DEFAULT_BASE_URL)
        except Exception as exc:  # noqa: BLE001
            raise DeepSeekError(f"DeepSeek 客户端初始化失败: {exc}") from exc

    def generate(self, system_instruction: str, user_prompt: str) -> str:
        """调用模型并返回纯文本。失败抛 `DeepSeekError`。"""
        last_exc: Optional[BaseException] = None

        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_instruction or ""},
                        {"role": "user", "content": user_prompt or ""},
                    ],
                    temperature=self.temperature,
                )
                content = response.choices[0].message.content
                return (content or "").strip()
            except Exception as exc:  # noqa: BLE001 - 统一转换成本项目异常
                last_exc = exc
                status = _error_status(exc)
                if status not in _RETRYABLE_STATUS or attempt >= self.max_retries - 1:
                    break
                # 指数退避 + 抖动
                delay = (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)

        raise DeepSeekError(f"DeepSeek 调用失败: {last_exc}")
