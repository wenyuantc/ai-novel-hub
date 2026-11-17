"""仅过滤人工设定投影的图片字段，不改写普通分析内容。"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from app.models import AgentConversation, AgentMessage, AIConfig, Project
from app.schemas.analysis import AnalysisOut
from app.schemas.organization import OrganizationOut
from app.schemas.setting_image import ThumbnailMeta
from app.schemas.story import CharacterWithOrigin
from app.services.agent import context
from app.services.agent.tools.base import serialized
from tests.helpers import create_ai_config, create_project, register


def _analysis():
    return AnalysisOut(
        id=1, chapter_id=1, chapter_no=1, task_id=None, model="test", scores={"thumbnail": "评分内容"},
        metrics={}, created_at=datetime(2026, 1, 1, tzinfo=UTC), summary="摘要",
        plot_points=[{"thumbnail": "情节点内容"}], hooks=[], conflict={}, emotion={},
        changes_raw={"nested": {"thumbnail_editable": "作者内容"}}, foreshadow_candidates=[],
        suggestions=[], warnings=[], changes=[],
    )


def _character():
    return CharacterWithOrigin(
        id=1, project_id=1, name="角色", role_type="", profile="", relations_text="", aliases=[],
        age="", gender="", appearance="", personality="", background="", current_state="",
        life_status="alive", always_in_context=False, sort=0,
        thumbnail=ThumbnailMeta(version="private-resource-id", mime_type="image/webp", width=20, height=20),
        thumbnail_editable=True,
    )


def _organization():
    return OrganizationOut(
        id=2, project_id=1, name="组织", org_type="", purpose="", location="", power_level=None,
        parent_id=None, description="", sort=0,
        thumbnail=ThumbnailMeta(version="private-resource-id", mime_type="image/webp", width=20, height=20),
    )


class _Wrapper(BaseModel):
    characters: list[CharacterWithOrigin]
    organizations: dict[str, OrganizationOut]
    analysis: AnalysisOut


def test_tool_serialization_only_removes_thumbnail_from_known_entity_projections():
    analysis = _analysis()
    wrapper = _Wrapper(characters=[_character()], organizations={"one": _organization()}, analysis=analysis)
    result = serialized(wrapper)
    assert "thumbnail" not in result["characters"][0]
    assert "thumbnail_editable" not in result["characters"][0]
    assert "thumbnail" not in result["organizations"]["one"]
    assert result["analysis"] == analysis.model_dump(mode="json")
    assert serialized({"thumbnail": "合法普通内容", "nested": {"thumbnail_editable": "合法内容"}}) == {
        "thumbnail": "合法普通内容", "nested": {"thumbnail_editable": "合法内容"},
    }


@pytest.mark.parametrize("tool_name", ["list_organizations", "list_careers", "list_characters", "list_world_settings"])
def test_entity_list_history_removes_only_outer_thumbnail_metadata(tool_name):
    original = {"untrusted": True, "data": {"items": [{
        "id": 3, "name": "角色", "thumbnail": {"version": "private-resource-id"},
        "thumbnail_editable": True, "user_data": {"thumbnail": "普通嵌套内容"},
    }], "total": 1}}
    content = json.dumps(original, ensure_ascii=False)
    row = AgentMessage(role="tool", tool_name=tool_name, tool_call_id="legacy", content=content)
    result = json.loads(context.message_dict(row)["content"])["data"]
    assert "thumbnail" not in result["items"][0] and "thumbnail_editable" not in result["items"][0]
    assert result["items"][0]["user_data"]["thumbnail"] == "普通嵌套内容"
    assert row.content == content


def test_entity_history_uses_original_tool_name_and_hides_unverifiable_fragments():
    value = {"untrusted": True, "data": {"id": 1, "name": "组织", "thumbnail": {"version": "private"}}}
    row = AgentMessage(role="tool", tool_name="unknown", tool_call_id="old", content=json.dumps(value))
    safe = json.loads(context.message_dict(row, expected_tool_name="get_organization")["content"])["data"]
    assert safe["name"] == "组织" and "thumbnail" not in safe
    row.content = '{"untrusted": true, "data": "截断的 thumbnail 私密片段"}'
    safe = json.loads(context.message_dict(row, expected_tool_name="get_organization")["content"])["data"]
    assert "私密片段" not in json.dumps(safe, ensure_ascii=False)


def test_analysis_history_keeps_same_named_user_json():
    analysis = _analysis().model_dump(mode="json")
    row = AgentMessage(role="tool", tool_name="get_chapter_analysis", tool_call_id="analysis",
                       content=context.tool_result({"analysis": analysis}))
    result = json.loads(context.message_dict(row)["content"])["data"]
    assert result["analysis"] == analysis


async def test_actual_model_history_replay_filters_old_image_projection_but_keeps_analysis(client, app, fake_llm):
    owner = await register(client)
    project = await create_project(client)
    config = await create_ai_config(client)
    org = _organization().model_dump(mode="json")
    analysis = _analysis().model_dump(mode="json")
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        db.add(AgentMessage(conversation_id=conversation.id, role="user", content="继续创作"))
        for name, ident, data in (
            ("list_organizations", "old-org", {"items": [org], "page": 1, "pages": 1, "total": 1}),
            ("get_chapter_analysis", "old-analysis", {"analysis": analysis}),
        ):
            db.add(AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
                "id": ident, "name": name, "arguments": "{}",
            }]))
            db.add(AgentMessage(conversation_id=conversation.id, role="tool", tool_name=name, tool_call_id=ident,
                                content=context.tool_result(data)))
        await db.flush()
        messages, _ = await context.build_messages(
            db, conversation, await db.get(Project, project["id"]), await db.get(AIConfig, config["id"]),
            fake_llm.factory("openai_compat", "test", None, 30),
        )
    results = {message["tool_call_id"]: json.loads(message["content"])["data"] for message in messages
               if message.get("tool_call_id") in {"old-org", "old-analysis"}}
    assert set(results) == {"old-org", "old-analysis"}
    assert "thumbnail" not in results["old-org"]["items"][0]
    assert results["old-analysis"]["analysis"] == analysis
    assert "private-resource-id" not in json.dumps(messages, ensure_ascii=False)
