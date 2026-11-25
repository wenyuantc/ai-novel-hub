import json

import pytest

from app.core.errors import BadRequestError
from app.schemas.reader import ReaderCastingRequest
from app.services.reader_casting_service import messages_for, parse_direction


def payload():
    return ReaderCastingRequest.model_validate({
        "content_hash": "1" * 64,
        "cues": [{"start": 2, "end": 6}],
        "voices": [{"id": "device-uri", "name": "婷婷", "lang": "zh-CN"}],
    })


def answer():
    return {"cues": [{"cue": 0, "speaker": "小舟", "emotion": "angry"}],
            "voices": [{"speaker": "@narrator", "voice": 0}, {"speaker": "小舟", "voice": 0}]}


def test_only_saved_text_and_sound_catalog_labels_go_to_model():
    req = payload()
    data = messages_for("🌿：“好。”", req)[1]["content"]
    parsed = json.loads(data)
    assert parsed["chapter"] == "🌿：“好。”"
    assert parsed["cues"] == [{"cue": 0, "start": 2, "end": 6, "opening": "“好。”"}]
    assert "device-uri" not in data
    assert parsed["voices"] == [{"voice": 0, "name": "婷婷", "lang": "zh-CN"}]
    with pytest.raises(BadRequestError):
        messages_for("短", req)
    req.cues *= 2
    with pytest.raises(BadRequestError):
        messages_for("🌿：“好。”", req)


@pytest.mark.parametrize("change", [
    lambda x: x["cues"].clear(),
    lambda x: x["cues"].append(x["cues"][0]),
    lambda x: x["cues"][0].update(cue=False),
    lambda x: x["cues"][0].update(emotion="unknown"),
    lambda x: x["cues"][0].update(speaker="@dialogue"),
    lambda x: x["voices"][0].update(voice=-1),
    lambda x: x["voices"][0].update(voice=1),
    lambda x: x["voices"][0].update(voice=False),
    lambda x: x["voices"].pop(),
])
def test_partial_or_unbound_direction_discarded(change):
    data = answer()
    change(data)
    with pytest.raises(BadRequestError):
        parse_direction(json.dumps(data), payload())


def test_unknown_speaker_is_assigned_to_generic_dialogue_voice():
    data = answer()
    data["cues"][0]["speaker"] = None
    data["voices"][1]["speaker"] = "@dialogue"
    result = parse_direction(json.dumps(data), payload())
    assert result.cues[0].start == 2
    assert result.cues[0].speaker is None
    assert result.voices[1].voice_id == "device-uri"


def test_narration_only_chapter_can_select_narrator():
    req = payload()
    req.cues.clear()
    messages_for("晨光落在窗沿。", req)
    result = parse_direction('{"cues":[],"voices":[{"speaker":"@narrator","voice":0}]}', req)
    assert result.cues == []
    assert result.voices[0].voice_id == "device-uri"
