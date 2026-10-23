import re
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from app.ai.providers.base import (
    AIProviderError,
    ChatMessage,
    LLMProvider,
    ModelInfo,
    ThinkingLevel,
    describe_error,
    split_system,
)

DEFAULT_MAX_TOKENS = 8192

# 4.5 及更早的模型不支持 adaptive thinking，只能用 budget_tokens
_LEGACY_THINKING_MODEL = re.compile(r"claude-3|claude-(opus|sonnet|haiku)-4(-[0-5])?(-\d{8})?$")
# max 档受 Opus 4 / 4.1 输出上限 32000 约束：budget + 正文预留 ≤ 32000
_LEGACY_THINKING_BUDGET = {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 24576, "max": 27648}
# budget_tokens 必须小于 max_tokens，额外留给正文的空间
_LEGACY_ANSWER_RESERVE = 4096
# xhigh 自 4.7 起才有，4.6 降为 high
_NO_XHIGH_MODEL = re.compile(r"claude-(opus|sonnet|haiku)-4-6")


def thinking_kwargs(model: str, thinking: ThinkingLevel, max_tokens: int) -> tuple[dict, int]:
    """返回 (额外请求参数, 实际 max_tokens)。off 不发送任何思考参数。"""
    if thinking == "off":
        return {}, max_tokens
    if _LEGACY_THINKING_MODEL.search(model):
        budget = _LEGACY_THINKING_BUDGET[thinking]
        return (
            {"thinking": {"type": "enabled", "budget_tokens": budget}},
            max(max_tokens, budget + _LEGACY_ANSWER_RESERVE),
        )
    effort = "high" if thinking == "xhigh" and _NO_XHIGH_MODEL.search(model) else thinking
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}, max_tokens


class AnthropicProvider(LLMProvider):
    def _client(self) -> AsyncAnthropic:
        return AsyncAnthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            http_client=DefaultAsyncHttpxClient(follow_redirects=self.follow_redirects),
        )

    async def generate_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingLevel = "off",
    ) -> AsyncIterator[str]:
        client = self._client()
        system, rest = split_system(messages)
        kwargs, limit = thinking_kwargs(model, thinking, max_tokens or DEFAULT_MAX_TOKENS)
        if system:
            kwargs["system"] = system
        # 开启思考时不能自定义 temperature
        if temperature is not None and thinking == "off":
            kwargs["temperature"] = temperature
        try:
            async with client.messages.stream(
                model=model,
                max_tokens=limit,
                messages=[{"role": m["role"], "content": m["content"]} for m in rest],
                **kwargs,
            ) as stream:
                # text_stream 只包含正文，不含 thinking 块
                async for text in stream.text_stream:
                    if text:
                        yield text
        except AIProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc
        finally:
            await client.close()

    async def list_models(self) -> list[ModelInfo]:
        client = self._client()
        try:
            return [
                ModelInfo(id=m.id, name=getattr(m, "display_name", None) or None)
                async for m in client.models.list()
                if m.id
            ]
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc
        finally:
            await client.close()
