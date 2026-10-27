import pytest

from app.ai.providers import AIProviderError
from app.ai.providers.anthropic import thinking_kwargs
from app.ai.providers.gemini import thinking_config
from app.models import AIConfig
from app.services import generation_service
from app.services.ai_config_service import context_char_budget, max_output_tokens
from tests.helpers import create_ai_config, register, run_task
from tests.test_chapters import _setup


async def test_defaults_and_roundtrip(client):
    await register(client)
    cfg = await create_ai_config(client)
    assert cfg["thinking_level"] == "off"
    assert cfg["context_window_k"] is None and cfg["max_output_k"] is None

    cfg = await create_ai_config(client, name="B", thinking_level="high", context_window_k=128, max_output_k=16)
    assert (cfg["thinking_level"], cfg["context_window_k"], cfg["max_output_k"]) == ("high", 128, 16)

    resp = await client.patch(f"/api/v1/ai-configs/{cfg['id']}", json={"thinking_level": "low"})
    body = resp.json()
    # 省略的字段保持不变
    assert (body["thinking_level"], body["context_window_k"], body["max_output_k"]) == ("low", 128, 16)

    # 显式 null = 清空
    resp = await client.patch(f"/api/v1/ai-configs/{cfg['id']}", json={"context_window_k": None, "max_output_k": None})
    assert resp.json()["context_window_k"] is None and resp.json()["max_output_k"] is None


@pytest.mark.parametrize(
    "extra",
    [
        {"thinking_level": "ultra"},
        {"context_window_k": 0},
        {"context_window_k": 2001},
        {"max_output_k": 257},
        {"context_window_k": 8, "max_output_k": 8},
    ],
)
async def test_create_validation(client, extra):
    await register(client)
    body = {"name": "x", "provider": "openai_compat", "api_key": "k", "model": "m", **extra}
    assert (await client.post("/api/v1/ai-configs", json=body)).status_code == 422


async def test_update_rejects_output_not_below_context(client):
    await register(client)
    cfg = await create_ai_config(client, context_window_k=32)
    resp = await client.patch(f"/api/v1/ai-configs/{cfg['id']}", json={"max_output_k": 32})
    assert resp.status_code == 400
    assert "最大输出必须小于上下文大小" in resp.json()["detail"]


async def test_list_models_with_form_key(client, fake_llm):
    await register(client)
    fake_llm.models = [{"id": "m-b"}, {"id": "m-a", "name": "Model A"}, {"id": "m-b"}]
    resp = await client.post(
        "/api/v1/ai-configs/models",
        json={"provider": "anthropic", "base_url": "https://relay.example.com/", "api_key": " sk-form "},
    )
    assert resp.status_code == 200
    assert resp.json() == {"models": [{"id": "m-a", "name": "Model A"}, {"id": "m-b", "name": None}]}
    call = fake_llm.list_calls[-1]
    assert call == {
        "provider": "anthropic",
        "api_key": "sk-form",
        "base_url": "https://relay.example.com",
        "timeout": 30,
    }


async def test_list_models_falls_back_to_saved_key(client, make_client, fake_llm):
    await register(client)
    cfg = await create_ai_config(client)
    resp = await client.post(
        "/api/v1/ai-configs/models", json={"provider": "openai_compat", "api_key": "", "config_id": cfg["id"]}
    )
    assert resp.status_code == 200
    assert fake_llm.list_calls[-1]["api_key"] == "sk-test-abcdefgh1234"

    # 他人的配置不可借用 Key
    bob = make_client()
    await register(bob, "bob")
    resp = await bob.post("/api/v1/ai-configs/models", json={"provider": "openai_compat", "config_id": cfg["id"]})
    assert resp.status_code == 404


async def test_list_models_errors(client, fake_llm):
    await register(client)
    fake_llm.models = AIProviderError("模型调用失败：401 Unauthorized")
    resp = await client.post("/api/v1/ai-configs/models", json={"provider": "gemini", "api_key": "k"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "获取模型列表失败：401 Unauthorized"

    # 默认禁止内网地址
    resp = await client.post(
        "/api/v1/ai-configs/models", json={"provider": "openai_compat", "base_url": "http://127.0.0.1:11434/v1"}
    )
    assert resp.status_code == 400
    assert "内网" in resp.json()["detail"]


async def test_list_models_requires_login(client):
    resp = await client.post("/api/v1/ai-configs/models", json={"provider": "gemini"})
    assert resp.status_code == 401


async def test_generation_passes_thinking_and_max_tokens(client, fake_llm):
    _, base, o1, _ = await _setup(client)
    await create_ai_config(client, thinking_level="medium", max_output_k=16)
    ch = (await client.post(f"{base}/chapters", json={"outline_id": o1["id"]})).json()
    fake_llm.push("正文内容")
    resp = await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 1000})
    task, _ = await run_task(client, resp)
    assert task["status"] == "succeeded"
    call = fake_llm.calls[-1]
    assert call["thinking"] == "medium"
    assert call["max_tokens"] == 16000


async def test_connectivity_test_never_thinks(client, fake_llm):
    await register(client)
    cfg = await create_ai_config(client, thinking_level="high", max_output_k=64)
    await client.post(f"/api/v1/ai-configs/{cfg['id']}/test")
    assert fake_llm.calls[-1]["thinking"] == "off"
    assert fake_llm.calls[-1]["max_tokens"] == 16


async def test_chapter_context_budget_from_config(client, fake_llm, monkeypatch):
    _, base, o1, _ = await _setup(client)
    await create_ai_config(client, context_window_k=32, max_output_k=8)
    ch = (await client.post(f"{base}/chapters", json={"outline_id": o1["id"]})).json()
    seen = []
    original = generation_service.build_chapter_context

    async def _spy(db, project, chapter, budget, **kw):
        seen.append(budget)
        return await original(db, project, chapter, budget, **kw)

    monkeypatch.setattr(generation_service, "build_chapter_context", _spy)
    fake_llm.push("正文")
    await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 1000}))
    assert seen == [(32 - 8) * 1000 - 4000]


def _cfg(**kw) -> AIConfig:
    return AIConfig(**{"context_window_k": None, "max_output_k": None, **kw})


def test_context_char_budget():
    assert context_char_budget(_cfg(), 12000) == 12000
    assert context_char_budget(_cfg(context_window_k=128), 12000) == (128 - 8) * 1000 - 4000
    assert context_char_budget(_cfg(context_window_k=200, max_output_k=64), 12000) == 132000
    # 下限保护
    assert context_char_budget(_cfg(context_window_k=8, max_output_k=7), 12000) == 1000
    assert max_output_tokens(_cfg()) is None
    assert max_output_tokens(_cfg(max_output_k=8)) == 8000


@pytest.mark.parametrize(
    ("model", "expected_type"),
    [
        ("claude-opus-5-5", "adaptive"),
        ("claude-sonnet-4-6", "adaptive"),
        ("claude-sonnet-4-5", "enabled"),
        ("claude-sonnet-4-5-20250929", "enabled"),
        ("claude-haiku-4-5", "enabled"),
        ("claude-3-7-sonnet-latest", "enabled"),
        ("claude-sonnet-4-20250514", "enabled"),
    ],
)
def test_anthropic_thinking_mapping(model, expected_type):
    kwargs, limit = thinking_kwargs(model, "high", 8192)
    assert kwargs["thinking"]["type"] == expected_type
    if expected_type == "adaptive":
        assert kwargs["output_config"] == {"effort": "high"} and limit == 8192
    else:
        assert limit > kwargs["thinking"]["budget_tokens"]
    assert thinking_kwargs(model, "off", 8192) == ({}, 8192)


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
async def test_all_levels_accepted(client, level):
    await register(client)
    assert (await create_ai_config(client, thinking_level=level))["thinking_level"] == level


def test_anthropic_xhigh_and_max():
    assert thinking_kwargs("claude-opus-5-5", "max", 8192)[0]["output_config"] == {"effort": "max"}
    assert thinking_kwargs("claude-opus-4-7", "xhigh", 8192)[0]["output_config"] == {"effort": "xhigh"}
    # 4.6 没有 xhigh
    assert thinking_kwargs("claude-sonnet-4-6", "xhigh", 8192)[0]["output_config"] == {"effort": "high"}
    # 旧模型 max 档不超过 Opus 4.1 的 32000 输出上限
    kwargs, limit = thinking_kwargs("claude-opus-4-1", "max", 8192)
    assert kwargs["thinking"]["budget_tokens"] < limit <= 32000


def test_gemini_thinking_mapping():
    assert thinking_config("gemini-2.5-pro", "off") is None
    assert thinking_config("gemini-2.5-flash", "low").thinking_budget == 2048
    cfg = thinking_config("gemini-3-pro-preview", "medium")
    assert cfg.thinking_budget is None and cfg.thinking_level.value == "MEDIUM"
    assert thinking_config("gemini-3-pro-preview", "max").thinking_level.value == "HIGH"
    assert thinking_config("gemini-2.5-pro", "xhigh").thinking_budget == 24576
    assert thinking_config("gemini-2.5-pro", "max").thinking_budget == -1
