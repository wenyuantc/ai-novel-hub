import io

import pytest
from PIL import Image, ImageSequence, features

from app.core.errors import BadRequestError
from app.services.setting_image_codec import normalize_image, validate_standard_image


def image_bytes(fmt, size=(100, 50), *, mode="RGB", **save):
    output = io.BytesIO()
    Image.new(mode, size, (20, 30, 40, 100) if mode == "RGBA" else (20, 30, 40)).save(output, fmt, **save)
    return output.getvalue()


@pytest.mark.parametrize(("fmt", "mime"), [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")])
def test_normalizes_real_images_without_upscaling_and_preserves_standard_bytes(fmt, mime):
    assert features.check("webp")
    data = image_bytes(fmt, (1200, 600))
    result = normalize_image(data, mime)
    assert (result.width, result.height, result.mime_type) == (512, 256, "image/webp")
    assert validate_standard_image(result.data, "image/webp").data == result.data
    small = normalize_image(image_bytes("PNG", (5, 3)))
    assert (small.width, small.height) == (5, 3)


def test_transparency_exif_and_metadata_are_handled():
    exif = Image.Exif()
    exif[274] = 6
    result = normalize_image(image_bytes("PNG", (40, 20), mode="RGBA", exif=exif, icc_profile=b"fake"))
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.size == (20, 40)
        assert image.mode == "RGBA" and image.getpixel((0, 0))[3] == 100
        assert not image.getexif() and not image.info.get("icc_profile")


@pytest.mark.parametrize("metadata", ["exif", "icc_profile", "xmp"])
def test_import_rejects_real_webp_metadata_without_reencoding(metadata):
    exif = Image.Exif()
    exif[274] = 6
    value = {"exif": exif, "icc_profile": b"private color profile", "xmp": b"private xmp"}[metadata]
    original = image_bytes("WEBP", **{metadata: value})
    with Image.open(io.BytesIO(original)) as image:
        assert image.info.get(metadata)
    with pytest.raises(BadRequestError, match="元数据"):
        validate_standard_image(original, "image/webp")


def test_import_rejects_unknown_chunks_and_trailing_bytes():
    clean = normalize_image(image_bytes("PNG")).data
    with pytest.raises(BadRequestError, match="结构"):
        validate_standard_image(clean + b"private")
    unknown = clean + b"META\x04\0\0\0hide"
    unknown = unknown[:4] + (len(unknown) - 8).to_bytes(4, "little") + unknown[8:]
    with pytest.raises(BadRequestError, match="元数据"):
        validate_standard_image(unknown)


def test_rejects_invalid_wrong_mime_animation_and_dimensions():
    with pytest.raises(BadRequestError):
        normalize_image(b"\x89PNG\r\n\x1a\ninvalid", "image/png")
    with pytest.raises(BadRequestError):
        normalize_image(image_bytes("PNG"), "image/jpeg")
    with pytest.raises(BadRequestError):
        normalize_image(image_bytes("PNG", (8193, 1)))
    out = io.BytesIO()
    first = Image.new("RGB", (2, 2), "red")
    first.save(out, "PNG", save_all=True, append_images=[Image.new("RGB", (2, 2), "blue")], duration=100)
    assert len(list(ImageSequence.Iterator(Image.open(io.BytesIO(out.getvalue()))))) == 2
    with pytest.raises(BadRequestError, match="动画"):
        normalize_image(out.getvalue())
