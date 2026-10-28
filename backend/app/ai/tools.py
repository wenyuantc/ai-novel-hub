"""智能体工具协议；provider_metadata 仅供服务端持久化并回传原生签名块。"""

import json
from typing import Literal, NotRequired, TypedDict


class ToolSpec(TypedDict):
    name: str
    description: str
    parameters: dict


class ToolCall(TypedDict):
    id: str
    name: str
    arguments: str
    # 每条 assistant 响应仅在首个调用上保存完整原生块，避免丢失签名及块顺序。
    provider_metadata: NotRequired[dict]


class AgentMessage(TypedDict, total=False):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall]
    tool_call_id: str
    name: str


ToolChoice = Literal["auto", "none"]
AgentEvent = tuple[Literal["text", "tool_call", "usage", "finish"], dict]


def native_metadata(message: AgentMessage, provider: str) -> dict:
    """只恢复当前 provider 的协议元数据，切换模型提供方时重新构造通用消息。"""
    for call in message.get("tool_calls", []):
        metadata = call.get("provider_metadata", {})
        if metadata.get("provider") == provider:
            return metadata
    return {}


def history_arguments(raw: str) -> dict:
    """把历史中的参数原文转成对象。其他 provider 产生的非法 JSON 不能让之后每轮都失败；
    该调用当时已收到「参数必须是合法的 JSON 对象」的工具结果，这里只需保持协议结构。"""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
