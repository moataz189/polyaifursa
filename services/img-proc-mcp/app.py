"""Image-processing MCP server.

Exposes a small set of image-manipulation tools over the Model Context Protocol.
Each tool works directly with S3:

    1. Download the input image bytes from S3 (using an input S3 key).
    2. Open the image with Pillow and convert it to RGB.
    3. Apply a single transformation.
    4. Save the result as PNG.
    5. Upload the result to S3 under the processed prefix with a unique name.
    6. Return only the output S3 key.

Configuration comes from the environment (see s3.py):

    AWS_REGION, S3_BUCKET, S3_PROCESSED_PREFIX
"""

import io
import random
from typing import Optional

from mcp.server.fastmcp import FastMCP
from PIL import Image, ImageFilter

import s3

mcp = FastMCP("img-proc")


def _load_image(input_key: str) -> Image.Image:
    """Download the object at `input_key` from S3 and open it as an RGB image."""
    data = s3.download_image(input_key)
    return Image.open(io.BytesIO(data)).convert("RGB")


def _fmt_num(value) -> str:
    """Format a number for use in a filename: drop a trailing ".0" from whole floats."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _store_image(image: Image.Image, input_key: str, descriptor: str) -> str:
    """Save `image` as PNG and upload it to the processed key derived from
    `input_key` and `descriptor`. Returns the output S3 key."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    png_bytes = buffer.getvalue()
    output_key = s3.build_processed_key(input_key, descriptor)
    s3.upload_image(output_key, png_bytes, content_type="image/png")
    return output_key


def _resolve_box(
    left: Optional[int],
    top: Optional[int],
    right: Optional[int],
    bottom: Optional[int],
):
    """Return the box (left, top, right, bottom) if all four coordinates are
    given, or None if none are given.

    Raises ValueError if only some of the four coordinates are supplied.
    """
    coords = (left, top, right, bottom)
    if all(c is None for c in coords):
        return None
    if any(c is None for c in coords):
        raise ValueError(
            "a bounding box requires all of left, top, right and bottom"
        )
    return coords


def _validate_box(box, size) -> None:
    """Validate that `box` (left, top, right, bottom) is a sane region that
    fits inside an image of `size` (width, height)."""
    left, top, right, bottom = box
    width, height = size
    if left < 0 or top < 0:
        raise ValueError("left and top must be non-negative")
    if right <= left or bottom <= top:
        raise ValueError(
            "right must be greater than left and bottom greater than top"
        )
    if right > width or bottom > height:
        raise ValueError("bounding box must fit inside the image")


def _add_noise_in_place(image: Image.Image, amount: float) -> Image.Image:
    """Add random Gaussian noise to every pixel of `image` in place and return
    it. `amount` (0..1) scales the noise standard deviation over 0..255."""
    pixels = image.load()
    width, height = image.size
    sigma = amount * 255.0
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            r = int(r + random.gauss(0, sigma))
            g = int(g + random.gauss(0, sigma))
            b = int(b + random.gauss(0, sigma))
            pixels[x, y] = (
                max(0, min(255, r)),
                max(0, min(255, g)),
                max(0, min(255, b)),
            )
    return image


@mcp.tool()
def rotate(input_key: str, angle: float) -> str:
    """Rotate the image counter-clockwise by `angle` degrees.

    Returns the S3 key of the processed image.
    """
    image = _load_image(input_key)
    processed = image.rotate(angle, expand=True)
    return _store_image(processed, input_key, f"rotate_{_fmt_num(angle)}")


@mcp.tool()
def flip(input_key: str, direction: str = "horizontal") -> str:
    """Flip the image along `direction` ("horizontal" or "vertical").

    Returns the S3 key of the processed image.
    """
    if direction == "horizontal":
        transpose = Image.Transpose.FLIP_LEFT_RIGHT
    elif direction == "vertical":
        transpose = Image.Transpose.FLIP_TOP_BOTTOM
    else:
        raise ValueError("direction must be 'horizontal' or 'vertical'")

    image = _load_image(input_key)
    processed = image.transpose(transpose)
    return _store_image(processed, input_key, f"flip_{direction}")


@mcp.tool()
def blur(
    input_key: str,
    radius: float = 2.0,
    left: Optional[int] = None,
    top: Optional[int] = None,
    right: Optional[int] = None,
    bottom: Optional[int] = None,
) -> str:
    """Apply a Gaussian blur with the given `radius`.

    If no bounding box is given (left/top/right/bottom all omitted), the blur
    is applied to the whole image.

    If a bounding box (left, top, right, bottom) is given, only that region is
    blurred and pasted back into the full-size image, so the returned image
    keeps its original dimensions.

    Returns the S3 key of the processed image.
    """
    if radius < 0:
        raise ValueError("radius must be non-negative")

    box = _resolve_box(left, top, right, bottom)
    image = _load_image(input_key)

    if box is None:
        processed = image.filter(ImageFilter.GaussianBlur(radius))
        descriptor = f"blur_radius{_fmt_num(radius)}"
    else:
        _validate_box(box, image.size)
        region = image.crop(box).filter(ImageFilter.GaussianBlur(radius))
        image.paste(region, box)
        processed = image
        left, top, right, bottom = box
        descriptor = (
            f"blur_radius{_fmt_num(radius)}_box{left}_{top}_{right}_{bottom}"
        )

    return _store_image(processed, input_key, descriptor)


@mcp.tool()
def resize(input_key: str, width: int, height: int) -> str:
    """Resize the image to `width` x `height` pixels.

    Returns the S3 key of the processed image.
    """
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")

    image = _load_image(input_key)
    processed = image.resize((width, height))
    return _store_image(processed, input_key, f"resize_{width}x{height}")


@mcp.tool()
def crop(
    input_key: str,
    left: Optional[int] = None,
    top: Optional[int] = None,
    right: Optional[int] = None,
    bottom: Optional[int] = None,
) -> str:
    """Crop the image to the box (left, top, right, bottom).

    Unlike blur/add_noise, crop always returns only the cropped region, so a
    bounding box is required: all four of left, top, right and bottom must be
    supplied. Omitting any of them raises ValueError.

    Returns the S3 key of the processed image.
    """
    box = _resolve_box(left, top, right, bottom)
    if box is None:
        raise ValueError("crop requires left, top, right and bottom")

    left, top, right, bottom = box
    if left < 0 or top < 0:
        raise ValueError("left and top must be non-negative")
    if right <= left or bottom <= top:
        raise ValueError("right must be greater than left and bottom greater than top")

    image = _load_image(input_key)
    processed = image.crop((left, top, right, bottom))
    return _store_image(processed, input_key, f"crop_{left}_{top}_{right}_{bottom}")


@mcp.tool()
def add_noise(
    input_key: str,
    amount: float = 0.02,
    left: Optional[int] = None,
    top: Optional[int] = None,
    right: Optional[int] = None,
    bottom: Optional[int] = None,
) -> str:
    """Add random Gaussian noise to the image.

    `amount` (0..1) controls the noise strength.

    If no bounding box is given (left/top/right/bottom all omitted), noise is
    added to the whole image.

    If a bounding box (left, top, right, bottom) is given, only that region is
    noised and pasted back into the full-size image, so the returned image
    keeps its original dimensions.

    Returns the S3 key of the processed image.
    """
    if not 0 <= amount <= 1:
        raise ValueError("amount must be between 0 and 1")

    box = _resolve_box(left, top, right, bottom)
    image = _load_image(input_key)

    if box is None:
        _add_noise_in_place(image, amount)
        descriptor = "noise_" + str(amount).replace(".", "")
    else:
        _validate_box(box, image.size)
        region = _add_noise_in_place(image.crop(box), amount)
        image.paste(region, box)
        left, top, right, bottom = box
        descriptor = (
            "noise_"
            + str(amount).replace(".", "")
            + f"_box{left}_{top}_{right}_{bottom}"
        )

    return _store_image(image, input_key, descriptor)


if __name__ == "__main__":
    mcp.run()
