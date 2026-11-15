"""历史工具结果、原生工具参数与外部回执进入模型前的精准私人投影。"""

import json
from copy import deepcopy

from app.models import AgentChange, AgentMessage
from app.services import personal_projection
from app.services.agent import context


def _payload():
    secret = "MODEL-PRIVATE-MARKER"
    deep_encoded = {"_externalExecution": secret}
    for _ in range(9):
        deep_encoded = json.dumps(deep_encoded)
    return {
        "_BatchOutcomeUnknown": secret,
        "_TaskActorTokenVersion": secret,
        "_WizardActorTokenVersion": secret,
        "_AgentActorTokenVersion": secret,
        "_ExternalExecution": secret,
        "_ExternalSnapshotMac": secret,
        "apiKeyEnc": secret,
        "taskKey": secret,
        "all_reserved": [{key: secret} for key in personal_projection._INTERNAL_FIELDS],
        "encoded": json.dumps({"payload": [{"_SharedEmbedding": secret, "style": "保留"}]}),
        "double_encoded": json.dumps(json.dumps({"_analysisPending": secret})),
        "truncated_private": f'{{"_externalExecution":"{secret}',
        "truncated_double": json.dumps(json.dumps({"_externalExecution": secret}))[:-2],
        "truncated_triple": json.dumps(json.dumps(json.dumps({"_externalExecution": secret})))[:-2],
        "truncated_deep": deep_encoded[:-2],
        "truncated_preview": {"preview": f'{{"guards":[{{"table":"tasks","hash":"{secret}'},
        "truncated_public": '{"story":"作者保留的未完文本',
        "truncated_encoded_public": json.dumps(json.dumps({"story": "作者的公开文字"}))[:-2],
        "text": "角色在故事里提到了 task_key 这个术语。",
        "ordinary": {"style": "古典", "guards": [{"name": "城门守卫"}]},
        "preview": {"description": "保留预览", "guards": [{"table": "tasks", "pk": {"id": 1}, "hash": secret}]},
        "direct_guard": {"guards": [{"plugin_id": 1, "schema_hash": secret, "enabled": True}]},
        "diff": [{"field": "tasks._analysisCompletion", "before": secret, "after": secret},
                 {"field": "tasks._external.snapshot.mac", "before": secret, "after": secret},
                 {"field": "chapters.style", "before": "古典", "after": "清新"}],
    }


def test_model_tool_result_filters_private_aliases_and_preserves_public_values():
    original = _payload()
    snapshot = deepcopy(original)
    result = json.loads(context.tool_result(original))
    assert result["untrusted"] is True
    assert "MODEL-PRIVATE-MARKER" not in json.dumps(result, ensure_ascii=False)
    data = result["data"]
    assert data["ordinary"] == {"style": "古典", "guards": [{"name": "城门守卫"}]}
    assert data["text"] == "角色在故事里提到了 task_key 这个术语。"
    assert data["truncated_public"] == '{"story":"作者保留的未完文本'
    assert data["truncated_encoded_public"] == original["truncated_encoded_public"]
    assert data["preview"] == {"description": "保留预览"}
    assert data["direct_guard"] == {}
    assert data["diff"][-1] == {"field": "chapters.style", "before": "古典", "after": "清新"}
    assert original == snapshot


def test_old_native_tool_history_and_external_receipt_never_replay_private_aliases():
    data = _payload()
    encoded = json.dumps({"untrusted": True, "data": data}, ensure_ascii=False)
    history = AgentMessage(role="tool", tool_call_id="call", tool_name="read", content=encoded)
    assistant = AgentMessage(role="assistant", content="继续", tool_calls=[{
        "id": "call", "name": "read", "arguments": json.dumps(data, ensure_ascii=False),
        "provider_metadata": {"signature": "保留协议签名"},
    }])
    change = AgentChange(kind="external", status="failed", result=deepcopy(data))
    before = deepcopy(change.result)
    values = (context.message_dict(history), context.message_dict(assistant), context.change_result_for_model(change))
    for value in values:
        assert "MODEL-PRIVATE-MARKER" not in json.dumps(value, ensure_ascii=False)
        assert "城门守卫" in json.dumps(value, ensure_ascii=False)
    assert values[1]["tool_calls"][0]["provider_metadata"]["signature"] == "保留协议签名"
    assert change.result == before


def test_truncated_model_tool_result_stays_parseable_and_contains_no_private_value():
    raw = {"public": "正文" * context.RESULT_LIMIT, "nested": {"_ExternalExecution": "MODEL-PRIVATE-MARKER"}}
    result = context.tool_result(raw)
    parsed = json.loads(result)
    assert parsed["untrusted"] is True and "warning" in parsed
    assert len(result) <= context.RESULT_LIMIT and "MODEL-PRIVATE-MARKER" not in result
