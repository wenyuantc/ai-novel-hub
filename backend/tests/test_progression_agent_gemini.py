"""真实注册工具经 Gemini SDK 生成的请求与结构化返回。"""

import json

from app.services.agent.tools import get_tool
from tests.test_agent_providers import MESSAGES, SDKTransport, gemini_chunk


async def test_setting_tools_gemini_sdk_wire_schema_and_structured_arguments(monkeypatch):
    identity = {"a_id": 2, "b_id": 3, "type_id": 4}
    base = {
        "timeline_id": 1,
        "expected_revision": 2,
        "operation": "replace",
        "anchor_kind": "chapter",
        "chapter_id": 7,
        "chapter_no": None,
        "boundary": "end",
    }
    inputs = [
        (
            "update_setting_baseline",
            {
                "timeline_id": 1,
                "expected_revision": 2,
                "changes": {"identity": identity, "intimacy": -13, "status": "normal", "exists": True},
                "confirm": False,
            },
        ),
        ("record_setting_progression", {**base, "field_key": "current_state", "value": {"text": "筑基"}}),
        ("record_setting_progression", {**base, "field_key": "intimacy", "value": {"intimacy": -13}}),
        ("record_setting_progression", {**base, "field_key": "stage_id", "value": {"stage_id": 9}}),
        ("record_setting_progression", {**base, "field_key": "identity", "value": {"identity": identity}}),
        ("record_setting_progression", {**base, "field_key": "presence", "value": {"presence": {"exists": False}}}),
        (
            "record_setting_progression",
            {
                **base,
                "field_key": "presence",
                "value": {
                    "presence": {
                        "exists": True,
                        "state": {"identity": identity, "intimacy": 7, "status": "normal", "description": "师徒"},
                    }
                },
            },
        ),
    ]
    parts = [{"functionCall": {"name": name, "args": args}} for name, args in inputs]
    sdk = SDKTransport(monkeypatch, "gemini", [[gemini_chunk(parts, "STOP")]])
    tools = []
    for name in ("list_setting_timelines", "update_setting_baseline", "record_setting_progression"):
        tool = get_tool(name)
        tools.append({"name": tool.name, "description": tool.description, "parameters": tool.parameters})
    events = [event async for event in sdk.provider.chat_tools_stream(MESSAGES, "test-model", tools)]
    declarations = {item["name"]: item for item in sdk.requests[0]["tools"][0]["functionDeclarations"]}

    def assert_supported(node):
        assert node.get("type") in {"OBJECT", "STRING", "INTEGER", "BOOLEAN"}, node
        assert "anyOf" not in node
        if node["type"] == "OBJECT":
            assert node.get("properties"), node
            for child in node["properties"].values():
                assert_supported(child)

    for declaration in declarations.values():
        assert_supported(declaration["parameters"])
    timeline = declarations["list_setting_timelines"]["parameters"]["properties"]
    assert set(timeline["subject_kind"]["enum"]) == {"character", "world", "relationship", "career", "membership"}
    baseline = declarations["update_setting_baseline"]["parameters"]["properties"]["changes"]["properties"]
    assert set(baseline["life_status"]["enum"]) == {"alive", "dead", "missing", "other"}
    value = declarations["record_setting_progression"]["parameters"]["properties"]["value"]["properties"]
    assert value["intimacy"]["minimum"] == -100 and value["intimacy"]["maximum"] == 100
    assert value["stage_id"]["nullable"] is True
    identity_schema = value["identity"]["properties"]
    assert set(identity_schema) == {"a_id", "b_id", "type_id"}
    state = value["presence"]["properties"]["state"]
    assert state["nullable"] is True and state["properties"]["identity"]["type"] == "OBJECT"

    calls = [payload for kind, payload in events if kind == "tool_call"]
    assert [(call["name"], json.loads(call["arguments"])) for call in calls] == inputs
    assert type(json.loads(calls[2]["arguments"])["value"]["intimacy"]) is int
    assert json.loads(calls[5]["arguments"])["value"]["presence"]["exists"] is False
    assert all(client.is_closed for client in sdk.clients)
