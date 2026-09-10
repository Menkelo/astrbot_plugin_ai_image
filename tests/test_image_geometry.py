from __future__ import annotations

import unittest
from io import BytesIO

from PIL import Image, ImageDraw

from image_geometry import (
    gpt_image_2_size,
    is_gpt_image_2,
    legacy_gpt_image_size,
    oriented_size,
    pad_to_ratio,
)


def encode(image: Image.Image, format="PNG", **kwargs) -> bytes:
    stream = BytesIO()
    image.save(stream, format, **kwargs)
    return stream.getvalue()


def subject_picture(size=(512, 512)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.ellipse(
        (width * 0.42, height * 0.13, width * 0.58, height * 0.29), fill="navy"
    )
    draw.rectangle(
        (width * 0.47, height * 0.32, width * 0.53, height * 0.7), fill="navy"
    )
    draw.line(
        (
            width * 0.1,
            height * 0.5,
            width * 0.5,
            height * 0.38,
            width * 0.9,
            height * 0.5,
        ),
        fill="red",
        width=9,
    )
    return image


class ImageGeometryTests(unittest.TestCase):
    def test_white_background_does_not_cause_subject_edges_to_be_removed(self):
        source = subject_picture()
        output = Image.open(BytesIO(pad_to_ratio(encode(source), (1, 2))))
        self.assertEqual(output.size, (512, 1024))
        self.assertEqual(
            output.crop((0, 256, 512, 768)).tobytes(), source.convert("RGBA").tobytes()
        )
        self.assertEqual(output.getpixel((0, 0)), (255, 255, 255, 255))

    def test_texture_and_uniform_color_follow_identical_geometry_rules(self):
        texture = Image.new("RGB", (96, 96))
        draw = ImageDraw.Draw(texture)
        for x in range(0, 96, 8):
            draw.rectangle((x, 0, x + 7, 95), fill="navy" if x % 16 else "orange")
        for source in (texture, Image.new("RGB", (96, 96), "steelblue")):
            output = Image.open(BytesIO(pad_to_ratio(encode(source), (1, 2))))
            self.assertEqual(output.size, (96, 192))
            self.assertEqual(
                output.crop((0, 48, 96, 144)).tobytes(),
                source.convert("RGBA").tobytes(),
            )

    def test_arbitrary_ratios_enclose_the_whole_image_without_stretching(self):
        source = subject_picture((96, 80))
        original = source.convert("RGBA").tobytes()
        for target in (
            (719, 1600),
            (1600, 719),
            (9, 21),
            (5, 4),
            (1, 2),
            (1, 8),
            (8, 1),
        ):
            with self.subTest(target=target):
                output = Image.open(BytesIO(pad_to_ratio(encode(source), target)))
                width, height = output.size
                self.assertLessEqual(
                    abs(width * target[1] - height * target[0]), max(target)
                )
                x, y = (width - source.width) // 2, (height - source.height) // 2
                self.assertEqual(
                    output.crop((x, y, x + source.width, y + source.height)).tobytes(),
                    original,
                )

    def test_matching_ratio_preserves_original_encoding(self):
        data = encode(Image.new("RGB", (160, 90), "navy"), "JPEG")
        self.assertIs(pad_to_ratio(data, (16, 9)), data)

    def test_transparency_is_preserved_without_multiplying_alpha(self):
        image = Image.new("RGBA", (80, 40), (200, 60, 30, 128))
        output = Image.open(BytesIO(pad_to_ratio(encode(image), (1, 1))))
        self.assertEqual(output.getpixel((40, 40)), (200, 60, 30, 128))
        self.assertEqual(output.getpixel((0, 0))[3], 0)

    def test_large_canvas_fits_memory_limits_and_retains_all_four_corners(self):
        source = Image.new("RGB", (1000, 1000))
        draw = ImageDraw.Draw(source)
        for box, color in [
            ((0, 0, 499, 499), "red"),
            ((500, 0, 999, 499), "lime"),
            ((0, 500, 499, 999), "blue"),
            ((500, 500, 999, 999), "yellow"),
        ]:
            draw.rectangle(box, fill=color)
        output = Image.open(
            BytesIO(
                pad_to_ratio(
                    encode(source), (1, 8), max_pixels=1_000_000, max_edge=1024
                )
            )
        )
        self.assertLessEqual(output.width * output.height, 1_000_000)
        self.assertLessEqual(max(output.size), 1024)
        self.assertEqual(output.size, (128, 1024))
        self.assertEqual(output.getpixel((5, 453))[:3], (255, 0, 0))
        self.assertEqual(output.getpixel((123, 453))[:3], (0, 255, 0))
        self.assertEqual(output.getpixel((5, 570))[:3], (0, 0, 255))
        self.assertEqual(output.getpixel((123, 570))[:3], (255, 255, 0))

    def test_unrepresentable_ratio_is_rejected_before_large_allocation(self):
        data = encode(Image.new("RGB", (10, 10), "red"))
        for ratio in ((0, 1), (1, -1), (1, 100_000_000)):
            with self.subTest(ratio=ratio), self.assertRaises(ValueError):
                pad_to_ratio(data, ratio)

    def test_exif_rotation_dimensions_for_jpeg_and_png(self):
        source = Image.new("RGB", (120, 60), "green")
        exif = source.getexif()
        exif[274] = 6
        for format in ("JPEG", "PNG"):
            self.assertEqual(
                oriented_size(encode(source, format, exif=exif)), (60, 120)
            )

    def test_gpt_image_2_sizes_respect_all_documented_limits(self):
        for ratio in ((1, 1), (16, 9), (1, 2), (719, 1600), (1, 8), (8, 1), (91, 93)):
            areas = []
            for tier in (1024, 2048, 4096):
                with self.subTest(ratio=ratio, tier=tier):
                    width, height = map(int, gpt_image_2_size(ratio, tier).split("x"))
                    self.assertEqual(width % 16, 0)
                    self.assertEqual(height % 16, 0)
                    self.assertLessEqual(max(width, height), 3840)
                    self.assertLessEqual(max(width, height), 3 * min(width, height))
                    self.assertGreaterEqual(width * height, 655_360)
                    self.assertLessEqual(width * height, 8_294_400)
                    areas.append(width * height)
            self.assertEqual(areas, sorted(areas))

    def test_model_capabilities_do_not_confuse_gpt_image_1_with_2(self):
        self.assertTrue(is_gpt_image_2("gpt-image-2"))
        self.assertTrue(is_gpt_image_2("provider/gpt-image-2-2026-04-21"))
        self.assertFalse(is_gpt_image_2("gpt-image-1.5"))
        self.assertFalse(is_gpt_image_2("gpt-image-2.5-sunburst"))
        self.assertEqual(legacy_gpt_image_size((16, 9)), "1536x1024")
        self.assertEqual(legacy_gpt_image_size((1, 2)), "1024x1536")
        self.assertEqual(legacy_gpt_image_size(None), "1024x1024")


if __name__ == "__main__":
    unittest.main()
