import httpx

from app.main import create_app
from tests.helpers import FakeLLM, make_settings


async def test_spa_fallback(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"ico")

    app = create_app(make_settings(tmp_path, frontend_dist_dir=str(dist)), provider_factory=FakeLLM().factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/")).text == "<html>spa</html>"
        assert (await c.get("/projects/1/chapters")).text == "<html>spa</html>"
        assert (await c.get("/assets/app.js")).text == "console.log(1)"
        assert (await c.get("/favicon.ico")).content == b"ico"
        assert (await c.get("/../../etc/passwd")).text == "<html>spa</html>"
        resp = await c.get("/api/v1/unknown")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/json")
        assert (await c.get("/healthz")).json() == {"status": "ok"}
    await app.state.engine.dispose()
