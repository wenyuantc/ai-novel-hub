import ipaddress
from types import SimpleNamespace

import pytest

from app.ai.endpoint_guard import ensure_endpoint_allowed, is_blocked_ip
from app.ai.providers import AIProviderError
from app.core.errors import BadRequestError
from app.services import ai_config_service
from tests.helpers import create_ai_config, create_project, register


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.10",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
    ],
)
def test_blocked_addresses(addr):
    assert is_blocked_ip(ipaddress.ip_address(addr))


@pytest.mark.parametrize("addr", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_allowed(addr):
    assert not is_blocked_ip(ipaddress.ip_address(addr))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://localhost:11434/v1",
        "http://[::1]:8000",
        "http://internal.example.test/v1",
        "http://metadata.example.test/latest",
        "https://rebind.example.test/v1",  # 任一解析结果为内网即拒绝
    ],
)
async def test_private_urls_rejected(url, monkeypatch):
    if "localhost" in url:
        from app.ai import endpoint_guard

        async def _resolve(host, port):
            return ["127.0.0.1", "::1"]

        monkeypatch.setattr(endpoint_guard, "_resolve", _resolve)
    with pytest.raises(AIProviderError, match="ALLOW_PRIVATE_AI_ENDPOINTS"):
        await ensure_endpoint_allowed(url)


async def test_public_and_empty_urls_allowed():
    await ensure_endpoint_allowed(None)
    await ensure_endpoint_allowed("")
    await ensure_endpoint_allowed("https://api.example.com/v1")
    await ensure_endpoint_allowed("https://8.8.8.8/v1")


@pytest.mark.parametrize("url", ["ftp://api.example.com", "api.example.com/v1", "http://:80"])
async def test_malformed_urls_rejected(url):
    with pytest.raises(AIProviderError, match="格式不正确"):
        await ensure_endpoint_allowed(url)


async def test_unresolvable_host_rejected():
    with pytest.raises(AIProviderError, match="无法解析"):
        await ensure_endpoint_allowed("https://nxdomain.example.test/v1")


def _cfg(base_url):
    return SimpleNamespace(provider="openai_compat", api_key_enc=None, base_url=base_url)


class _Cipher:
    def decrypt(self, _):
        return "sk-x"


async def test_build_provider_sets_redirect_policy(fake_llm):
    blocked = await ai_config_service.build_provider(
        _cfg("https://api.example.com/v1"), _Cipher(), fake_llm.factory, 10, allow_private=False
    )
    assert blocked.follow_redirects is False

    allowed = await ai_config_service.build_provider(
        _cfg("http://127.0.0.1:11434/v1"), _Cipher(), fake_llm.factory, 10, allow_private=True
    )
    assert allowed.follow_redirects is True


async def test_build_provider_rejects_private_by_default(fake_llm):
    with pytest.raises(BadRequestError, match="ALLOW_PRIVATE_AI_ENDPOINTS"):
        await ai_config_service.build_provider(
            _cfg("http://10.0.0.5/v1"), _Cipher(), fake_llm.factory, 10, allow_private=False
        )


async def test_connectivity_test_reports_blocked_endpoint(client, fake_llm):
    await register(client)
    cfg = await create_ai_config(client, base_url="http://internal.example.test/v1")
    resp = await client.post(f"/api/v1/ai-configs/{cfg['id']}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "ALLOW_PRIVATE_AI_ENDPOINTS" in body["message"]
    assert fake_llm.calls == []


async def test_wizard_rejects_private_endpoint(client, fake_llm):
    await register(client)
    await create_ai_config(client, base_url="http://169.254.169.254/latest")
    project = await create_project(client)
    resp = await client.post(f"/api/v1/projects/{project['id']}/wizard/world", json={})
    assert resp.status_code == 400
    assert "ALLOW_PRIVATE_AI_ENDPOINTS" in resp.json()["detail"]
    assert fake_llm.calls == []


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_private_endpoint_allowed_when_enabled(client, fake_llm):
    await register(client)
    cfg = await create_ai_config(client, base_url="http://internal.example.test/v1")
    resp = await client.post(f"/api/v1/ai-configs/{cfg['id']}/test")
    assert resp.json()["ok"] is True
    assert fake_llm.calls[0]["base_url"] == "http://internal.example.test/v1"
