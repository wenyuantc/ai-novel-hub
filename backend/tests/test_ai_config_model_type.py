from app.ai.providers import AIProviderError
from tests.helpers import create_ai_config, create_project, register


async def _list(client) -> dict[int, dict]:
    return {c["id"]: c for c in (await client.get("/api/v1/ai-configs")).json()}


async def test_model_type_defaults_to_text(client):
    await register(client)
    cfg = await create_ai_config(client)
    assert cfg["model_type"] == "text"


async def test_default_unique_per_model_type(client):
    await register(client)
    text_a = await create_ai_config(client, name="文本A")
    img_a = await create_ai_config(client, name="图片A", model_type="image", model="gpt-image-1")
    # 各类型的第一条都自动成为默认
    assert text_a["is_default"] is True and img_a["is_default"] is True

    img_b = await create_ai_config(client, name="图片B", model_type="image", is_default=True)
    listed = await _list(client)
    assert listed[text_a["id"]]["is_default"] is True  # 不影响文本默认
    assert listed[img_a["id"]]["is_default"] is False
    assert listed[img_b["id"]]["is_default"] is True

    await client.post(f"/api/v1/ai-configs/{img_a['id']}/default")
    listed = await _list(client)
    assert listed[text_a["id"]]["is_default"] is True
    assert [listed[i]["is_default"] for i in (img_a["id"], img_b["id"])] == [True, False]

    # 删除图片默认 → 提升另一条图片配置，而不是文本配置
    await client.delete(f"/api/v1/ai-configs/{img_a['id']}")
    listed = await _list(client)
    assert listed[img_b["id"]]["is_default"] is True
    assert listed[text_a["id"]]["is_default"] is True


async def test_change_type_moves_default(client):
    await register(client)
    text_a = await create_ai_config(client, name="文本A")
    text_b = await create_ai_config(client, name="文本B")
    img = await create_ai_config(client, name="图片", model_type="image")

    # 文本默认改为图片：不抢占图片已有默认，文本侧提升另一条
    resp = await client.patch(f"/api/v1/ai-configs/{text_a['id']}", json={"model_type": "image"})
    assert resp.status_code == 200
    assert resp.json() == resp.json() | {"model_type": "image", "is_default": False}
    listed = await _list(client)
    assert listed[img["id"]]["is_default"] is True
    assert listed[text_b["id"]]["is_default"] is True

    # 改到尚无配置的类型时自动成为默认
    await client.delete(f"/api/v1/ai-configs/{text_b['id']}")
    resp = await client.patch(f"/api/v1/ai-configs/{text_a['id']}", json={"model_type": "text"})
    assert resp.json()["is_default"] is True


async def test_generation_rejects_image_config(client, fake_llm):
    await register(client)
    img = await create_ai_config(client, name="图片", model_type="image")
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}/wizard/outline"

    # 只有图片模型时，未指定配置也不会误用它
    resp = await client.post(base)
    assert resp.status_code == 400
    assert "尚未配置文本模型" in resp.text

    await create_ai_config(client, name="文本")
    resp = await client.post(base, json={"ai_config_id": img["id"]})
    assert resp.status_code == 400
    assert "不是文本模型" in resp.text
    assert fake_llm.calls == []


async def test_image_connectivity_uses_model_list(client, fake_llm):
    await register(client)
    img = await create_ai_config(client, name="图片", model_type="image", model="gpt-image-1")
    fake_llm.models = [{"id": "gpt-image-1"}]
    body = (await client.post(f"/api/v1/ai-configs/{img['id']}/test")).json()
    assert body["ok"] is True and "gpt-image-1" in body["message"]
    assert fake_llm.calls == []  # 不发起生成请求

    fake_llm.models = [{"id": "other"}]
    body = (await client.post(f"/api/v1/ai-configs/{img['id']}/test")).json()
    assert body["ok"] is True and "未找到" in body["message"]

    fake_llm.models = AIProviderError("获取失败：401")
    body = (await client.post(f"/api/v1/ai-configs/{img['id']}/test")).json()
    assert body["ok"] is False and "401" in body["message"]
