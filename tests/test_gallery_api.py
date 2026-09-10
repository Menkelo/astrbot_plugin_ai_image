from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from gallery_store import GalleryStore
from tests.test_gallery_store import picture
from tests.web_support import load_web_api, make_app

try:
    from fastapi.testclient import TestClient

    WEB = load_web_api()
    REASON = ""
except ImportError as exc:
    WEB = None
    REASON = str(exc)


@unittest.skipIf(WEB is None, REASON)
class GalleryAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = GalleryStore(Path(self.temp.name))
        app, self.context, self.api, self.api_class = make_app(self.store, WEB)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer local-test-session"}
        self.prefix = "/api/astrbot_plugin_ai_image/gallery"

    def get(self, endpoint, **kwargs):
        return self.client.get(self.prefix + endpoint, headers=self.headers, **kwargs)

    def post(self, endpoint, payload):
        return self.client.post(
            self.prefix + endpoint, headers=self.headers, json=payload
        )

    def test_all_routes_require_authenticated_dashboard_identity(self):
        self.assertEqual(self.client.get(self.prefix + "/overview").status_code, 401)
        self.assertEqual(
            self.client.post(self.prefix + "/batch", json={}).status_code, 401
        )
        self.assertEqual(self.get("/overview").status_code, 200)

    def test_import_preview_and_original_download_use_actual_bytes(self):
        data = picture()
        response = self.client.post(
            self.prefix + "/import",
            headers=self.headers,
            files={"file": ("landscape.png", data, "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        image = response.json()["image"]
        self.assertEqual(self.get("/images").json()["total"], 1)
        preview = self.get(f"/images/{image['id']}/preview")
        self.assertTrue(
            preview.json()["data_url"].startswith("data:image/webp;base64,")
        )
        download = self.get(f"/images/{image['id']}/download")
        self.assertEqual(download.content, data)
        self.assertEqual(download.headers["content-type"], "image/png")
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(download.headers["cache-control"], "no-store")

    def test_invalid_requests_return_public_errors_without_tracebacks(self):
        for endpoint, payload in [
            ("/batch", []),
            ("/batch", {"ids": ["../secret"], "action": "purge"}),
            ("/thumbnails", {"ids": []}),
            ("/albums/save", {"name": None}),
        ]:
            result = self.post(endpoint, payload)
            self.assertEqual(result.status_code, 400, result.text)
            self.assertEqual(result.json()["status"], "error")
            self.assertNotIn("Traceback", result.text)
        self.assertEqual(
            self.get("/images", params={"page_size": "oops"}).status_code, 400
        )
        self.assertEqual(self.get("/images", params={"after": "nan"}).status_code, 400)
        self.assertEqual(self.get("/images/" + "a" * 32).status_code, 404)

    def test_batch_mutations_and_album_memberships_round_trip(self):
        image = self.store.add_image(picture())["image"]
        album = self.post("/albums/save", {"name": "壁纸"}).json()
        self.post(
            "/batch",
            {"ids": [image["id"]], "action": "add_album", "value": album["id"]},
        )
        update = self.post(
            f"/images/{image['id']}/update", {"favorite": True, "tags": ["风景"]}
        )
        self.assertTrue(update.json()["favorite"])
        self.assertEqual(
            self.get("/images", params={"album_id": album["id"], "tag": "风景"}).json()[
                "total"
            ],
            1,
        )
        self.post("/batch", {"ids": [image["id"]], "action": "trash"})
        self.assertEqual(self.get("/images").json()["total"], 0)
        self.post("/batch", {"ids": [image["id"]], "action": "restore"})
        self.assertEqual(
            self.get("/images", params={"view": "favorites"}).json()["total"], 1
        )

    def test_streamed_zip_includes_originals_and_manifest(self):
        image = self.store.add_image(picture(), {"prompt": "森林"})["image"]
        response = self.get("/export", params={"ids": image["id"]})
        self.assertEqual(
            response.status_code,
            200,
            response.text[:150] if response.status_code != 200 else "",
        )
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            self.assertEqual(metadata["images"][0]["prompt"], "森林")
            self.assertEqual(
                archive.read(
                    next(
                        name
                        for name in archive.namelist()
                        if name.startswith("images/")
                    )
                ),
                picture(),
            )

    def test_import_rejects_oversize_and_non_image_files(self):
        self.store.max_upload_bytes = 10
        result = self.client.post(
            self.prefix + "/import",
            headers=self.headers,
            files={"file": ("large.png", picture(), "image/png")},
        )
        self.assertEqual(result.status_code, 400)
        self.store.max_upload_bytes = 10000
        result = self.client.post(
            self.prefix + "/import",
            headers=self.headers,
            files={"file": ("fake.png", b"<html>unsafe</html>", "image/png")},
        )
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.store.overview()["total"], 0)

    def test_late_shutdown_does_not_unregister_reloaded_plugin(self):
        newer = self.api_class(self.context, self.store)
        self.api.close()
        self.assertEqual(len(self.context.registered_web_apis), 12)
        self.assertEqual(self.get("/overview").status_code, 200)
        newer.close()
        self.assertEqual(self.context.registered_web_apis, [])


if __name__ == "__main__":
    unittest.main()
