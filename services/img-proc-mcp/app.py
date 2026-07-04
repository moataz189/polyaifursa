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
def blur(input_key: str, radius: float = 2.0) -> str:
    """Apply a Gaussian blur with the given `radius`.

    Returns the S3 key of the processed image.
    """
    if radius < 0:
        raise ValueError("radius must be non-negative")

    image = _load_image(input_key)
    processed = image.filter(ImageFilter.GaussianBlur(radius))
    return _store_image(processed, input_key, f"blur_radius{_fmt_num(radius)}")


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
def crop(input_key: str, left: int, top: int, right: int, bottom: int) -> str:
    """Crop the image to the box (left, top, right, bottom).

    Returns the S3 key of the processed image.
    """
    if left < 0 or top < 0:
        raise ValueError("left and top must be non-negative")
    if right <= left or bottom <= top:
        raise ValueError("right must be greater than left and bottom greater than top")

    image = _load_image(input_key)
    processed = image.crop((left, top, right, bottom))
    return _store_image(processed, input_key, f"crop_{left}_{top}_{right}_{bottom}")


@mcp.tool()
def add_noise(input_key: str, amount: float = 0.02) -> str:
    """Add random Gaussian noise to the image.

    `amount` (0..1) controls the noise strength. Returns the S3 key of the
    processed image.
    """
    if not 0 <= amount <= 1:
        raise ValueError("amount must be between 0 and 1")

    image = _load_image(input_key)
    pixels = image.load()
    width, height = image.size

    # Standard deviation of the noise, scaled by `amount` over the 0..255 range.
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

    descriptor = "noise_" + str(amount).replace(".", "")
    return _store_image(image, input_key, descriptor)


if __name__ == "__main__":
    mcp.run()
