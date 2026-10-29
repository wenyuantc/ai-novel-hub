"""封面图片的共同边界：只接受 JPEG / PNG / WebP，单张不超过 10MB。"""

import base64
import binascii

MAX_COVER_BYTES = 10 * 1024 * 1024
TOO_LARGE = "封面图片过大，上限为 10MB"
IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
IMAGE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def sniff_image(data: bytes) -> str | None:
    """按文件头识别格式，不信任文件名与调用方提供的 Content-Type。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data: bytes, mime_type: str | None = None) -> str:
    if len(data) > MAX_COVER_BYTES:
        raise ValueError(TOO_LARGE)
    detected = sniff_image(data)
    if detected is None:
        raise ValueError("封面仅支持 JPG、PNG 或 WebP 图片，请检查文件内容")
    if mime_type is not None and detected != mime_type:
        raise ValueError("封面图片格式与声明的类型不一致")
    return detected


def decode_image(value: str) -> bytes:
    # 编码长度先限流，避免在解码时分配无上限的额外内存。
    if len(value) > 4 * ((MAX_COVER_BYTES + 2) // 3):
        raise ValueError(TOO_LARGE)
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("封面图片的 base64 数据已损坏") from exc
