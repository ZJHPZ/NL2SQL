"""模型提供者实现。

* `OpenAICompatProvider` —— 一切 OpenAI 兼容端点（DeepSeek / 通义 / Kimi / GLM / OpenAI）
* `MockLLM` —— 测试用，按预设脚本返回，不联网
* `NullLLM` —— 明确表示"没有模型"，`available()` 恒为 False，
  让上层干净地降级到规则规划，而不是抛异常

换厂商只改 base_url 与 model 两行——这是选 OpenAI 兼容协议的全部理由。
"""
from __future__ import annotations

import json
import time
from typing import Any, Sequence

from ..contracts.llm import ChatMessage, LLMResponse

__all__ = ["LLMError", "OpenAICompatProvider", "MockLLM", "NullLLM", "DEFAULT_BASE_URLS"]

# 常见厂商的默认端点（只填 base_url，key 由用户提供）
DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "openai": "https://api.openai.com/v1",
}

DEFAULT_MODELS = {
    "deepseek": "deepseek-chat",
    "dashscope": "qwen-plus",
    "moonshot": "moonshot-v1-8k",
    "zhipu": "glm-4-plus",
    "openai": "gpt-4o-mini",
}


class LLMError(Exception):
    """模型调用失败。上层应据此降级（如回退到规则规划）。"""


class NullLLM:
    """没有配置模型。

    `available()` 恒为 False，上层据此选择其它规划器，
    而不是捕获异常来控制流程——"没有模型"是正常状态，不是错误。
    """

    name = "null"
    model = ""

    def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> LLMResponse:
        raise LLMError("未配置模型提供者")

    def available(self) -> bool:
        return False


class MockLLM:
    """测试替身：按队列返回预设响应，不联网。

    用途：验证编排链路（Prompt 组装、JSON 解析、失败降级）而不依赖真实模型。
    """

    name = "mock"

    def __init__(self, responses: list[str] | str, *, model: str = "mock-1",
                 available: bool = True) -> None:
        self._responses = [responses] if isinstance(responses, str) else list(responses)
        self.model = model
        self._available = available
        self.calls: list[list[ChatMessage]] = []

    def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> LLMResponse:
        self.calls.append(list(messages))
        if not self._responses:
            raise LLMError("MockLLM 没有更多预设响应")
        text = self._responses.pop(0)
        return LLMResponse(text=text, model=self.model, latency_ms=1)

    def available(self) -> bool:
        return self._available


class OpenAICompatProvider:
    """OpenAI 兼容端点客户端。

    延迟导入 `openai`：核心不依赖它（没装也能用规则规划器）。
    """

    def __init__(self, *, api_key: str | None, base_url: str | None = None,
                 model: str = "deepseek-chat", vendor: str = "deepseek",
                 timeout_s: float = 60.0, max_retries: int = 2,
                 name: str | None = None) -> None:
        self._api_key = api_key or ""
        self._vendor = vendor
        self._base_url = base_url or DEFAULT_BASE_URLS.get(vendor, "")
        self.model = model or DEFAULT_MODELS.get(vendor, "")
        self._timeout = timeout_s
        self._max_retries = max_retries
        self.name = name or f"openai-compat:{vendor}"
        self._client: Any = None

    # ------------------------------------------------------------ 客户端
    @property
    def base_url(self) -> str:
        return self._base_url

    def available(self) -> bool:
        return bool(self._api_key and self._base_url and self.model)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI  # 延迟导入
        except ImportError as e:
            raise LLMError(
                "未安装 openai 客户端；pip install openai"
            ) from e
        self._client = OpenAI(
            api_key=self._api_key, base_url=self._base_url,
            timeout=self._timeout, max_retries=self._max_retries,
        )
        return self._client

    # ------------------------------------------------------------ 调用
    def chat(self, messages: Sequence[ChatMessage], *,
             temperature: float = 0.0, max_tokens: int | None = None,
             json_mode: bool = False) -> LLMResponse:
        if not self.available():
            raise LLMError(
                f"模型未就绪：key={'有' if self._api_key else '缺'}"
                f" base_url={'有' if self._base_url else '缺'} model={self.model or '缺'}"
            )
        client = self._get_client()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            # 不是所有兼容端点都支持；失败时上层会降级到文本解析
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            resp = client.chat.completions.create(**payload)
        except Exception as e:  # noqa: BLE001 - 统一转成 LLMError
            if json_mode:
                # 退一步：不带 response_format 重试一次
                payload.pop("response_format", None)
                try:
                    resp = client.chat.completions.create(**payload)
                except Exception as e2:  # noqa: BLE001
                    raise LLMError(f"模型调用失败: {e2}") from e2
            else:
                raise LLMError(f"模型调用失败: {e}") from e
        latency_ms = int((time.perf_counter() - started) * 1000)

        choice = resp.choices[0] if resp.choices else None
        text = (choice.message.content if choice and choice.message else "") or ""
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text=text,
            model=getattr(resp, "model", self.model),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency_ms,
            finish_reason=(getattr(choice, "finish_reason", "") or "") if choice else "",
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "vendor": self._vendor, "model": self.model,
            "base_url": self._base_url, "available": self.available(),
            "has_key": bool(self._api_key),
        }

    # ------------------------------------------------------------ 工具
    @staticmethod
    def parse_json(text: str) -> dict[str, Any]:
        """从模型输出里抠出 JSON。

        模型经常在 JSON 外包一层 ```json 代码块或解释文字，
        因此不能直接 json.loads。这是接入真实模型后必然要处理的现实。
        """
        text = (text or "").strip()
        if not text:
            raise LLMError("模型返回为空")
        # 去掉 markdown 代码块围栏
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 退而求其次：截取第一个 { 到最后一个 }
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError as e:
                raise LLMError(f"模型输出不是合法 JSON: {e}") from e
        raise LLMError("模型输出中找不到 JSON 对象")
