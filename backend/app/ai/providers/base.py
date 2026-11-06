from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, TypedDict

from app.ai.tools import AgentEvent, AgentMessage, ToolChoice, ToolSpec

TOOLS_UNSUPPORTED = "当前模型或接口不支持工具调用，请在「模型配置」中换用支持工具调用（function calling）的模型"


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


# off = 不发送任何思考参数（默认会思考的模型按其默认行为）
ThinkingLevel = Literal["off", "low", "medium", "high", "xhigh", "max"]


class ModelInfo(TypedDict):
    id: str
    name: str | None


class AIProviderError(Exception):
    """模型调用失败（网络、鉴权、配额等），message 为可直接展示给用户的中文说明。"""


class RequestPolicyError(AIProviderError):
    """受控请求被本地策略拒绝，不应归类为网络故障或模型不支持。"""


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime_type: str


class LLMProvider(ABC):
    # 禁止访问私网时由 build_provider 置为 False，防止重定向跳回内网
    follow_redirects: bool = True
    # 三种实际 SDK 均在 HTTPX 每次发送前执行策略钩子；测试替身由服务层校验。
    uses_dispatch_hooks: bool = False

    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 300.0):
        self.api_key = api_key
        self.base_url = base_url or None
        self.timeout = timeout

    @abstractmethod
    def generate_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingLevel = "off",
    ) -> AsyncIterator[str]:
        """流式返回增量正文文本（不含思考内容）。实现为 async generator。失败时抛 AIProviderError。"""

    async def list_models(self) -> list[ModelInfo]:
        """列出接口可用模型。失败时抛 AIProviderError。"""
        raise AIProviderError("该接口类型不支持获取模型列表")

    async def generate_image(self, prompt: str, model: str) -> GeneratedImage:
        raise AIProviderError(
            "该接口类型不支持图片生成，请在「模型配置」中换用 OpenAI 兼容或 Gemini 的图片模型"
        )

    async def chat_tools_stream(
        self,
        messages: list[AgentMessage],
        model: str,
        tools: list[ToolSpec],
        *,
        tool_choice: ToolChoice = "auto",
        max_tokens: int | None = None,
        thinking: ThinkingLevel = "off",
        temperature: float | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """完整调用参数才产出 tool_call；默认不支持，绝不退化成文本模拟工具。"""
        raise AIProviderError(TOOLS_UNSUPPORTED)
        yield  # 维持 async generator 协议；异常在开始消费时抛出

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """对每条文本求向量（顺序与输入一致）。调用方负责分批与截断。失败时抛 AIProviderError。"""
        raise AIProviderError("该提供方不支持向量模型")


def split_system(messages: list[ChatMessage]) -> tuple[str, list[ChatMessage]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    return system, rest


def describe_error(exc: Exception) -> str:
    # OpenAI / Anthropic SDK 会把请求钩子抛出的异常包装为 APIConnectionError。
    # 恢复本地的受控错误，不把它翻译成可自动重试的网络失败。
    cause: BaseException | None = exc
    for _ in range(8):
        if isinstance(cause, RequestPolicyError):
            raise cause from None
        if cause is None:
            break
        cause = cause.__cause__
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > 500:
        text = text[:500] + "…"
    return f"模型调用失败：{text}"


def describe_tools_error(exc: Exception) -> str:
    """工具能力拒绝给出换模型提示；参数、历史协议及其他失败保留具体原因。"""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    text = str(exc).lower()
    mentions_tools = "tool" in text or "function" in text
    capability_rejected = any(
        word in text
        for word in (
            "unsupported",
            "not support",
            "not available",
            "not implemented",
            "unrecognized request argument",
            "unknown parameter",
        )
    )
    invalid_payload = any(word in text for word in ("schema", "tool_call_id", "tool_use_id", "tool_result"))
    if mentions_tools and capability_rejected and not invalid_payload and status in (None, 400, 404, 422):
        return TOOLS_UNSUPPORTED
    return describe_error(exc)
