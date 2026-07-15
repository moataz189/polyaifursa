"""Unit tests for the image-processing MCP tools.

S3 download/upload are mocked (see conftest.py) so nothing here touches AWS.
Each test asserts the tool returns an output key under the processed prefix,
or raises a ValueError for invalid input.
"""

import io

import pytest
from PIL import Image

from app import add_noise, blur, crop, flip, resize, rotate

INPUT_KEY = "chat/pred/original/test.jpeg"
PROCESSED_DIR = "chat/pred/processed/"

# The mock S3 fixture downloads a 16x16 test image (see conftest.py).
SOURCE_SIZE = (16, 16)


def _uploaded_size(key, uploaded):
    """Return the (width, height) of the PNG bytes uploaded under `key`."""
    return Image.open(io.BytesIO(uploaded[key])).size



def _assert_processed_key(key, uploaded):
    """A returned key must be a processed key that was actually uploaded.

    It preserves the <chat_id>/<prediction_id> prefix, swaps "original" for
    "processed", keeps the original filename stem, and is saved as PNG.
    """
    assert isinstance(key, str)
    assert key.startswith(PROCESSED_DIR)
    assert key.endswith("_test.png")
    assert key in uploaded


def test_rotate_returns_output_key(mock_s3):
    key = rotate(INPUT_KEY, angle=90)
    _assert_processed_key(key, mock_s3)


def test_flip_horizontal_returns_output_key(mock_s3):
    key = flip(INPUT_KEY, direction="horizontal")
    _assert_processed_key(key, mock_s3)


def test_flip_vertical_returns_output_key(mock_s3):
    key = flip(INPUT_KEY, direction="vertical")
    _assert_processed_key(key, mock_s3)


def test_blur_returns_output_key(mock_s3):
    key = blur(INPUT_KEY, radius=3.0)
    _assert_processed_key(key, mock_s3)


def test_resize_returns_output_key(mock_s3):
    key = resize(INPUT_KEY, width=8, height=10)
    _assert_processed_key(key, mock_s3)


def test_crop_returns_output_key(mock_s3):
    key = crop(INPUT_KEY, left=0, top=0, right=4, bottom=4)
    _assert_processed_key(key, mock_s3)


def test_add_noise_returns_output_key(mock_s3):
    key = add_noise(INPUT_KEY, amount=0.05)
    _assert_processed_key(key, mock_s3)


def test_rotate_key_format(mock_s3):
    assert rotate(INPUT_KEY, angle=90) == "chat/pred/processed/rotate_90_test.png"


def test_blur_key_format(mock_s3):
    assert blur(INPUT_KEY, radius=2) == "chat/pred/processed/blur_radius2_test.png"


def test_resize_key_format(mock_s3):
    assert resize(INPUT_KEY, width=800, height=600) == "chat/pred/processed/resize_800x600_test.png"


def test_flip_key_format(mock_s3):
    assert flip(INPUT_KEY, direction="horizontal") == "chat/pred/processed/flip_horizontal_test.png"


def test_crop_key_format(mock_s3):
    key = crop(INPUT_KEY, left=10, top=20, right=100, bottom=150)
    assert key == "chat/pred/processed/crop_10_20_100_150_test.png"


def test_add_noise_key_format(mock_s3):
    assert add_noise(INPUT_KEY, amount=0.05) == "chat/pred/processed/noise_005_test.png"


def test_flip_invalid_direction_raises(mock_s3):
    with pytest.raises(ValueError):
        flip(INPUT_KEY, direction="diagonal")


def test_resize_invalid_dimensions_raises(mock_s3):
    with pytest.raises(ValueError):
        resize(INPUT_KEY, width=0, height=10)
    with pytest.raises(ValueError):
        resize(INPUT_KEY, width=5, height=-1)


def test_crop_invalid_coordinates_raises(mock_s3):
    # right <= left
    with pytest.raises(ValueError):
        crop(INPUT_KEY, left=5, top=0, right=5, bottom=4)
    # negative coordinate
    with pytest.raises(ValueError):
        crop(INPUT_KEY, left=-1, top=0, right=4, bottom=4)


def test_add_noise_invalid_amount_raises(mock_s3):
    with pytest.raises(ValueError):
        add_noise(INPUT_KEY, amount=-0.1)
    with pytest.raises(ValueError):
        add_noise(INPUT_KEY, amount=1.5)


# --- Bounding-box (region) processing -------------------------------------


def test_blur_with_box_returns_output_key(mock_s3):
    key = blur(INPUT_KEY, radius=3.0, left=2, top=2, right=10, bottom=10)
    _assert_processed_key(key, mock_s3)


def test_blur_with_box_preserves_dimensions(mock_s3):
    key = blur(INPUT_KEY, radius=3.0, left=2, top=2, right=10, bottom=10)
    assert _uploaded_size(key, mock_s3) == SOURCE_SIZE


def test_add_noise_with_box_returns_output_key(mock_s3):
    key = add_noise(INPUT_KEY, amount=0.05, left=1, top=1, right=8, bottom=8)
    _assert_processed_key(key, mock_s3)


def test_add_noise_with_box_preserves_dimensions(mock_s3):
    key = add_noise(INPUT_KEY, amount=0.05, left=1, top=1, right=8, bottom=8)
    assert _uploaded_size(key, mock_s3) == SOURCE_SIZE


def test_crop_with_box_returns_region_size(mock_s3):
    key = crop(INPUT_KEY, left=2, top=2, right=10, bottom=12)
    assert _uploaded_size(key, mock_s3) == (8, 10)


def test_crop_without_coordinates_raises(mock_s3):
    with pytest.raises(ValueError):
        crop(INPUT_KEY)


def test_partial_box_raises(mock_s3):
    # Supplying only some of the four coordinates is invalid for blur/add_noise.
    with pytest.raises(ValueError):
        blur(INPUT_KEY, radius=2.0, left=1, top=1)
    with pytest.raises(ValueError):
        add_noise(INPUT_KEY, amount=0.05, right=8, bottom=8)


def test_blur_invalid_box_raises(mock_s3):
    # right <= left
    with pytest.raises(ValueError):
        blur(INPUT_KEY, radius=2.0, left=5, top=0, right=5, bottom=4)
    # negative coordinate
    with pytest.raises(ValueError):
        blur(INPUT_KEY, radius=2.0, left=-1, top=0, right=4, bottom=4)
    # box exceeds image bounds
    with pytest.raises(ValueError):
        blur(INPUT_KEY, radius=2.0, left=0, top=0, right=100, bottom=100)


def test_add_noise_invalid_box_raises(mock_s3):
    with pytest.raises(ValueError):
        add_noise(INPUT_KEY, amount=0.05, left=0, top=5, right=4, bottom=5)
    with pytest.raises(ValueError):
        add_noise(INPUT_KEY, amount=0.05, left=0, top=0, right=999, bottom=4)


# --- Region rotation -------------------------------------------------------


def test_rotate_with_box_180_preserves_dimensions(mock_s3):
    # A 180 degree rotation works on any rectangle and keeps the full-image size.
    key = rotate(INPUT_KEY, angle=180, left=2, top=2, right=12, bottom=8)
    _assert_processed_key(key, mock_s3)
    assert _uploaded_size(key, mock_s3) == SOURCE_SIZE


def test_rotate_with_box_180_key_format(mock_s3):
    key = rotate(INPUT_KEY, angle=180, left=2, top=2, right=12, bottom=8)
    assert key == "chat/pred/processed/rotate_180_box2_2_12_8_test.png"


def test_rotate_with_square_box_90_preserves_dimensions(mock_s3):
    # 90 degrees is allowed when the region is square.
    key = rotate(INPUT_KEY, angle=90, left=2, top=2, right=10, bottom=10)
    _assert_processed_key(key, mock_s3)
    assert _uploaded_size(key, mock_s3) == SOURCE_SIZE


def test_rotate_with_square_box_90_key_format(mock_s3):
    key = rotate(INPUT_KEY, angle=90, left=2, top=2, right=10, bottom=10)
    assert key == "chat/pred/processed/rotate_90_box2_2_10_10_test.png"


def test_rotate_non_square_box_90_raises(mock_s3):
    with pytest.raises(ValueError, match="square bounding box"):
        rotate(INPUT_KEY, angle=90, left=2, top=2, right=12, bottom=8)


def test_rotate_non_square_box_270_raises(mock_s3):
    with pytest.raises(ValueError, match="square bounding box"):
        rotate(INPUT_KEY, angle=270, left=2, top=2, right=12, bottom=8)


def test_rotate_box_arbitrary_angle_raises(mock_s3):
    with pytest.raises(ValueError, match="0, 90, 180, or 270"):
        rotate(INPUT_KEY, angle=45, left=2, top=2, right=10, bottom=10)


def test_rotate_box_0_degrees_any_rectangle(mock_s3):
    # 0 degrees is a no-op rotation but still valid for a rectangle.
    key = rotate(INPUT_KEY, angle=0, left=2, top=2, right=12, bottom=8)
    _assert_processed_key(key, mock_s3)
    assert _uploaded_size(key, mock_s3) == SOURCE_SIZE


def test_rotate_partial_box_raises(mock_s3):
    with pytest.raises(ValueError):
        rotate(INPUT_KEY, angle=90, left=2, top=2)


def test_rotate_invalid_box_raises(mock_s3):
    # box exceeds image bounds
    with pytest.raises(ValueError):
        rotate(INPUT_KEY, angle=180, left=0, top=0, right=100, bottom=100)


# --- Region flip -----------------------------------------------------------


def test_flip_horizontal_with_box_returns_output_key(mock_s3):
    key = flip(INPUT_KEY, direction="horizontal", left=2, top=2, right=10, bottom=10)
    _assert_processed_key(key, mock_s3)


def test_flip_horizontal_with_box_preserves_dimensions(mock_s3):
    key = flip(INPUT_KEY, direction="horizontal", left=2, top=2, right=10, bottom=8)
    assert _uploaded_size(key, mock_s3) == SOURCE_SIZE


def test_flip_horizontal_with_box_key_format(mock_s3):
    key = flip(INPUT_KEY, direction="horizontal", left=2, top=2, right=10, bottom=8)
    assert key == "chat/pred/processed/flip_horizontal_box2_2_10_8_test.png"


def test_flip_vertical_with_box_returns_output_key(mock_s3):
    key = flip(INPUT_KEY, direction="vertical", left=1, top=1, right=12, bottom=9)
    _assert_processed_key(key, mock_s3)


def test_flip_vertical_with_box_preserves_dimensions(mock_s3):
    key = flip(INPUT_KEY, direction="vertical", left=1, top=1, right=12, bottom=9)
    assert _uploaded_size(key, mock_s3) == SOURCE_SIZE


def test_flip_vertical_with_box_key_format(mock_s3):
    key = flip(INPUT_KEY, direction="vertical", left=1, top=1, right=12, bottom=9)
    assert key == "chat/pred/processed/flip_vertical_box1_1_12_9_test.png"


def test_flip_partial_box_raises(mock_s3):
    with pytest.raises(ValueError):
        flip(INPUT_KEY, direction="horizontal", left=2, top=2)


def test_flip_invalid_box_raises(mock_s3):
    # right <= left
    with pytest.raises(ValueError):
        flip(INPUT_KEY, direction="horizontal", left=5, top=0, right=5, bottom=4)
    # negative coordinate
    with pytest.raises(ValueError):
        flip(INPUT_KEY, direction="vertical", left=-1, top=0, right=4, bottom=4)
    # box exceeds image bounds
    with pytest.raises(ValueError):
        flip(INPUT_KEY, direction="horizontal", left=0, top=0, right=100, bottom=100)

