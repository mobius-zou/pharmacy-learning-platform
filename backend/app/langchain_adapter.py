"""LangChain 适配层。

把本项目的标准库 LLM 客户端包装成 LangChain ``BaseChatModel``，
从而可以直接驱动 ``LLMGraphTransformer`` 等 LangChain 组件，
而**不需要任何厂商 SDK**（openai / anthropic 等一律不需要）。

同时提供 ``build_graph_transformer()``：在未配置 LLM 或缺少
``langchain-experimental`` 时返回 None，由调用方退化到规则抽取。
"""

from __future__ import annotations

from typing import Any, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .llm import LLMClient, default_client
from .logging_config import get_logger

logger = get_logger(__name__)


class StdlibChatModel(BaseChatModel):
    """用标准库 HTTP 客户端实现的 OpenAI 兼容 ChatModel。

    只依赖 ``urllib``，因此不需要安装任何厂商 SDK 即可接入
    任意 OpenAI 兼容服务（本地 vLLM / Ollama / 自建网关等）。
    """

    model: str = ""
    api_base: str = ""
    api_key: str = ""
    timeout: float = 15.0
    temperature: float = 0.0
    max_tokens: int = 1024

    @property
    def _llm_type(self) -> str:
        return "pharmacy-stdlib-chat"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        # 注意：不包含 api_key，避免凭据进入日志/追踪
        return {"model": self.model, "api_base": self.api_base}

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        client = LLMClient(
            api_base=self.api_base,
            api_key=self.api_key,
            model=self.model,
            timeout=self.timeout,
        )
        payload_messages = [
            {
                "role": _role_of(message),
                "content": message.content if isinstance(message.content, str) else str(message.content),
            }
            for message in messages
        ]

        response = client._chat(  # noqa: SLF001 - 故意复用内部统一实现
            payload_messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            parse_json=False,
        )
        if not response.ok:
            # 交给上层捕获并降级；LangChain 期望异常而不是静默空结果
            raise RuntimeError(f"LLM 调用失败：{response.error}")

        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=response.text or ""))]
        )

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any):  # pragma: no cover
        """本适配器不支持工具调用。

        ``LLMGraphTransformer`` 在部分版本下会尝试绑定工具；
        这里显式抛出，以便其上层的 ``ignore_tool_usage`` 分支接管。
        """
        raise NotImplementedError("StdlibChatModel 不支持工具调用")


def _role_of(message: BaseMessage) -> str:
    kind = getattr(message, "type", "") or ""
    return {
        "system": "system",
        "human": "user",
        "ai": "assistant",
        "tool": "tool",
    }.get(kind, "user")


def build_chat_model(client: LLMClient | None = None) -> StdlibChatModel | None:
    """按当前配置构造 ChatModel；未配置 LLM 时返回 None。"""
    source = client if client is not None else default_client
    if not source.available:
        return None
    return StdlibChatModel(
        model=source.model,
        api_base=source.api_base,
        api_key=source.api_key,
        timeout=source.timeout,
    )


def build_graph_transformer(
    allowed_nodes: list[str] | None = None,
    allowed_relationships: list[str] | None = None,
    client: LLMClient | None = None,
):
    """构造 LangChain ``LLMGraphTransformer``。

    返回 ``(transformer, None)`` 或 ``(None, 原因)``；
    缺少依赖或未配置 LLM 时返回原因字符串，调用方据此退化到规则抽取。
    """
    chat_model = build_chat_model(client)
    if chat_model is None:
        return None, "未配置 LLM（LLM_API_BASE / LLM_MODEL），使用规则抽取"

    try:
        from langchain_experimental.graph_transformers import LLMGraphTransformer
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        return None, f"缺少 langchain-experimental，使用规则抽取（{exc}）"

    try:
        transformer = LLMGraphTransformer(
            llm=chat_model,
            allowed_nodes=allowed_nodes or [],
            allowed_relationships=allowed_relationships or [],
            ignore_tool_usage=True,
        )
    except Exception as exc:  # pragma: no cover - 版本差异
        return None, f"LLMGraphTransformer 初始化失败，使用规则抽取（{exc}）"

    return transformer, None
