"""Unit tests for the image-processing MCP tools.

S3 download/upload are mocked (see conftest.py) so nothing here touches AWS.
Each test asserts the tool returns an output key under the processed prefix,
or raises a ValueError for invalid input.
"""

import pytest

from app import add_noise, blur, crop, flip, resize, rotate

INPUT_KEY = "chat/pred/original/test.jpeg"
PROCESSED_DIR = "chat/pred/processed/"


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
