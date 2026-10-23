import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import SecretCipher, mask_secret
from tests.helpers import TEST_FERNET_KEY, TEST_JWT_SECRET


def test_missing_encryption_key_fails_fast(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_secret=TEST_JWT_SECRET)  # type: ignore[call-arg]


def test_invalid_encryption_key_fails_fast():
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, jwt_secret=TEST_JWT_SECRET, encryption_key="not-a-fernet-key")  # type: ignore[call-arg]
    assert "ENCRYPTION_KEY" in str(exc.value)


def test_missing_jwt_secret_fails_fast(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, encryption_key=TEST_FERNET_KEY)  # type: ignore[call-arg]


def test_cors_origin_list():
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        jwt_secret=TEST_JWT_SECRET,
        encryption_key=TEST_FERNET_KEY,
        cors_origins=" http://a.com , ,http://b.com",
    )
    assert s.cors_origin_list == ["http://a.com", "http://b.com"]


def test_cipher_roundtrip():
    cipher = SecretCipher(TEST_FERNET_KEY)
    token = cipher.encrypt("sk-secret")
    assert token != "sk-secret"
    assert cipher.decrypt(token) == "sk-secret"


@pytest.mark.parametrize(
    ("raw", "masked"),
    [("sk-abcdefgh1234", "sk-***1234"), ("abcdefgh", "***efgh"), ("abc", "***"), ("", "")],
)
def test_mask_secret(raw, masked):
    assert mask_secret(raw) == masked
