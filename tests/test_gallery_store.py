from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gallery_store import GalleryError, GalleryStore, ImageNotFound


def picture(color="seagreen", size=(320, 180), format="PNG"):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=format)
    return output.getvalue()


class GalleryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "library"
        self.store = GalleryStore(self.root)

    def add(self, color="seagreen", **metadata):
        return self.store.add_image(picture(color), metadata)["image"]

    def test_original_and_metadata_survive_restart(self):
        original = picture()
        image = self.store.add_image(
            original,
            {
                "prompt": "山野来信 🌿",
                "model": "gemini-image",
                "user_id": "123",
                "command": "生图",
            },
        )["image"]
        restarted = GalleryStore(self.root)
        path, record = restarted.original(image["id"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(record["prompt"], "山野来信 🌿")
        self.assertEqual(record["model"], "gemini-image")
        self.assertEqual(restarted.overview()["total"], 1)
        self.assertNotIn("sha256", record)

    def test_thumbnails_and_previews_keep_aspect_and_are_bounded(self):
        image = self.store.add_image(picture(size=(2400, 1200)))["image"]
        for preview, dimensions in [(False, (480, 240)), (True, (1800, 900))]:
            response = self.store.image_data_url(image["id"], preview=preview)
            data = base64.b64decode(response["data_url"].split(",", 1)[1])
            with Image.open(BytesIO(data)) as decoded:
                self.assertEqual(decoded.size, dimensions)
                self.assertEqual(decoded.format, "WEBP")

    def test_exif_orientation_used_for_dimensions(self):
        buffer = BytesIO()
        image = Image.new("RGB", (300, 150), "green")
        exif = image.getexif()
        exif[274] = 6
        image.save(buffer, "JPEG", exif=exif)
        record = self.store.add_image(buffer.getvalue(), source="imported")["image"]
        self.assertEqual((record["width"], record["height"]), (150, 300))
        self.assertEqual(
            self.store.original(record["id"])[0].read_bytes(), buffer.getvalue()
        )

    def test_import_deduplicates_without_erasing_existing_metadata(self):
        image = self.add(prompt="原始提示词")
        result = self.store.add_image(
            picture(), source="imported", filename="another.png"
        )
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["image"]["id"], image["id"])
        self.assertEqual(result["image"]["prompt"], "原始提示词")
        self.store.batch([image["id"]], "trash")
        duplicate = self.store.add_image(picture(), source="imported")
        self.assertIsNotNone(duplicate["image"]["deleted_at"])
        self.assertEqual(self.store.overview()["total"], 0)

    def test_identical_generations_retain_separate_job_metadata(self):
        first = self.add(prompt="first")
        second = self.add(prompt="second")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.store.overview()["total"], 2)

    def test_search_tags_dates_orientation_and_literal_wildcards(self):
        first = self.add(prompt="森林 100% 质量", model="model-a", user_name="小明")
        second = self.store.add_image(
            picture("navy", (200, 400)), {"prompt": "forest_2", "model": "model-b"}
        )["image"]
        self.store.update_image(second["id"], {"tags": ["壁纸"], "notes": "雨夜"})
        self.assertEqual(
            self.store.list_images(query="%")["items"][0]["id"], first["id"]
        )
        self.assertEqual(self.store.list_images(query="_")["total"], 1)
        self.assertEqual(self.store.list_images(query="壁纸")["total"], 1)
        self.assertEqual(self.store.list_images(query="小明")["total"], 1)
        self.assertEqual(self.store.list_images(query="雨夜")["total"], 1)
        self.assertEqual(
            self.store.list_images(tag="壁纸", model="model-b", orientation="portrait")[
                "total"
            ],
            1,
        )
        self.assertEqual(self.store.list_images(orientation="square")["total"], 0)
        self.assertEqual(
            self.store.list_images(
                after=first["created_at"], before=second["created_at"]
            )["total"],
            1,
        )
        self.assertEqual(self.store.list_images(query="' OR 1=1 --")["total"], 0)

    def test_album_delete_keeps_originals_and_other_memberships(self):
        image = self.add()
        first = self.store.save_album("风景")
        second = self.store.save_album("待整理")
        self.store.update_image(image["id"], {"album_ids": [first["id"], second["id"]]})
        self.assertEqual(self.store.list_images(album_id=first["id"])["total"], 1)
        self.assertEqual(self.store.overview()["unfiled"], 0)
        self.store.delete_album(first["id"])
        self.assertEqual(self.store.get_image(image["id"])["albums"], [second])
        self.assertTrue(self.store.original(image["id"])[0].exists())
        self.store.batch([image["id"]], "remove_album", value=second["id"])
        self.assertEqual(self.store.overview()["unfiled"], 1)

    def test_album_names_are_unique_and_rename_is_persistent(self):
        album = self.store.save_album("Landscape")
        with self.assertRaises(GalleryError):
            self.store.save_album("LANDSCAPE")
        self.store.save_album("旅途", album["id"])
        self.assertEqual(
            GalleryStore(self.root).overview()["albums"][0]["name"], "旅途"
        )

    def test_trash_restore_and_purge_preserve_then_remove_files(self):
        image = self.add()
        image_id = image["id"]
        self.store.update_image(image_id, {"favorite": True, "tags": ["风景"]})
        path, _ = self.store.original(image_id)
        used = self.store.overview()["storage_bytes"]
        with self.assertRaises(GalleryError):
            self.store.batch([image_id], "purge")
        self.store.batch([image_id], "trash")
        self.assertTrue(path.exists())
        self.assertEqual(self.store.list_images()["total"], 0)
        self.assertEqual(self.store.list_images(view="trash")["total"], 1)
        self.assertEqual(self.store.overview()["trash_bytes"], used)
        with self.assertRaises(GalleryError):
            self.store.update_image(image_id, {"title": "change"})
        self.store.batch([image_id], "restore")
        self.assertEqual(self.store.list_images(view="favorites")["total"], 1)
        self.assertEqual(self.store.get_image(image_id)["tags"], ["风景"])
        self.store.batch([image_id], "trash")
        self.store.batch([image_id], "purge")
        self.assertFalse(path.exists())
        self.assertEqual(list((self.root / "previews").iterdir()), [])
        self.assertEqual(list((self.root / "thumbnails").iterdir()), [])
        self.assertEqual(self.store.overview()["storage_bytes"], 0)

    def test_batch_validation_is_atomic(self):
        first, second = self.add(), self.add("navy")
        self.store.update_image(first["id"], {"tags": [f"tag{i}" for i in range(20)]})
        with self.assertRaises(GalleryError):
            self.store.batch([second["id"], first["id"]], "add_tags", value=["extra"])
        self.assertEqual(self.store.get_image(second["id"])["tags"], [])
        with self.assertRaises(ImageNotFound):
            self.store.batch([first["id"], "a" * 32], "trash")
        self.assertEqual(self.store.overview()["total"], 2)

    def test_invalid_ids_payloads_and_formats_cannot_read_or_write_paths(self):
        for value in ["../outside", "a/" + "b" * 32, "a" * 31, None, 5, ["a"]]:
            with self.subTest(value=value), self.assertRaises(GalleryError):
                self.store.get_image(value)
        for data in [b"", b"<svg onload='alert(1)'></svg>", b"not an image"]:
            with self.assertRaises(GalleryError):
                self.store.add_image(data, source="imported")
        image = self.add()
        for changes in [
            {"album_ids": None},
            {"favorite": "yes"},
            {"tags": "bad"},
            {"title": " "},
            {"model": "override"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(GalleryError):
                self.store.update_image(image["id"], changes)
        result = self.store.add_image(
            picture("red"), source="imported", filename="../../outside.png"
        )
        self.assertTrue(
            self.store.original(result["image"]["id"])[0].is_relative_to(self.root)
        )
        self.assertEqual(result["image"]["original_name"], "outside.png")

    def test_capacity_includes_trash_and_does_not_delete_old_images(self):
        image = self.add()
        before = self.store.overview()["storage_bytes"]
        self.store.max_storage_bytes = before + 1
        self.store.batch([image["id"]], "trash")
        with self.assertRaisesRegex(GalleryError, "上限"):
            self.add("navy")
        self.assertTrue(self.store.original(image["id"])[0].exists())
        self.assertEqual(self.store.overview()["storage_bytes"], before)

    def test_upload_size_limit_and_export_limit(self):
        self.store.max_upload_bytes = 10
        with self.assertRaises(GalleryError):
            self.store.add_image(picture(), source="imported")
        image = self.add()
        with (
            patch.object(self.store, "MAX_EXPORT_BYTES", 1),
            self.assertRaises(GalleryError),
        ):
            self.store.export_zip([image["id"]])

    def test_failed_database_insert_cleans_up_new_files(self):
        with sqlite3.connect(self.store.db_path) as db:
            db.execute(
                "CREATE TRIGGER reject_image BEFORE INSERT ON images BEGIN SELECT RAISE(ABORT, 'test failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.add()
        for folder in ("originals", "thumbnails", "previews"):
            self.assertEqual(list((self.root / folder).iterdir()), [])

    def test_concurrent_imports_deduplicate_and_generations_are_not_lost(self):
        data = picture()
        with ThreadPoolExecutor(max_workers=6) as pool:
            imports = list(
                pool.map(
                    lambda _: self.store.add_image(data, source="imported"), range(6)
                )
            )
            generated = list(
                pool.map(
                    lambda i: self.store.add_image(data, {"prompt": str(i)}), range(6)
                )
            )
        self.assertEqual(sum(not item["duplicate"] for item in imports), 1)
        self.assertEqual(len({item["image"]["id"] for item in generated}), 6)
        self.assertEqual(self.store.overview()["total"], 7)

    def test_export_has_exact_original_and_portable_metadata(self):
        original = picture()
        image = self.store.add_image(
            original, {"title": "../作品/<>?", "prompt": "森林里的小屋"}
        )["image"]
        self.store.update_image(image["id"], {"tags": ["山林"], "notes": "灵感"})
        with (
            self.store.export_zip([image["id"]]) as stream,
            zipfile.ZipFile(stream) as archive,
        ):
            names = archive.namelist()
            self.assertEqual(len(names), 2)
            self.assertFalse(any(".." in name.split("/") for name in names))
            self.assertEqual(archive.read(names[0]), original)
            metadata = json.loads(archive.read("metadata.json"))
            self.assertEqual(metadata["images"][0]["prompt"], "森林里的小屋")
            self.assertEqual(metadata["images"][0]["tags"], ["山林"])
            self.assertNotIn("api_key", json.dumps(metadata))
            self.assertNotIn(str(self.root), json.dumps(metadata))

    def test_pagination_bounds_missing_thumbnail_and_sort(self):
        images = [
            self.add(color, title=title)
            for color, title in [("red", "z"), ("blue", "a"), ("green", "c")]
        ]
        self.assertEqual(self.store.list_images(sort="name")["items"][0]["title"], "a")
        self.assertEqual(self.store.list_images(page=99, page_size=2)["page"], 2)
        for kwargs in [
            {"page_size": 1000},
            {"sort": "DROP TABLE"},
            {"after": float("nan")},
            {"after": 10, "before": 1},
        ]:
            with self.assertRaises(GalleryError):
                self.store.list_images(**kwargs)
        (self.root / "thumbnails" / f"{images[0]['id']}.webp").unlink()
        result = self.store.thumbnails([image["id"] for image in images])
        self.assertEqual(result["missing"], [images[0]["id"]])
        self.assertEqual(len(result["items"]), 2)


if __name__ == "__main__":
    unittest.main()
