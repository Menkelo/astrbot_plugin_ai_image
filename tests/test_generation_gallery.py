"""Exercise the existing generator and delivery pipeline without calling providers."""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from gallery_store import GalleryStore
from tests.test_gallery_store import picture


def load_plugin_with_host_stubs():
    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("_ai_image_regression")
    package.__path__ = [str(root)]
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.logger = Mock()
    host = types.ModuleType("astrbot")
    host.__path__ = []
    host.api = api
    components = types.ModuleType("astrbot.api.message_components")
    components.Image = types.SimpleNamespace(
        fromFileSystem=lambda path: ("image", path)
    )
    components.Reply = lambda **values: ("reply", values)
    api.message_components = components
    events = types.ModuleType("astrbot.api.event")
    events.AstrMessageEvent = object
    events.filter = types.SimpleNamespace(
        command=lambda *a, **kw: lambda fn: fn, regex=lambda *a, **kw: lambda fn: fn
    )
    stars = types.ModuleType("astrbot.api.star")
    stars.Context = object
    stars.Star = type("Star", (), {"__init__": lambda self, context: None})
    stars.StarTools = types.SimpleNamespace(get_data_dir=Mock())
    config = types.ModuleType("astrbot.core.config.astrbot_config")
    config.AstrBotConfig = dict
    io = types.ModuleType("astrbot.core.utils.io")
    io.download_image_by_url = AsyncMock()
    io.save_temp_img = Mock()
    modules = {
        "_ai_image_regression": package,
        "astrbot": host,
        "astrbot.api": api,
        "astrbot.api.message_components": components,
        "astrbot.api.event": events,
        "astrbot.api.star": stars,
        "astrbot.core.config.astrbot_config": config,
        "astrbot.core.utils.io": io,
        "astrbot.api.web": None,
    }
    with patch.dict(sys.modules, modules):
        main = importlib.import_module("_ai_image_regression.main")
        generator = importlib.import_module("_ai_image_regression.image_generator")
    return main, generator, modules


MAIN, GENERATOR, HOST_MODULES = load_plugin_with_host_stubs()


class GenerationGalleryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = MAIN.Gemini_Images.__new__(MAIN.Gemini_Images)
        self.plugin.timeout = 30
        self.plugin._vertex_key_cursor = 0
        self.plugin._gemini_key_cursor = 0
        self.plugin._get_http_session = Mock(return_value=object())
        self.plugin._safe_send_chain = AsyncMock()
        self.plugin._reply_error = AsyncMock()
        self.plugin._refund_quota = AsyncMock()
        self.plugin.gallery_auto_archive = True
        self.plugin.gallery_store = GalleryStore(self.root / "gallery")
        self.main_provider = GENERATOR.ProviderConfig(
            "primary",
            "openai",
            "https://example.invalid",
            "test-private-key",
            "primary-model",
        )
        self.backup_provider = GENERATOR.ProviderConfig(
            "fallback",
            "openai",
            "https://example.invalid",
            "another-private-key",
            "fallback-model",
        )
        self.event = types.SimpleNamespace(
            get_sender_name=Mock(return_value="创作者"),
            message_obj=types.SimpleNamespace(group_id="group-1"),
        )
        self.output = [picture(), picture("navy")]
        self.fake_generator = types.SimpleNamespace(
            generate_image=AsyncMock(return_value=(self.output, None)),
            last_used_provider=self.backup_provider,
            close_session=AsyncMock(),
        )

    def save_temp(self, data):
        path = self.root / f"sent-{len(list(self.root.glob('sent-*')))}.png"
        path.write_bytes(data)
        return str(path)

    async def generate(self):
        with (
            patch.object(MAIN, "AIImageGenerator", return_value=self.fake_generator),
            patch.object(MAIN, "save_temp_img", side_effect=self.save_temp),
        ):
            await self.plugin._generate_and_send_image_async(
                "森林里的小屋",
                self.event,
                self.main_provider,
                images_data=[(picture("white"), "image/png")],
                resolution="2K",
                aspect_ratio="16:9",
                task_id="job-test",
                user_id="user-1",
                quota_consumed=True,
                reply_id="42",
                command="生图",
                backup_provider=self.backup_provider,
            )

    async def test_generated_images_archive_actual_fallback_and_keep_delivery(self):
        await self.generate()
        images = self.plugin.gallery_store.list_images()["items"]
        self.assertEqual(len(images), 2)
        record = self.plugin.gallery_store.get_image(images[0]["id"])
        self.assertEqual(record["model"], "fallback-model")
        self.assertEqual(record["provider"], "fallback")
        self.assertEqual(record["prompt"], "森林里的小屋")
        self.assertEqual(record["reference_count"], 1)
        self.assertEqual(record["group_id"], "group-1")
        self.assertEqual(record["command"], "生图")
        self.assertEqual(record["resolution"], "2K")
        self.assertNotIn("private-key", json.dumps(record))
        chain = self.plugin._safe_send_chain.await_args.args[1]
        self.assertEqual(len(chain), 3)
        self.assertEqual(Path(chain[1][1]).read_bytes(), self.output[0])
        self.fake_generator.close_session.assert_awaited_once()
        self.plugin._refund_quota.assert_not_awaited()

    async def test_disk_or_capacity_failure_does_not_turn_success_into_generation_failure(
        self,
    ):
        self.plugin.gallery_store = types.SimpleNamespace(
            add_image=Mock(side_effect=OSError("disk full"))
        )
        await self.generate()
        self.assertEqual(self.plugin.gallery_store.add_image.call_count, 2)
        self.plugin._safe_send_chain.assert_awaited_once()
        self.plugin._reply_error.assert_not_awaited()
        self.plugin._refund_quota.assert_not_awaited()

    async def test_disabled_capture_does_not_touch_sender_metadata(self):
        self.plugin.gallery_auto_archive = False
        self.event.get_sender_name.side_effect = RuntimeError("not needed")
        await self.generate()
        self.event.get_sender_name.assert_not_called()
        self.assertEqual(self.plugin.gallery_store.overview()["total"], 0)
        self.plugin._safe_send_chain.assert_awaited_once()

    async def test_missing_sender_metadata_does_not_break_image_delivery(self):
        self.event.get_sender_name.side_effect = AttributeError("adapter has no sender")
        await self.generate()
        self.plugin._safe_send_chain.assert_awaited_once()
        self.plugin._reply_error.assert_not_awaited()

    async def test_provider_failure_does_not_archive_and_refunds_consumed_quota(self):
        self.fake_generator.generate_image.return_value = (None, "超时")
        await self.generate()
        self.assertEqual(self.plugin.gallery_store.overview()["total"], 0)
        self.plugin._safe_send_chain.assert_not_awaited()
        self.plugin._refund_quota.assert_awaited_once_with("user-1")

    async def test_old_astrbot_without_page_api_can_still_archive(self):
        self.plugin.gallery_store = None
        self.plugin._gallery_api = None
        self.plugin.gallery_enabled = True
        self.plugin.gallery_max_storage_mb = 0
        self.plugin.gallery_max_upload_mb = 25
        with (
            patch.dict(sys.modules, HOST_MODULES),
            patch.object(
                MAIN.StarTools, "get_data_dir", return_value=self.root / "persistent"
            ),
        ):
            await self.plugin._initialize_gallery()
        self.assertIsNotNone(self.plugin.gallery_store)
        self.assertIsNone(self.plugin._gallery_api)
        await self.generate()
        self.assertEqual(self.plugin.gallery_store.overview()["total"], 2)

    async def test_generator_reports_fallback_and_resets_it_on_a_failed_job(self):
        generator = GENERATOR.AIImageGenerator(
            self.main_provider, backup_config=self.backup_provider
        )
        generator._generate_openai = AsyncMock(
            side_effect=[(None, "API 500"), ([picture()], None)]
        )
        images, error = await generator.generate_image("test")
        self.assertIsNone(error)
        self.assertEqual(len(images), 1)
        self.assertIs(generator.last_used_provider, self.backup_provider)
        generator._generate_openai = AsyncMock(return_value=(None, "API 500"))
        await generator.generate_image("test")
        self.assertIsNone(generator.last_used_provider)
        await generator.close_session()


if __name__ == "__main__":
    unittest.main()
