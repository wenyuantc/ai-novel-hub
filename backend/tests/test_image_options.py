import base64
from io import BytesIO

import httpx
import pytest
from openai import AsyncOpenAI
from PIL import Image
from sqlalchemy import select

from app.ai.providers.base import AIProviderError, GeneratedImage, ImageInput, ImageOptions
from app.ai.providers.gemini import GeminiProvider
from app.ai.providers.openai_compat import OpenAICompatProvider
from app.ai.references import normalize_images
from app.core.errors import AppError, BadRequestError
from app.models import AIConfig, AiUsageCall, User
from app.services import ai_usage_storage
from app.services.authorization_service import AuthenticatedActor
from app.services.generated_media import _verify_ratio, generate_stored_image
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_cover_ai import _invite, _sequence_server
from tests.test_image_providers import gemini_provider, image_response, openai_provider


def png(size, color="red"):
    stream = BytesIO()
    Image.new("RGB", size, color).save(stream, "PNG")
    return stream.getvalue()


def test_normalize_images_scales_deduplicates_and_limits():
    ref = ImageInput(png((2000, 1000)), "image/png")
    images = normalize_images((ref, ref), max_count=1)
    assert len(images) == 1 and images[0].mime_type == "image/jpeg"
    with Image.open(BytesIO(images[0].data)) as image:
        assert image.size == (1536, 768)
    with pytest.raises(AIProviderError, match="参考图"):
        normalize_images((ref, ImageInput(png((10, 8)), "image/png")), max_count=1)
    with pytest.raises(AIProviderError, match="参考图"):
        normalize_images((ImageInput(b"bad", "image/jpeg"),))


@pytest.mark.parametrize(("ratio", "size"), [("16:9", "1536x864"), ("9:16", "864x1536"),
    ("1:1", "1024x1024"), ("4:3", "1536x1152"), ("3:4", "1152x1536")])
async def test_openai_gpt_image_2_supported_sizes(monkeypatch, ratio, size):
    provider, calls, _ = openai_provider(monkeypatch, [(200, image_response())])
    await provider.generate_image("场景", "gpt-image-2", options=ImageOptions(aspect_ratio=ratio))
    assert calls[0]["size"] == size


async def test_openai_edit_uses_multiple_files(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=image_response())

    client = AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    provider = OpenAICompatProvider(api_key="test")
    monkeypatch.setattr(provider, "_client", lambda: client)
    refs = tuple(ImageInput(png((4, 4), color), "image/png") for color in ("red", "blue"))
    await provider.generate_image("场景", "gpt-image-1", options=ImageOptions("1:1", refs))
    assert len(calls) == 1 and calls[0].url.path.endswith("/images/edits")
    assert calls[0].content.count(b"Content-Type: image/jpeg") == 2
    assert b"1024x1024" in calls[0].content


async def test_gemini_native_reference_and_ratio(monkeypatch):
    data = png((160, 90))
    response = {"candidates": [{"content": {"parts": [{"inlineData": {
        "mimeType": "image/png", "data": base64.b64encode(data).decode()}}]}}]}
    provider, calls, _ = gemini_provider(monkeypatch, response)
    options = ImageOptions("16:9", (ImageInput(png((5, 5)), "image/png"),))
    assert (await provider.generate_image("场景", "gemini-image", options=options)).data == data
    body = calls[0][1]
    assert body["generationConfig"]["imageConfig"]["aspectRatio"] == "16:9"
    parts = body["contents"][0]["parts"]
    inline = parts[1]["inlineData"]
    assert parts[0]["text"] == "场景" and inline["mime_type"] == "image/jpeg"
    assert base64.urlsafe_b64decode(inline["data"]).startswith(b"\xff\xd8")


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_project_reference_image_stored_and_attributed_to_model_owner(app, client, make_client):
    owner = await register(client)
    guest_client = make_client()
    guest = await register(guest_client, "image-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    membership = await _invite(client, guest_client, base, "image-editor", allow_owner_ai=True)
    data = png((160, 90))
    response = {"created": 1, "data": [{"b64_json": base64.b64encode(data).decode()}]}
    async with _sequence_server([(200, response)]) as (url, calls):
        config = await create_ai_config(client, base_url=f"{url}/v1/", model_type="image", model="gpt-image-2")

        async def generate():
            async with app.state.sessionmaker() as db:
                user = await db.get(User, guest["id"])
                db.info["authenticated_actor"] = AuthenticatedActor(user.id, user.token_version)
                options = ImageOptions("16:9", (ImageInput(png((16, 10)), "image/png"),))
                return await generate_stored_image(
                    db, user.id, project["id"], "海上日出", options=options, cipher=app.state.cipher,
                    ai_timeout=10, allow_private=True,
                )

        image = await generate()
        assert len(calls) == 1 and b"1536x864" in calls[0]
        assert image.status == "ready" and (image.width, image.height) == (160, 90)
        assert image.owner_user_id == owner["id"] and image.project_id == project["id"]
        assert (await guest_client.get(f"/api/v1/media/{image.id}")).content == data
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, config["id"])
            await ai_usage_storage.set_price(db, cfg, currency="CNY", image_price_nano=2_000_000_000)
            await ai_usage_storage.set_budget(db, project_id=project["id"], currency="CNY",
                                              limit_nano=1_000_000_000, threshold_percent=80)
            await db.commit()
        with pytest.raises(BadRequestError, match="预算"):
            await generate()
        revoked = await client.delete(f"{base}/collaborators/{guest['id']}",
                                      params={"expected_revision": membership["revision"]})
        assert revoked.status_code == 204
        with pytest.raises(AppError) as denied:
            await generate()
        assert denied.value.status_code in (403, 404) and len(calls) == 1

    async with app.state.sessionmaker() as db:
        call = (await db.scalars(select(AiUsageCall))).one()
        assert call.image_count == 1 and call.status == "succeeded"
        assert call.actor_id == guest["id"] and call.payer_id == owner["id"] and call.project_id == project["id"]


async def test_openai_explicit_size_never_retried_with_default(monkeypatch):
    provider, calls, http = openai_provider(monkeypatch, [
        (400, {"error": {"message": "Invalid size", "type": "invalid_request_error"}}),
    ])
    with pytest.raises(AIProviderError, match="不支持所选画幅"):
        await provider.generate_image("场景", "custom-image", options=ImageOptions("4:3"))
    assert len(calls) == 1 and http.is_closed


async def test_old_openai_model_rejects_unrepresentable_ratio(monkeypatch):
    provider = OpenAICompatProvider(api_key="test")
    monkeypatch.setattr(provider, "_client", lambda: pytest.fail("unsupported request sent"))
    with pytest.raises(AIProviderError, match="不支持所选画幅"):
        await provider.generate_image("场景", "gpt-image-1", options=ImageOptions("16:9"))


@pytest.mark.parametrize(("cls", "model"), [(GeminiProvider, "imagen-4"), (OpenAICompatProvider, "dall-e-3")])
async def test_unsupported_references_rejected_before_send(monkeypatch, cls, model):
    provider = cls(api_key="test")
    monkeypatch.setattr(provider, "_client", lambda: pytest.fail("unsupported request sent"))
    with pytest.raises(AIProviderError, match="不支持参考图"):
        options = ImageOptions(references=(ImageInput(png((5, 5)), "image/png"),))
        await provider.generate_image("场景", model, options=options)


def test_verify_generated_raster_preserves_model_native_dimensions():
    gemini = GeneratedImage(png((1184, 864)), "image/png")
    _verify_ratio(gemini, "4:3", "gemini")
    with pytest.raises(AIProviderError, match="画幅"):
        _verify_ratio(gemini, "16:9", "gemini")
    with pytest.raises(AIProviderError, match="画幅"):
        _verify_ratio(gemini, "4:3", "openai_compat")


async def test_compat_endpoint_rejecting_edit_reports_capability(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(400, json={"error": {"message": "This model does not support image edits."}})

    client = AsyncOpenAI(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    provider = OpenAICompatProvider(api_key="test")
    monkeypatch.setattr(provider, "_client", lambda: client)
    options = ImageOptions(references=(ImageInput(png((4, 4)), "image/png"),))
    with pytest.raises(AIProviderError, match="不支持参考图"):
        await provider.generate_image("场景", "custom-image", options=options)
    assert len(calls) == 1 and calls[0].endswith("/images/edits")
