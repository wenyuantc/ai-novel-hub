"""设定生图凭据的用途、有效期和保密性。"""

import pytest

from app.ai.prompts.setting_thumbnail import build_thumbnail_prompt
from app.core.errors import ConflictError
from app.core.security import SecretCipher
from app.services import thumbnail_ai_tokens
from tests.helpers import TEST_FERNET_KEY


def test_token_is_encrypted_purpose_bound_and_expires(monkeypatch):
    cipher = SecretCipher(TEST_FERNET_KEY)
    monkeypatch.setattr(thumbnail_ai_tokens.time, "time", lambda: 1000)
    token = thumbnail_ai_tokens.issue_token(cipher, "project-prompt", {"private_model": "secret"}, expires_seconds=10)
    assert "secret" not in token
    assert thumbnail_ai_tokens.read_token(cipher, "project-prompt", token) == {"private_model": "secret"}
    with pytest.raises(ConflictError):
        thumbnail_ai_tokens.read_token(cipher, "project-preview", token)
    with pytest.raises(ConflictError):
        thumbnail_ai_tokens.read_token(cipher, "project-prompt", token[:-5] + "xxxxx")
    monkeypatch.setattr(thumbnail_ai_tokens.time, "time", lambda: 1010)
    with pytest.raises(ConflictError):
        thumbnail_ai_tokens.read_token(cipher, "project-prompt", token)


def test_default_prompt_fits_request_limit():
    result = build_thumbnail_prompt("character", [("描述", "很长" * 3000)] * 20)
    assert len(result) <= 2000
    # 结尾为用户亲手修改的默认文案，截断字段后仍须完整保留。
    assert result.endswith("画面中不要出现除了当前角色以外的人物、文字、字母、数字或水印。")
