from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import AsyncMock

from PIL import Image

from image_geometry import is_gpt_image_2, is_gpt_image_2_5, supports_custom_image_size
from tests.test_generation_gallery import GENERATOR
from tests.test_image_geometry import encode, subject_picture
from tests.test_ratio_pipeline import RecordingSession, Response, provider

MODELS = ("gpt-image-2.5-sunburst", "gpt-image-2.5-flare")


class GPTImage25Tests(unittest.IsolatedAsyncioTestCase):
    def generator(
        self,
        model=MODELS[0],
        *,
        quality="auto",
        background="auto",
        responses=None,
        output=None,
    ):
        session = RecordingSession(responses)
        config = provider(model)
        generator = GENERATOR.AIImageGenerator(
            config,
            session=session,
            gpt_image_quality=quality,
            gpt_image_background=background,
        )
        generator._extract_images_from_response = AsyncMock(
            return_value=([output or encode(subject_picture())], {}, None)
        )
        return generator, session

    def test_family_recognition_keeps_2_and_25_distinct(self):
        for model in (
            *MODELS,
            "gpt-image-2.5",
            "gateway/gpt-image-2.5-flare-2026-09-01",
        ):
            with self.subTest(model=model):
                self.assertTrue(is_gpt_image_2_5(model))
                self.assertTrue(supports_custom_image_size(model))
                self.assertFalse(is_gpt_image_2(model))
        self.assertTrue(supports_custom_image_size("gpt-image-2"))
        self.assertFalse(is_gpt_image_2_5("gpt-image-2"))
        self.assertFalse(supports_custom_image_size("gpt-image-1.5"))
        self.assertFalse(is_gpt_image_2_5("gpt-image-2.50"))

    def test_both_25_models_request_native_resolution_instead_of_auto(self):
        for model in MODELS:
            with self.subTest(model=model):
                generator, _session = self.generator(model)
                self.assertEqual(
                    generator._openai_image_request_size(
                        provider(model), "2K", "16:9", []
                    ),
                    "2048x1152",
                )
                self.assertEqual(
                    generator._openai_image_request_size(
                        provider(model), "4K", "16:9", []
                    ),
                    "3840x2160",
                )
                reference = encode(Image.new("RGB", (719, 1600), "green"))
                size = generator._openai_image_request_size(
                    provider(model), "4K", None, [(reference, "image/png")]
                )
                self.assertNotEqual(size, "auto")
                width, height = map(int, size.split("x"))
                self.assertEqual(width % 16, 0)
                self.assertEqual(height % 16, 0)
                self.assertLessEqual(max(width, height), 3840)
                self.assertGreaterEqual(width * height, 655_360)
                self.assertLessEqual(width * height, 8_294_400)
                self.assertEqual(
                    generator._resolution_target_long_edge(
                        "4K", "719:1600", provider(model)
                    ),
                    3840,
                )

    async def test_default_generation_uses_png_and_the_upstream_quality_default(self):
        for model in MODELS:
            with self.subTest(model=model):
                generator, session = self.generator(model)
                images, error = await generator._generate_openai_image_api(
                    provider(model), "test", [], "2K", "16:9"
                )
                self.assertIsNone(error)
                self.assertTrue(images)
                fields = session.requests[0]["fields"]
                self.assertEqual(fields["model"], model)
                self.assertEqual(fields["size"], "2048x1152")
                self.assertEqual(fields["output_format"], "png")
                for absent in (
                    "quality",
                    "background",
                    "input_fidelity",
                    "response_format",
                    "output_compression",
                ):
                    self.assertNotIn(absent, fields)

    async def test_high_quality_choices_are_sent_verbatim_in_json(self):
        for model in MODELS:
            for quality in ("low", "medium", "high", "xhigh", "max"):
                with self.subTest(model=model, quality=quality):
                    generator, session = self.generator(
                        model, quality=quality, background="opaque"
                    )
                    _images, error = await generator._generate_openai_image_api(
                        provider(model), "test", [], "1K", "1:1"
                    )
                    self.assertIsNone(error)
                    fields = session.requests[0]["fields"]
                    self.assertEqual(fields["quality"], quality)
                    self.assertEqual(fields["background"], "opaque")
                    self.assertEqual(fields["output_format"], "png")

    async def test_edits_forward_options_and_preserve_transparency_through_padding(
        self,
    ):
        transparent = Image.new("RGBA", (512, 512), (200, 60, 30, 128))
        reference = encode(Image.new("RGB", (256, 512), "white"))
        for model in MODELS:
            with self.subTest(model=model):
                generator, session = self.generator(
                    model,
                    quality="xhigh",
                    background="transparent",
                    output=encode(transparent),
                )
                images, error = await generator.generate_image(
                    "透明素材", [(reference, "image/png")]
                )
                self.assertIsNone(error)
                fields = session.requests[0]["fields"]
                self.assertEqual(fields["quality"], "xhigh")
                self.assertEqual(fields["background"], "transparent")
                self.assertEqual(fields["output_format"], "png")
                self.assertNotIn("input_fidelity", fields)
                self.assertNotIn("response_format", fields)
                self.assertEqual(session.requests[0]["files"][0][3], reference)
                result = Image.open(BytesIO(images[0]))
                self.assertEqual(result.format, "PNG")
                self.assertEqual(result.size, (512, 1024))
                self.assertEqual(result.getpixel((0, 0))[3], 0)
                self.assertEqual(result.getpixel((256, 512)), (200, 60, 30, 128))

    async def test_25_alias_is_not_silently_changed_to_a_different_model(self):
        for model in ("gpt-image-2.5", "gateway/gpt-image-2.5-flare"):
            with self.subTest(model=model):
                generator, session = self.generator(model, quality="max")
                await generator._generate_openai_image_api(
                    provider(model), "test", [], "1K", "1:1"
                )
                self.assertEqual(session.requests[0]["fields"]["model"], model)

    async def test_25_options_do_not_change_older_or_unknown_model_requests(self):
        for model in ("gpt-image-2", "gpt-image-1.5", "gpt-image-custom"):
            with self.subTest(model=model):
                generator, session = self.generator(
                    model, quality="max", background="transparent"
                )
                await generator._generate_openai_image_api(
                    provider(model), "test", [], "1K", "1:1"
                )
                fields = session.requests[0]["fields"]
                self.assertEqual(fields["model"], model)
                self.assertEqual(fields["response_format"], "b64_json")
                for option in ("quality", "background", "output_format"):
                    self.assertNotIn(option, fields)

    async def test_size_downgrade_preserves_explicit_quality_and_background(self):
        unsupported = Response(
            400, {"error": {"code": "unsupported_value", "param": "size"}}
        )
        generator, session = self.generator(
            quality="max", background="transparent", responses=[unsupported, Response()]
        )
        reference = encode(Image.new("RGB", (256, 512), "white"))
        images, error = await generator.generate_image(
            "test", [(reference, "image/png")]
        )
        self.assertIsNone(error)
        self.assertEqual(len(session.requests), 2)
        self.assertIn("size", session.requests[0]["fields"])
        self.assertNotIn("size", session.requests[1]["fields"])
        for request in session.requests:
            self.assertEqual(request["fields"]["quality"], "max")
            self.assertEqual(request["fields"]["background"], "transparent")
            self.assertEqual(request["fields"]["output_format"], "png")
        self.assertEqual(Image.open(BytesIO(images[0])).size, (512, 1024))

    async def test_provider_fallback_only_sends_25_options_to_the_25_provider(self):
        generator, session = self.generator(
            quality="max",
            background="transparent",
            responses=[Response(500), Response()],
        )
        generator.backup_config = provider("gpt-image-2")
        images, error = await generator.generate_image("test")
        self.assertIsNone(error)
        self.assertTrue(images)
        self.assertEqual(session.requests[0]["fields"]["quality"], "max")
        self.assertEqual(session.requests[1]["fields"]["model"], "gpt-image-2")
        self.assertNotIn("quality", session.requests[1]["fields"])
        self.assertNotIn("background", session.requests[1]["fields"])
        self.assertEqual(generator.last_used_provider.model, "gpt-image-2")

    async def test_option_rejections_are_reported_without_silent_downgrades_or_extra_calls(
        self,
    ):
        for param, label in (
            ("quality", "质量"),
            ("background", "背景"),
            ("output_format", "PNG"),
        ):
            for status in (400, 422):
                for editing in (False, True):
                    with self.subTest(param=param, status=status, editing=editing):
                        failure = Response(
                            status,
                            {"error": {"code": "unsupported_value", "param": param}},
                        )
                        generator, session = self.generator(
                            quality="max", background="transparent", responses=[failure]
                        )
                        generator.backup_config = provider("gpt-image-2.5-flare")
                        reference = (
                            [(encode(Image.new("RGB", (20, 40))), "image/png")]
                            if editing
                            else None
                        )
                        images, error = await generator.generate_image(
                            "test", reference
                        )
                        self.assertIsNone(images)
                        self.assertIn(label, error)
                        self.assertNotIn("内容审核", error)
                        self.assertEqual(len(session.requests), 1)

    async def test_moderation_rejection_is_not_misclassified_as_a_quality_error(self):
        failure = Response(
            400, {"error": {"code": "moderation_blocked", "param": "quality"}}
        )
        generator, session = self.generator(quality="max", responses=[failure])
        images, error = await generator.generate_image("test")
        self.assertIsNone(images)
        self.assertIn("内容审核", error)
        self.assertEqual(len(session.requests), 1)

    def test_invalid_or_missing_option_values_use_upstream_defaults(self):
        generator, _session = self.generator(quality="not-a-quality", background=None)
        self.assertEqual(
            generator._gpt_image_25_options(provider(MODELS[0])),
            {"output_format": "png"},
        )
        generator, _session = self.generator(
            quality=" XHIGH ", background=" TRANSPARENT "
        )
        self.assertEqual(
            generator._gpt_image_25_options(provider(MODELS[0])),
            {"output_format": "png", "quality": "xhigh", "background": "transparent"},
        )


if __name__ == "__main__":
    unittest.main()
