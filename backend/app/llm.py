"""LLM 调用封装（阶段二：知识点识别与苏格拉底式提问）。

约束与设计
----------
1. **不引入新依赖**：仅使用标准库 ``urllib``，不依赖任何第三方 SDK。
2. **无 Agent 降级模式**：未配置 LLM，或调用超时/报错/返回非法 JSON 时，
   一律返回 ``ok=False`` 而不抛异常，由调用方退化到本地启发式实现。
   「核心业务逻辑不依赖 DSH」——错题录入、薄弱点警报、复习调度都不需要 LLM。
3. **接口形态**：OpenAI 兼容的 ``POST {api_base}/chat/completions``。

配置项（全部可选，见 .env.example）：
    LLM_API_BASE / LLM_API_KEY / LLM_MODEL / LLM_TIMEOUT_SECONDS
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import settings
from .logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """一次 LLM 调用的结果。"""

    ok: bool
    data: dict | None = None
    error: str | None = None
    text: str | None = None


def parse_json_object(text: str | None) -> dict | None:
    """从模型输出中稳健地抽取第一个 JSON 对象。

    兼容 ```json 代码块、前后夹杂说明文字等常见情况。
    """
    if not text:
        return None

    raw = text.strip()
    # 去掉 Markdown 代码块围栏
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()

    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    # 退而求其次：截取第一个 { 到最后一个 } 之间的内容
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


class LLMClient:
    """OpenAI 兼容接口的最小客户端；任何失败都不抛异常。"""

    def __init__(
        self,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_base = (settings.llm_api_base if api_base is None else api_base).rstrip("/")
        self.api_key = settings.llm_api_key if api_key is None else api_key
        self.model = settings.llm_model if model is None else model
        self.timeout = settings.llm_timeout_seconds if timeout is None else timeout

    @property
    def available(self) -> bool:
        """是否具备调用条件（缺任一必要配置即视为不可用）。"""
        return bool(self.api_base and self.model)

    def complete_text(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> LLMResponse:
        """发起一次对话补全，返回**原始文本**（不做 JSON 解析）。"""
        return self._chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            parse_json=False,
        )

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> LLMResponse:
        """发起一次对话补全并解析出 JSON 对象。"""
        return self._chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            parse_json=True,
        )

    def _chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 800,
        parse_json: bool = False,
    ) -> LLMResponse:
        """统一的对话补全实现；任何失败都返回 ``ok=False``，不抛异常。"""
        if not self.available:
            return LLMResponse(ok=False, error="LLM 未配置")

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        request = urllib.request.Request(
            url=f"{self.api_base}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # 不回显任何凭据
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # pragma: no cover - 读取错误体失败
                pass
            logger.warning("LLM HTTP 错误 model=%s status=%s %s", self.model, exc.code, detail)
            return LLMResponse(ok=False, error=f"HTTP {exc.code}")
        except Exception as exc:  # 超时 / DNS / 连接失败等
            logger.warning("LLM 调用失败 model=%s: %s: %s", self.model, type(exc).__name__, exc)
            return LLMResponse(ok=False, error=f"{type(exc).__name__}: {exc}")

        try:
            envelope = json.loads(body)
            text = envelope["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            logger.warning("LLM 响应结构异常 model=%s: %s", self.model, exc)
            return LLMResponse(ok=False, error="响应结构异常", text=body[:500])

        if not parse_json:
            return LLMResponse(ok=True, text=text)

        data = parse_json_object(text)
        if data is None:
            logger.warning("LLM 未返回合法 JSON model=%s", self.model)
            return LLMResponse(ok=False, error="未返回合法 JSON", text=text[:500])

        return LLMResponse(ok=True, data=data, text=text)


# 默认客户端（路由/服务层复用）
default_client = LLMClient()
