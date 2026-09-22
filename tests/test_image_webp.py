from io import BytesIO

from PIL import Image
from starlette.datastructures import UploadFile

from app.image_utils import ImageConversionError, image_to_webp
from app.routers.api_access import _save_gate_snapshot
from app.routers.api_readings import _compress_meter_photo_if_needed
from app.routers.api_users import _save_avatar


def _image_bytes(fmt: str, size=(2400, 1600), mode="RGB") -> bytes:
    output = BytesIO()
    color = (40, 120, 210, 160) if mode == "RGBA" else (40, 120, 210)
    Image.new(mode, size, color).save(output, format=fmt)
    return output.getvalue()


def _assert_webp(data: bytes, max_dimension: int) -> None:
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WEBP"
    with Image.open(BytesIO(data)) as image:
        assert image.format == "WEBP"
        assert max(image.size) <= max_dimension


def test_image_to_webp_resizes_and_preserves_alpha():
    result = image_to_webp(
        _image_bytes("PNG", mode="RGBA"),
        max_dimension=800,
        target_max_bytes=200 * 1024,
    )

    _assert_webp(result, 800)
    with Image.open(BytesIO(result)) as image:
        assert image.mode == "RGBA"


def test_image_to_webp_rejects_corrupt_input():
    try:
        image_to_webp(b"not an image", max_dimension=800)
    except ImageConversionError:
        pass
    else:
        raise AssertionError("Corrupt bytes must not be stored")


def test_image_to_webp_rejects_excessive_decoded_dimensions(monkeypatch):
    from app import image_utils

    monkeypatch.setattr(image_utils, "MAX_DECODED_IMAGE_PIXELS", 100)
    try:
        image_to_webp(_image_bytes("PNG", size=(11, 10)), max_dimension=800)
    except ImageConversionError:
        pass
    else:
        raise AssertionError("Excessive decoded dimensions must be rejected")


def test_meter_photo_is_always_webp():
    result, extension = _compress_meter_photo_if_needed(
        _image_bytes("JPEG", size=(640, 480)),
        ".jpg",
    )

    assert extension == ".webp"
    _assert_webp(result, 2048)


def test_avatar_and_gate_snapshot_are_saved_as_webp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    png = _image_bytes("PNG", size=(1200, 800))

    avatar_url = _save_avatar(
        UploadFile(BytesIO(png), filename="avatar.png", headers={"content-type": "image/png"}),
        17,
    )
    gate_url = _save_gate_snapshot(
        UploadFile(BytesIO(png), filename="frame.png", headers={"content-type": "image/png"}),
        42,
    )

    assert avatar_url == "/uploads/avatars/17/avatar.webp"
    assert gate_url == "/uploads/gate/42.webp"
    _assert_webp((tmp_path / "uploads/avatars/17/avatar.webp").read_bytes(), 1024)
    _assert_webp((tmp_path / "uploads/gate/42.webp").read_bytes(), 1920)
