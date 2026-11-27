"""把用户提供的参考图转换为模型可以接收的 JPEG。"""

from collections.abc import Iterable
from io import BytesIO

from PIL import Image, ImageOps

from app.ai.images import MAX_COVER_BYTES, sniff_image
from app.ai.providers.base import ImageCapabilityError, ImageInput

MAX_IMAGE_REFERENCES = 16
MAX_REFERENCE_EDGE = 1536


def normalize_images(
    inputs: Iterable[ImageInput], *, max_count: int = MAX_IMAGE_REFERENCES, max_edge: int = MAX_REFERENCE_EDGE,
) -> tuple[ImageInput, ...]:
    references: list[ImageInput] = []
    seen: set[bytes] = set()
    for item in inputs:
        if len(item.data) > MAX_COVER_BYTES or sniff_image(item.data) != item.mime_type:
            raise ImageCapabilityError("参考图格式或大小无效，仅支持 10 MB 以内的 JPG、PNG 或 WebP")
        if item.data in seen:
            continue
        seen.add(item.data)
        if len(seen) > max_count:
            raise ImageCapabilityError(f"最多支持 {max_count} 张参考图")
        try:
            with Image.open(BytesIO(item.data)) as image:
                if image.width * image.height > 16_000_000:
                    raise ImageCapabilityError("参考图像素过多")
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                oriented = ImageOps.exif_transpose(image)
                if oriented.has_transparency_data:
                    rgba = oriented.convert("RGBA")
                    output = Image.new("RGB", rgba.size, "white")
                    output.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    output = oriented.convert("RGB")
                stream = BytesIO()
                output.save(stream, "JPEG", quality=90)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ImageCapabilityError("参考图损坏或无法解码") from exc
        references.append(ImageInput(stream.getvalue(), "image/jpeg"))
    return tuple(references)
