from dataclasses import replace

import pytest
from fastapi import APIRouter

from app.plugins import load
from tests.plugins.sample_plugin import SAMPLE, Handler


def test_registry_isolated_and_reverse_lookup(monkeypatch):
    monkeypatch.setattr("app.plugins.builtins.REGISTERED", ())
    registry = load((SAMPLE,))
    assert registry.get("example") == SAMPLE
    assert registry.plugin_of_kind("example.render") == "example"
    assert not load().all()


@pytest.mark.parametrize(
    "manifests, error",
    [
        ((SAMPLE, SAMPLE), "example"),
        ((replace(SAMPLE, id="ABC"),), "ABC"),
        ((replace(SAMPLE, name=" " * 50 + "A"),), "元数据"),
        ((replace(SAMPLE, version=" " * 32 + "v"),), "元数据"),
        ((replace(SAMPLE, id="wizard", routers=(), task_handlers=(
            type("WizardHandler", (), {"kinds": ("wizard.future",)})(),)),), "wizard.future"),
        ((replace(SAMPLE, routers=(APIRouter(prefix="/plugins/example2"),)),), "example2"),
        ((replace(SAMPLE, task_handlers=(Handler(), Handler())),), "example.render"),
        (
            (replace(SAMPLE, task_handlers=(type("Handler", (), {"kinds": ("chapter.generate",)})(),)),),
            "chapter.generate",
        ),
    ],
)
def test_invalid_manifest_fails_startup(manifests, error):
    with pytest.raises(ValueError, match=error):
        load(manifests)


def test_nested_http_routes_are_supported_and_plain_routes_rejected():
    router = APIRouter(prefix="/plugins/example")
    nested = APIRouter(prefix="/nested")

    async def read():
        return {}

    nested.get("/read")(read)
    router.include_router(nested)
    assert load((replace(SAMPLE, routers=(router,)),)).get("example")
    router.add_route("/plain", read)
    with pytest.raises(ValueError, match="FastAPI"):
        load((replace(SAMPLE, routers=(router,)),))
