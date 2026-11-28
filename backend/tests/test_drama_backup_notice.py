from sqlalchemy import delete

from app.plugins.drama.models import DramaAssetImage
from tests.helpers import create_project
from tests.test_drama_look_images import looks as looks


async def test_backup_warns_about_look_history_even_without_selected_file(app, client, looks, system_admin):
    _, pid, series_id, look_id, _ = looks
    other = await create_project(client)
    endpoint = f"/api/v1/projects/{pid}/backup/estimate"
    assert (await client.get(endpoint)).json()["warnings"] == []
    async with app.state.sessionmaker.begin() as db:
        db.add(DramaAssetImage(series_id=series_id, owner_type="look", owner_id=look_id, status="failed"))
    disabled = await system_admin.patch("/api/v1/admin/plugins/drama", json={"revision": 1, "enabled": False})
    assert disabled.is_success, disabled.text
    response = await client.get(endpoint)
    assert response.is_success, response.text
    (notice,) = response.json()["warnings"]
    assert "当前图片提示词随备份保存" in notice and "造型图片文件及图片历史" in notice
    other_estimate = await client.get(f"/api/v1/projects/{other['id']}/backup/estimate")
    assert other_estimate.json()["warnings"] == []
    async with app.state.sessionmaker.begin() as db:
        await db.execute(delete(DramaAssetImage).where(DramaAssetImage.series_id == series_id))
    assert (await client.get(endpoint)).json()["warnings"] == []
