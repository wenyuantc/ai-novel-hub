from sqlalchemy import select

from app.ai.providers import AIProviderError
from app.models import AIConfig
from tests.helpers import create_ai_config, register


async def test_api_key_masked_and_encrypted(app, client):
    await register(client)
    cfg = await create_ai_config(client, api_key="sk-test-abcdefgh1234")
    assert cfg["api_key_masked"] == "sk-***1234"
    assert "api_key" not in cfg
    assert cfg["base_url"] == "https://api.example.com/v1"  # 去掉尾部斜杠
    assert cfg["is_default"] is True  # 首条自动默认

    async with app.state.sessionmaker() as db:
        row = await db.scalar(select(AIConfig).where(AIConfig.id == cfg["id"]))
        assert row.api_key_enc != "sk-test-abcdefgh1234"
        assert "sk-test" not in row.api_key_enc
        assert app.state.cipher.decrypt(row.api_key_enc) == "sk-test-abcdefgh1234"

    listed = (await client.get("/api/v1/ai-configs")).json()
    assert listed[0]["api_key_masked"] == "sk-***1234"
    assert "sk-test-abcdefgh1234" not in str(listed)


async def test_update_keeps_key_when_omitted(app, client):
    await register(client)
    cfg = await create_ai_config(client)
    resp = await client.patch(f"/api/v1/ai-configs/{cfg['id']}", json={"model": "gpt-other", "api_key": ""})
    assert resp.status_code == 200
    assert resp.json()["model"] == "gpt-other"
    assert resp.json()["api_key_masked"] == "sk-***1234"

    resp = await client.patch(f"/api/v1/ai-configs/{cfg['id']}", json={"api_key": "sk-new-key-xyz9876"})
    assert resp.json()["api_key_masked"] == "sk-***9876"


async def test_default_unique_per_user(client):
    await register(client)
    a = await create_ai_config(client, name="A")
    b = await create_ai_config(client, name="B", is_default=True)
    listed = {c["id"]: c for c in (await client.get("/api/v1/ai-configs")).json()}
    assert listed[b["id"]]["is_default"] is True
    assert listed[a["id"]]["is_default"] is False

    resp = await client.post(f"/api/v1/ai-configs/{a['id']}/default")
    assert resp.json()["is_default"] is True
    listed = {c["id"]: c for c in (await client.get("/api/v1/ai-configs")).json()}
    assert [c["is_default"] for c in listed.values()].count(True) == 1

    # 删除默认配置后自动提升另一条
    assert (await client.delete(f"/api/v1/ai-configs/{a['id']}")).status_code == 204
    listed = (await client.get("/api/v1/ai-configs")).json()
    assert len(listed) == 1 and listed[0]["is_default"] is True


async def test_connectivity_test(client, fake_llm):
    await register(client)
    cfg = await create_ai_config(client)
    fake_llm.push(["o", "k"])
    resp = await client.post(f"/api/v1/ai-configs/{cfg['id']}/test")
    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert "ok" in body["message"]
    call = fake_llm.calls[-1]
    assert call["api_key"] == "sk-test-abcdefgh1234"
    assert call["model"] == "gpt-test"
    assert call["base_url"] == "https://api.example.com/v1"

    fake_llm.push(AIProviderError("模型调用失败：401 Unauthorized"))
    body = (await client.post(f"/api/v1/ai-configs/{cfg['id']}/test")).json()
    assert body["ok"] is False
    assert "401" in body["message"]


async def test_ai_config_isolation(client, make_client):
    await register(client, "alice")
    cfg = await create_ai_config(client)
    bob = make_client()
    await register(bob, "bob")
    assert (await bob.get("/api/v1/ai-configs")).json() == []
    for method, url in [
        ("GET", f"/api/v1/ai-configs/{cfg['id']}"),
        ("PATCH", f"/api/v1/ai-configs/{cfg['id']}"),
        ("DELETE", f"/api/v1/ai-configs/{cfg['id']}"),
        ("POST", f"/api/v1/ai-configs/{cfg['id']}/test"),
        ("POST", f"/api/v1/ai-configs/{cfg['id']}/default"),
    ]:
        resp = await bob.request(method, url, json={} if method == "PATCH" else None)
        assert resp.status_code == 404, (method, url)
