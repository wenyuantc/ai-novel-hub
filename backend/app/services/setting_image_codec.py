"""设定图有界解码和标准 WebP 快照校验。"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from hashlib import sha256

import anyio
from PIL import Image, ImageOps, UnidentifiedImageError

from app.ai.images import sniff_image
from app.core.errors import BadRequestError

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_STANDARD_BYTES = 512 * 1024
MAX_SIDE = 8192
MAX_PIXELS = 16_000_000
STANDARD_SIDE = 512
TOO_LARGE = "图片过大，上限为 10MB"
_IMAGE_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
_limiter = anyio.CapacityLimiter(2)


@dataclass(frozen=True)
class StandardImage:
    data: bytes
    width: int
    height: int
    mime_type: str = "image/webp"

    @property
    def sha256(self) -> str:
        return sha256(self.data).hexdigest()


def _dimensions(image: Image.Image, *, standard: bool = False) -> None:
    width, height = image.size
    side = STANDARD_SIDE if standard else MAX_SIDE
    if width <= 0 or height <= 0 or width > side or height > side or width * height > MAX_PIXELS:
        raise BadRequestError("图片尺寸超出允许范围")
    if getattr(image, "n_frames", 1) != 1:
        raise BadRequestError("不支持动画图片")


def _open_verified(data: bytes, mime_type: str | None, *, standard: bool = False) -> Image.Image:
    limit = MAX_STANDARD_BYTES if standard else MAX_INPUT_BYTES
    if len(data) > limit:
        raise BadRequestError("标准缩略图过大" if standard else TOO_LARGE)
    detected = sniff_image(data)
    if detected is None or (standard and detected != "image/webp"):
        raise BadRequestError("图片仅支持 JPG、PNG 或 WebP 格式")
    if mime_type and mime_type != detected:
        raise BadRequestError("图片格式与声明的类型不一致")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=[_IMAGE_FORMATS[detected]]) as probe:
                if probe.format != _IMAGE_FORMATS[detected]:
                    raise BadRequestError("图片格式与内容不一致")
                _dimensions(probe, standard=standard)
                probe.verify()
            with Image.open(io.BytesIO(data), formats=[_IMAGE_FORMATS[detected]]) as image:
                _dimensions(image, standard=standard)
                image.load()
                oriented = ImageOps.exif_transpose(image)
                mode = "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
                converted = oriented.convert(mode)
                # 用像素重建确保 EXIF、ICC、XMP 等元数据不会进入输出。
                return Image.frombytes(mode, converted.size, converted.tobytes())
    except BadRequestError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise BadRequestError("图片文件损坏或格式不受支持") from exc


def normalize_image(data: bytes, mime_type: str | None = None) -> StandardImage:
    image = _open_verified(data, mime_type)
    image.thumbnail((STANDARD_SIDE, STANDARD_SIDE), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="WEBP", quality=85, method=4)
    result = output.getvalue()
    if len(result) > MAX_STANDARD_BYTES:
        raise BadRequestError("标准缩略图过大，请选择更简单的图片")
    return StandardImage(result, *image.size)


def _validate_standard_container(data: bytes) -> None:
    """Only accept single-image WebP chunks; imported snapshots retain their exact bytes."""
    invalid = BadRequestError("标准缩略图结构无效或含有元数据")
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise invalid
    if int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise invalid
    chunks: list[bytes] = []
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise invalid
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        end = offset + 8 + size
        padded_end = end + (size & 1)
        if padded_end > len(data) or (size & 1 and data[end] != 0):
            raise invalid
        if kind == b"VP8X":
            payload = data[offset + 8:end]
            # Extended header: only the alpha flag is permitted; EXIF, XMP,
            # ICC, animation and all reserved bits/bytes must be absent.
            if size != 10 or payload[0] & ~0x10 or payload[1:4] != b"\0\0\0":
                raise invalid
        chunks.append(kind)
        offset = padded_end
    if chunks not in (
        [b"VP8 "], [b"VP8L"], [b"VP8X", b"VP8 "], [b"VP8X", b"VP8L"],
        [b"VP8X", b"ALPH", b"VP8 "],
    ):
        raise invalid


def validate_standard_image(data: bytes, mime_type: str | None = None) -> StandardImage:
    if len(data) > MAX_STANDARD_BYTES:
        raise BadRequestError("标准缩略图过大")
    _validate_standard_container(data)
    image = _open_verified(data, mime_type, standard=True)
    return StandardImage(data, *image.size)


async def normalize_image_async(data: bytes, mime_type: str | None = None) -> StandardImage:
    return await anyio.to_thread.run_sync(normalize_image, data, mime_type, limiter=_limiter)
