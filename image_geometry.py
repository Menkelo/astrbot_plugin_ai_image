"""Image geometry independent of providers: retain content and fit a target canvas."""

from __future__ import annotations

import math
from io import BytesIO

from PIL import Image, ImageOps

MAX_CANVAS_PIXELS = 40_000_000
MAX_CANVAS_EDGE = 16_384


def oriented_size(data: bytes) -> tuple[int, int]:
    """Read the displayed dimensions, including EXIF's quarter-turn rotations."""
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
        if image.getexif().get(274) in (5, 6, 7, 8):
            width, height = height, width
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸无效")
    return width, height


def ratio_label(dimensions: tuple[int, int]) -> str:
    width, height = dimensions
    divisor = math.gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def pad_to_ratio(
    data: bytes,
    target: tuple[int, int],
    *,
    min_long_edge: int = 0,
    max_pixels: int = MAX_CANVAS_PIXELS,
    max_edge: int = MAX_CANVAS_EDGE,
) -> bytes:
    """Fit the resolution tier and target ratio in a single content-preserving step.

    Opaque pictures get white padding. Pictures with transparency keep it. No
    attempt is made to infer which parts of a model-generated image are expendable.
    """
    rw, rh = target
    if rw <= 0 or rh <= 0 or min_long_edge < 0 or max_pixels < 1 or max_edge < 1:
        raise ValueError("无效的目标画幅")
    if max(rw, rh) / min(rw, rh) > min(max_edge, max_pixels):
        raise ValueError("目标比例过于极端，无法在画布限制内表示")

    with Image.open(BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source)
        width, height = image.size
        # Accept integer-pixel rounding without repeatedly adding thin borders.
        if (
            abs(width * rh - height * rw) <= max(rw, rh)
            and max(width, height) >= min_long_edge
        ):
            return data

        canvas_w = max(width, (height * rw + rh - 1) // rh)
        canvas_h = max(height, (width * rh + rw - 1) // rw)
        enlarge = min_long_edge > max(canvas_w, canvas_h)
        if enlarge:
            # Calculate from the true ratio, not an already-rounded small canvas.
            if rw >= rh:
                canvas_w = min_long_edge
                canvas_h = max(1, round(canvas_w * rh / rw))
            else:
                canvas_h = min_long_edge
                canvas_w = max(1, round(canvas_h * rw / rh))
        scale = min(
            1.0,
            max_edge / canvas_w,
            max_edge / canvas_h,
            math.sqrt(max_pixels / (canvas_w * canvas_h)),
        )
        if scale < 1:
            canvas_w = max(1, math.floor(canvas_w * scale))
            canvas_h = max(1, math.floor(canvas_h * scale))
            # Flooring one axis must not materially change the requested ratio.
            if abs(canvas_w * rh - canvas_h * rw) > max(rw, rh):
                raise ValueError("目标画幅超过像素限制，无法安全补边")

        image = image.convert("RGBA")
        if enlarge or image.width > canvas_w or image.height > canvas_h:
            image = ImageOps.contain(
                image, (canvas_w, canvas_h), Image.Resampling.LANCZOS
            )
        transparent = image.getchannel("A").getextrema()[0] < 255
        canvas = Image.new(
            "RGBA", (canvas_w, canvas_h), (255, 255, 255, 0 if transparent else 255)
        )
        # A second alpha mask would multiply alpha and fade semitransparent input.
        canvas.paste(
            image, ((canvas_w - image.width) // 2, (canvas_h - image.height) // 2)
        )
        output = BytesIO()
        canvas.save(output, "PNG")
        return output.getvalue()


def is_gpt_image_2(model: str) -> bool:
    name = model.strip().lower().rsplit("/", 1)[-1]
    return name == "gpt-image-2" or name.startswith("gpt-image-2-")


def is_gpt_image_2_5(model: str) -> bool:
    """Recognize 2.5 names while preserving the configured API identifier verbatim."""
    name = model.strip().lower().rsplit("/", 1)[-1]
    return name == "gpt-image-2.5" or name.startswith("gpt-image-2.5-")


def supports_custom_image_size(model: str) -> bool:
    return is_gpt_image_2(model) or is_gpt_image_2_5(model)


def gpt_image_2_size(target: tuple[int, int], long_edge: int) -> str:
    """Choose a valid GPT Image 2/2.5 size without changing the local target.

    Official constraints: 16px alignment, <=3840px per edge, <=3:1 aspect ratio,
    and 655,360..8,294,400 pixels. Enumerating nearby widths for each allowed height
    avoids rounding a legal size into an illegal one at a boundary.
    """
    rw, rh = target
    if rw <= 0 or rh <= 0:
        raise ValueError("无效的参考图比例")
    ratio = max(1 / 3, min(3, rw / rh))
    stretch = max(ratio, 1 / ratio)
    area = max(655_360, min(8_294_400, min(3840, long_edge) ** 2 / stretch))
    best = None
    for height in range(16, 3841, 16):
        ideal_units = height * ratio / 16
        for width_units in {math.floor(ideal_units), math.ceil(ideal_units)}:
            width = width_units * 16
            pixels = width * height
            if not (16 <= width <= 3840 and 655_360 <= pixels <= 8_294_400):
                continue
            if max(width, height) > 3 * min(width, height):
                continue
            # Favor both the requested ratio and the requested resolution tier.
            score = 4 * abs(math.log((width / height) / ratio)) + abs(
                math.log(pixels / area)
            )
            candidate = (score, abs(max(width, height) - long_edge), width, height)
            if best is None or candidate < best:
                best = candidate
    if best is None:
        raise ValueError("无法选择受支持的图像尺寸")
    return f"{best[2]}x{best[3]}"


def legacy_gpt_image_size(target: tuple[int, int] | None) -> str:
    """GPT Image 1/1.5 family request sizes; local padding retains the exact target."""
    if target is None:
        return "1024x1024"
    ratio = target[0] / target[1]
    candidates = ((1024, 1024), (1536, 1024), (1024, 1536))
    width, height = min(
        candidates, key=lambda size: abs(math.log(size[0] / size[1] / ratio))
    )
    return f"{width}x{height}"
