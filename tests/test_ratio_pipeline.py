from __future__ import annotations

import copy
import json
import unittest
from email import policy
from email.parser import BytesParser
from io import BytesIO
from unittest.mock import AsyncMock

from PIL import Image, ImageOps

from tests.test_generation_gallery import GENERATOR
from tests.test_image_geometry import encode, subject_picture


class Response:
    def __init__(self, status=200, error=None):
        self.status = status
        self.error = error or {}
        self.closed = False

    async def text(self):
        return json.dumps(self.error)

    def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.close()


class RecordingSession:
    """Serialize actual aiohttp multipart payloads, without making network calls."""

    def __init__(self, responses=None):
        self.closed = False
        self.responses = list(responses or [Response()])
        self.requests = []

    async def post(self, url, **kwargs):
        fields = {}
        files = []
        if "data" in kwargs:
            payload = kwargs["data"]()

            class Writer:
                def __init__(self):
                    self.buffer = bytearray()

                async def write(self, data):
                    self.buffer.extend(data)

            writer = Writer()
            await payload.write(writer)
            headers = f"Content-Type: {payload.headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            message = BytesParser(policy=policy.default).parsebytes(
                headers + writer.buffer
            )
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                content = part.get_payload(decode=True)
                if part.get_filename():
                    files.append(
                        (name, part.get_filename(), part.get_content_type(), content)
                    )
                else:
                    fields[name] = content.decode("utf-8")
        else:
            fields = copy.deepcopy(kwargs["json"])
        self.requests.append({"url": url, "fields": fields, "files": files})
        if not self.responses:
            raise AssertionError("Unexpected extra provider request")
        return self.responses.pop(0)


def provider(model="gpt-image-2", api_type="openai"):
    return GENERATOR.ProviderConfig(
        "test", api_type, "https://example.invalid/v1", "test-key", model
    )


class RatioPipelineTests(unittest.IsolatedAsyncioTestCase):
    def generator(self, config=None, responses=None, output=None):
        session = RecordingSession(responses)
        generator = GENERATOR.AIImageGenerator(config or provider(), session=session)
        generator._extract_images_from_response = AsyncMock(
            return_value=([output or encode(subject_picture())], {}, None)
        )
        return generator, session

    def assert_ratio(self, data, target):
        width, height = Image.open(BytesIO(data)).size
        self.assertLessEqual(abs(width * target[1] - height * target[0]), max(target))

    async def test_edit_keeps_reference_canvas_and_omits_unsupported_gpt2_fidelity(
        self,
    ):
        reference = encode(Image.new("RGB", (719, 1600), "green"))
        generator, session = self.generator()
        images, error = await generator.generate_image(
            "换成水彩", [(reference, "image/png")], image_size="1K"
        )
        self.assertIsNone(error)
        request = session.requests[0]
        self.assertEqual(request["files"][0][3], reference)
        self.assertNotIn("input_fidelity", request["fields"])
        self.assertIn("719:1600", request["fields"]["prompt"])
        self.assertNotIn("screenshot", request["fields"]["prompt"])
        width, height = map(int, request["fields"]["size"].split("x"))
        self.assertGreater(height, width)
        self.assertGreaterEqual(width * height, 655_360)
        self.assert_ratio(images[0], (719, 1600))

    async def test_white_background_subject_is_complete_after_entire_pipeline(self):
        original = subject_picture()
        generator, _session = self.generator(output=encode(original))
        reference = encode(Image.new("RGB", (256, 512), "white"))
        images, error = await generator.generate_image(
            "人物张开双臂", [(reference, "image/png")]
        )
        self.assertIsNone(error)
        output = Image.open(BytesIO(images[0]))
        self.assertEqual(output.size, (512, 1024))
        self.assertEqual(
            output.crop((0, 256, 512, 768)).tobytes(),
            original.convert("RGBA").tobytes(),
        )

    async def test_multiple_references_retain_their_individual_dimensions(self):
        references = [
            encode(Image.new("RGB", size, color))
            for size, color in [((200, 400), "green"), ((300, 100), "blue")]
        ]
        generator, session = self.generator()
        images, error = await generator.generate_image(
            "参考第二张的颜色", [(data, "image/png") for data in references]
        )
        self.assertIsNone(error)
        self.assertEqual([file[3] for file in session.requests[0]["files"]], references)
        self.assert_ratio(images[0], (1, 2))

    async def test_exif_is_applied_before_choosing_size_and_uploading(self):
        image = Image.new("RGB", (120, 60), "orange")
        exif = image.getexif()
        exif[274] = 6
        reference = encode(image, "JPEG", exif=exif)
        generator, session = self.generator()
        images, error = await generator.generate_image(
            "保持构图", [(reference, "image/jpeg")]
        )
        self.assertIsNone(error)
        upload = session.requests[0]["files"][0]
        self.assertEqual(upload[2], "image/png")
        decoded = Image.open(BytesIO(upload[3]))
        self.assertEqual(decoded.size, (60, 120))
        self.assertNotIn(decoded.getexif().get(274), (5, 6, 7, 8))
        self.assert_ratio(images[0], (1, 2))

    async def test_explicit_ratio_overrides_reference_without_cropping_it(self):
        reference = encode(Image.new("RGB", (200, 400), "green"))
        generator, session = self.generator()
        images, error = await generator.generate_image(
            "扩展风景", [(reference, "image/png")], aspect_ratio="16:9"
        )
        self.assertIsNone(error)
        self.assertEqual(session.requests[0]["files"][0][3], reference)
        self.assertEqual(session.requests[0]["fields"]["size"], "1280x720")
        self.assert_ratio(images[0], (16, 9))

    async def test_provider_routes_keep_content_with_their_own_ratio_policy(self):
        reference = encode(Image.new("RGB", (200, 400), "green"))
        returned = encode(subject_picture((1024, 1024)))
        for api_type, method in [
            ("gemini", "_generate_gemini"),
            ("vertex", "_generate_vertex"),
            ("openai", "_generate_openai"),
        ]:
            with self.subTest(api_type=api_type):
                generator = GENERATOR.AIImageGenerator(
                    provider("custom-model", api_type)
                )
                handler = AsyncMock(return_value=([returned], None))
                setattr(generator, method, handler)
                images, error = await generator.generate_image(
                    "保持完整", [(reference, "image/png")]
                )
                self.assertIsNone(error)
                self.assertIsNone(
                    handler.await_args.args[3]
                )  # 1:2 is not a native discrete ratio.
                self.assertIn("1:2", handler.await_args.args[1])
                if api_type == "openai":
                    self.assert_ratio(images[0], (1, 2))
                else:
                    self.assertEqual(images[0], returned)

    async def test_gemini_explicit_ratios_keep_native_pixels_without_borders(self):
        reference = encode(Image.new("RGB", (200, 400), "green"))
        for ratio, size in [
            ("16:9", (1344, 768)),
            ("16:9", (1376, 768)),
            ("9:16", (768, 1344)),
            ("9:16", (768, 1376)),
            ("4:5", (896, 1152)),
            ("5:4", (1152, 896)),
        ]:
            with self.subTest(ratio=ratio, size=size):
                returned = encode(subject_picture(size))
                generator, session = self.generator(
                    provider("gemini-test", "gemini"), output=returned
                )
                images, error = await generator.generate_image(
                    "保持完整", [(reference, "image/png")], aspect_ratio=ratio
                )
                self.assertIsNone(error)
                self.assertEqual(images[0], returned)
                payload = session.requests[0]["fields"]
                self.assertEqual(
                    payload["generationConfig"]["imageConfig"]["aspectRatio"], ratio
                )
                self.assertIn(ratio, payload["contents"][0]["parts"][0]["text"])

    async def test_gemini_config_retry_keeps_the_returned_canvas(self):
        returned = encode(subject_picture((1024, 1024)))
        failure = Response(400, {"error": {"message": "Unknown generationConfig"}})
        generator, session = self.generator(
            provider("gemini-test", "gemini"),
            responses=[failure, Response()], output=returned,
        )
        images, error = await generator.generate_image("风景", aspect_ratio="16:9")
        self.assertIsNone(error)
        self.assertEqual(images[0], returned)
        self.assertEqual(len(session.requests), 2)
        self.assertIn("generationConfig", session.requests[0]["fields"])
        self.assertNotIn("generationConfig", session.requests[1]["fields"])
        self.assertEqual(
            session.requests[0]["fields"]["contents"],
            session.requests[1]["fields"]["contents"],
        )

    async def test_gemini_upscale_preserves_native_ratio_and_transparency(self):
        returned = encode(Image.new("RGBA", (672, 384), (200, 60, 30, 128)))
        generator, session = self.generator(
            provider("gemini-test", "gemini"), output=returned
        )
        images, error = await generator.generate_image(
            "风景", aspect_ratio="16:9", image_size="2K"
        )
        self.assertIsNone(error)
        self.assert_ratio(images[0], (672, 384))
        with Image.open(BytesIO(images[0])) as output:
            self.assertEqual(max(output.size), 2048)
            self.assertEqual(output.mode, "RGBA")
            self.assertEqual(output.getchannel("A").getextrema(), (128, 128))
        self.assertEqual(
            session.requests[0]["fields"]["generationConfig"]["imageConfig"],
            {"aspectRatio": "16:9", "imageSize": "2K"},
        )

    async def test_gemini_output_exif_is_applied_without_padding(self):
        source = subject_picture((1344, 768))
        exif = source.getexif()
        exif[274] = 6
        returned = encode(source, exif=exif)
        generator, _session = self.generator(
            provider("gemini-test", "gemini"), output=returned
        )
        images, error = await generator.generate_image("人物", aspect_ratio="9:16")
        self.assertIsNone(error)
        with Image.open(BytesIO(returned)) as original:
            expected = ImageOps.exif_transpose(original)
            with Image.open(BytesIO(images[0])) as output:
                self.assertEqual(output.size, (768, 1344))
                self.assertEqual(output.tobytes(), expected.tobytes())
                self.assertNotIn(output.getexif().get(274), (5, 6, 7, 8))

    async def test_cross_provider_fallback_uses_successful_provider_ratio_policy(self):
        returned = encode(subject_picture((1024, 1024)))
        for primary_type, backup_type in [
            ("openai", "gemini"),
            ("openai", "vertex"),
            ("gemini", "openai"),
        ]:
            with self.subTest(primary=primary_type, backup=backup_type):
                primary = provider("primary", primary_type)
                backup = provider("backup", backup_type)
                generator = GENERATOR.AIImageGenerator(primary, backup_config=backup)
                setattr(
                    generator, f"_generate_{primary_type}",
                    AsyncMock(return_value=(None, "API 500")),
                )
                handler = AsyncMock(return_value=([returned], None))
                setattr(generator, f"_generate_{backup_type}", handler)
                images, error = await generator.generate_image(
                    "风景", aspect_ratio="16:9"
                )
                self.assertIsNone(error)
                self.assertIs(generator.last_used_provider, backup)
                self.assertEqual(handler.await_args.args[3], "16:9")
                if backup_type == "openai":
                    self.assert_ratio(images[0], (16, 9))
                else:
                    self.assertEqual(images[0], returned)

    async def test_exact_reference_ratio_can_use_a_native_supported_ratio(self):
        generator = GENERATOR.AIImageGenerator(provider("gemini-test", "gemini"))
        generator._generate_gemini = AsyncMock(
            return_value=([encode(subject_picture())], None)
        )
        reference = encode(Image.new("RGB", (90, 160), "green"))
        await generator.generate_image("保持完整", [(reference, "image/png")])
        self.assertEqual(generator._generate_gemini.await_args.args[3], "9:16")

    async def test_provider_fallback_does_not_reset_the_target_frame(self):
        primary, backup = provider("primary"), provider("backup")
        generator = GENERATOR.AIImageGenerator(primary, backup_config=backup)
        generator._generate_openai = AsyncMock(
            side_effect=[(None, "API 500"), ([encode(subject_picture())], None)]
        )
        reference = encode(Image.new("RGB", (719, 1600), "green"))
        images, error = await generator.generate_image(
            "保持完整", [(reference, "image/png")]
        )
        self.assertIsNone(error)
        self.assertIs(generator.last_used_provider, backup)
        self.assert_ratio(images[0], (719, 1600))

    async def test_size_rejection_drops_only_size_and_still_keeps_local_target(self):
        failure = Response(
            400, {"error": {"code": "unsupported_value", "param": "size"}}
        )
        generator, session = self.generator(responses=[failure, Response()])
        reference = encode(Image.new("RGB", (256, 512), "green"))
        images, error = await generator.generate_image(
            "保持完整", [(reference, "image/png")]
        )
        self.assertIsNone(error)
        self.assertEqual(len(session.requests), 2)
        self.assertIn("size", session.requests[0]["fields"])
        self.assertNotIn("size", session.requests[1]["fields"])
        self.assertEqual(session.requests[0]["files"], session.requests[1]["files"])
        self.assert_ratio(images[0], (1, 2))

    async def test_fidelity_rejection_does_not_throw_away_a_valid_size(self):
        failure = Response(
            400, {"error": {"code": "unsupported_parameter", "param": "input_fidelity"}}
        )
        generator, session = self.generator(
            provider("gpt-image-1.5"), responses=[failure, Response()]
        )
        reference = encode(Image.new("RGB", (256, 512), "green"))
        images, error = await generator.generate_image(
            "保持完整", [(reference, "image/png")]
        )
        self.assertIsNone(error)
        self.assertEqual(session.requests[0]["fields"]["size"], "1024x1536")
        self.assertEqual(session.requests[1]["fields"]["size"], "1024x1536")
        self.assertIn("input_fidelity", session.requests[0]["fields"])
        self.assertNotIn("input_fidelity", session.requests[1]["fields"])
        self.assert_ratio(images[0], (1, 2))

    async def test_unknown_and_moderation_400_do_not_trigger_parameter_retries(self):
        for error in [
            {"code": "moderation_blocked", "param": "size"},
            {"code": "unsupported_parameter", "param": "prompt"},
            {"message": "The image size or content cannot be accepted"},
        ]:
            with self.subTest(error=error):
                generator, session = self.generator(
                    responses=[Response(400, {"error": error})]
                )
                images, message = await generator.generate_image(
                    "test", [(encode(Image.new("RGB", (20, 40))), "image/png")]
                )
                self.assertIsNone(images)
                self.assertIsNotNone(message)
                self.assertEqual(len(session.requests), 1)

    async def test_text_generation_also_only_retries_explicit_size_errors(self):
        bad_size = Response(400, {"error": {"code": "invalid_size", "param": "size"}})
        generator, session = self.generator(responses=[bad_size, Response()])
        images, error = await generator.generate_image("风景", aspect_ratio="16:9")
        self.assertIsNone(error)
        self.assertEqual(session.requests[0]["fields"]["size"], "1280x720")
        self.assertNotIn("size", session.requests[1]["fields"])
        self.assert_ratio(images[0], (16, 9))

    def test_resolution_tiers_follow_the_successful_provider(self):
        generator, _session = self.generator()
        self.assertEqual(
            generator._resolution_target_long_edge(
                "4K", "1:1", provider("gemini-test", "gemini")
            ),
            4096,
        )
        self.assertEqual(
            generator._resolution_target_long_edge("4K", "1:1", provider()), 2880
        )
        self.assertEqual(
            generator._resolution_target_long_edge("4K", "719:1600", provider()), 3840
        )

    async def test_ultrawide_preset_keeps_its_native_resolution(self):
        returned = encode(Image.new("RGB", (2016, 864), "navy"))
        generator, session = self.generator(output=returned)
        images, error = await generator.generate_image(
            "wide landscape", aspect_ratio="21:9", image_size="2K"
        )
        self.assertIsNone(error)
        self.assertEqual(session.requests[0]["fields"]["size"], "2016x864")
        self.assertEqual(Image.open(BytesIO(images[0])).size, (2016, 864))

    async def test_small_output_does_not_magnify_padding_rounding_errors(self):
        returned = encode(Image.new("RGB", (16, 16), "navy"))
        generator, _session = self.generator(output=returned)
        reference = encode(Image.new("RGB", (719, 1600), "white"))
        images, error = await generator.generate_image(
            "tiny result", [(reference, "image/png")], image_size="1K"
        )
        self.assertIsNone(error)
        self.assert_ratio(images[0], (719, 1600))
        self.assertEqual(max(Image.open(BytesIO(images[0])).size), 1024)


if __name__ == "__main__":
    unittest.main()
