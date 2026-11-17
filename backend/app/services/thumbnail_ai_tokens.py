"""短期加密凭据：隔离提示词来源与预览确认用途。"""

import json
import time
from typing import Any

from app.core.errors import ConflictError
from app.core.security import SecretCipher

_TOKEN_DOMAIN = "setting-thumbnail-ai-v1"
_MAX_TOKEN_LENGTH = 8192


def issue_token(cipher: SecretCipher, purpose: str, payload: dict[str, Any], *, expires_seconds: int = 3600) -> str:
    document = {"domain": _TOKEN_DOMAIN, "purpose": purpose, "expires": int(time.time()) + expires_seconds,
                "payload": payload}
    return cipher.encrypt(json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def read_token(cipher: SecretCipher, purpose: str, token: str) -> dict[str, Any]:
    try:
        if not token or len(token) > _MAX_TOKEN_LENGTH:
            raise ValueError
        document = json.loads(cipher.decrypt(token))
        if (not isinstance(document, dict) or document.get("domain") != _TOKEN_DOMAIN
                or document.get("purpose") != purpose or type(document.get("expires")) is not int
                or document["expires"] <= int(time.time()) or not isinstance(document.get("payload"), dict)):
            raise ValueError
        return document["payload"]
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ConflictError("生成凭据已失效，请重新生成", extra={"code": "thumbnail_ai_token_invalid"}) from None
