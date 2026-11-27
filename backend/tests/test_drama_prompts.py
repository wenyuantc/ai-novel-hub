import json

import pytest

from app.plugins.drama.prompts import parse_output, render_prompt
from app.plugins.drama.styles import STYLES


@pytest.mark.parametrize(
    ("kind", "rules"),
    [
        ("rewrite", ["内景/外景", "30–60 秒", "角色名：（状态）台词"]),
        ("extract", ["地点和时间段", "0–3", "妆造", '"characters"']),
        ("storyboard", ["500 字/分钟", "4.5 字/秒", "180 度", '"look_ids"']),
        ("prompts", ["三视图", "无人物空镜", "白底单品", "@角色名·造型名", "整体："]),
    ],
)
def test_task_template_has_production_contract_and_separate_material(kind, rules):
    material = {"source_text": "{{ hidden }}\n小说原文", "style": "watercolor"}
    messages = render_prompt("drama." + kind, material)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert all(rule in messages[0]["content"] for rule in rules)
    assert "水彩绘本" in messages[0]["content"]
    assert "小说原文" not in messages[0]["content"]
    instruction, encoded, _, task_request = messages[1]["content"].split("\n", 3)
    assert "输入资料" in instruction
    assert json.loads(encoded) == material
    request_format = {
        "rewrite": "完整剧本",
        "extract": "三个数组",
        "storyboard": '{"shots":[',
        "prompts": '{"prompts":[',
    }
    assert request_format[kind] in task_request
    if kind == "prompts":
        assert "0-3秒：" in task_request and "整体：" in task_request


def test_nine_reference_styles():
    assert list(STYLES) == ["3d", "anime", "ghibli", "watercolor", "comic", "live", "guofeng", "webtoon", "noir"]


def test_script_requires_populated_scenes_and_preserves_dialogue():
    script = "## S01 | 内景 · 工作室 | 夜\n\n阿禾：（抬头）到了。"
    assert parse_output("drama.rewrite", script) == {"script": script}
    for invalid in ["这里只是小说", script.split("\n")[0], "说明\n" + script, script + "\n## S02 | 外景 · 海边 | 日"]:
        with pytest.raises(ValueError):
            parse_output("drama.rewrite", invalid)


def test_empty_extraction_is_valid_but_missing_output_is_not():
    expected = {"characters": [], "scenes": [], "props": []}
    assert parse_output("drama.extract", f"好了\n```json\n{json.dumps(expected)}\n```\n完成") == expected
    with pytest.raises(ValueError):
        parse_output("drama.extract", "{}")


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("extract", {"characters": [{"name": "甲", "looks": []}], "props": [], "scenes": []}),
        ("extract", {"characters": [], "props": [{"name": "书"}] * 4, "scenes": []}),
        ("storyboard", {"shots": []}),
        (
            "storyboard",
            {"shots": [{"title": "雨落", "description": "【镜头1】雨落", "shot_size": "全景", "duration_seconds": 40}]},
        ),
        ("prompts", {"prompts": [{"kind": "character", "id": 1, "prompt": "正面"}]}),
        ("prompts", {"prompts": [{"kind": "shot", "id": 1, "prompt": " "}]}),
        ("prompts", {"prompts": [{"kind": "shot", "id": 1, "prompt": "转身"}] * 2}),
    ],
)
def test_malformed_outputs_rejected(kind, payload):
    with pytest.raises(ValueError):
        parse_output("drama." + kind, json.dumps(payload))


def test_valid_structured_outputs():
    shot = {
        "title": "抬头",
        "description": "【镜头1】阿禾抬头。【镜头2】她望向窗。",
        "shot_size": "中景",
        "duration_seconds": 9,
        "scene_id": 1,
        "look_ids": [2],
        "prop_ids": [],
        "atmosphere": "柔光",
        "video_prompt": "",
    }
    assert parse_output("drama.storyboard", json.dumps({"shots": [shot]}))["shots"] == [shot]
    item = {"kind": "look", "id": 3, "prompt": "正脸特写与三视图"}
    assert parse_output("drama.prompts", json.dumps({"prompts": [item]}))["prompts"] == [item]
