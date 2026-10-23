"""密码哈希、JWT 与 API Key 对称加密。"""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_COOKIE = "access_token"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: int, secret: str, expire_minutes: int) -> str:
    now = datetime.now(UTC)
    payload = {"sub": str(user_id), "iat": now, "exp": now + timedelta(minutes=expire_minutes)}
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, secret: str) -> int | None:
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None


class SecretCipher:
    """基于 Fernet 的 API Key 加解密。"""

    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("API Key 解密失败，请检查 ENCRYPTION_KEY 是否被更换") from exc


def mask_secret(secret: str) -> str:
    """掩码展示，例如 sk-abcdefgh1234 -> sk-***1234。"""
    if not secret:
        return ""
    if len(secret) >= 12:
        return f"{secret[:3]}***{secret[-4:]}"
    if len(secret) >= 8:
        return f"***{secret[-4:]}"
    return "***"
