"""AstrBot Plugin Page endpoints. Imported only when astrbot.api.web is available."""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime, timezone

from astrbot.api.web import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)

from .gallery_store import GalleryError, GalleryStore, ImageNotFound

log = logging.getLogger(__name__)
PLUGIN_NAME = "astrbot_plugin_ai_image"
NO_CACHE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


class GalleryAPI:
    def __init__(self, context, store: GalleryStore, *, auto_archive: bool = True):
        self.context = context
        self.store = store
        self.auto_archive = auto_archive
        self.closed = False
        self._handlers = []
        routes = (
            ("overview", self.overview, "GET"),
            ("images", self.images, "GET"),
            ("images/<image_id>", self.image, "GET"),
            ("images/<image_id>/update", self.update, "POST"),
            ("images/<image_id>/preview", self.preview, "GET"),
            ("images/<image_id>/download", self.download, "GET"),
            ("thumbnails", self.thumbnails, "POST"),
            ("batch", self.batch, "POST"),
            ("import", self.import_image, "POST"),
            ("export", self.export, "GET"),
            ("albums/save", self.save_album, "POST"),
            ("albums/delete", self.delete_album, "POST"),
        )
        for endpoint, handler, method in routes:
            wrapped = self._guard(handler)
            context.register_web_api(
                f"/{PLUGIN_NAME}/gallery/{endpoint}", wrapped, [method], "AI 图片管理"
            )
            self._handlers.append(wrapped)

    def close(self) -> None:
        self.closed = True
        # Do not remove a newer instance's routes after a hot reload.
        registered = getattr(self.context, "registered_web_apis", None)
        if isinstance(registered, list):
            registered[:] = [
                item for item in registered if item[1] not in self._handlers
            ]
        self._handlers.clear()

    def _guard(self, handler):
        @functools.wraps(handler)
        async def guarded(**kwargs):
            if self.closed:
                return error_response(
                    "图库已关闭，请重载插件后刷新", status_code=503, headers=NO_CACHE
                )
            if not request.username:
                return error_response(
                    "请从已登录的 AstrBot WebUI 打开图库",
                    status_code=401,
                    headers=NO_CACHE,
                )
            try:
                return await handler(**kwargs)
            except ImageNotFound as exc:
                return error_response(str(exc), status_code=404, headers=NO_CACHE)
            except GalleryError as exc:
                return error_response(str(exc), status_code=400, headers=NO_CACHE)
            except Exception:
                log.exception("图片管理接口执行失败")
                return error_response(
                    "图库操作失败，请查看 AstrBot 日志后重试",
                    status_code=500,
                    headers=NO_CACHE,
                )

        return guarded

    @staticmethod
    async def _payload() -> dict:
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            raise GalleryError("请求内容必须是 JSON 对象")
        return payload

    @staticmethod
    def _integer(name: str, default: int) -> int:
        value = request.query.get(name)
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise GalleryError(f"无效的参数：{name}") from exc

    @staticmethod
    def _timestamp(name: str) -> float | None:
        value = request.query.get(name)
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise GalleryError("无效的日期范围") from exc

    async def overview(self):
        data = await asyncio.to_thread(self.store.overview)
        data["auto_archive"] = self.auto_archive
        return json_response(data, headers=NO_CACHE)

    async def images(self):
        query = request.query
        params = {
            key: query.get(key, "")
            for key in ("query", "album_id", "tag", "model", "source", "orientation")
        }
        params.update(
            page=self._integer("page", 1),
            page_size=self._integer("page_size", 48),
            view=query.get("view", "all"),
            sort=query.get("sort", "newest"),
            after=self._timestamp("after"),
            before=self._timestamp("before"),
        )
        data = await asyncio.to_thread(self.store.list_images, **params)
        return json_response(data, headers=NO_CACHE)

    async def image(self, image_id: str):
        return json_response(
            await asyncio.to_thread(self.store.get_image, image_id), headers=NO_CACHE
        )

    async def update(self, image_id: str):
        payload = await self._payload()
        return json_response(
            await asyncio.to_thread(self.store.update_image, image_id, payload),
            headers=NO_CACHE,
        )

    async def preview(self, image_id: str):
        return json_response(
            await asyncio.to_thread(self.store.image_data_url, image_id, preview=True),
            headers=NO_CACHE,
        )

    async def thumbnails(self):
        payload = await self._payload()
        return json_response(
            await asyncio.to_thread(self.store.thumbnails, payload.get("ids")),
            headers=NO_CACHE,
        )

    async def download(self, image_id: str):
        path, image = await asyncio.to_thread(self.store.original, image_id)
        return file_response(
            path,
            filename=self.store.download_name(image),
            content_type=image["mime_type"],
            headers=NO_CACHE,
        )

    async def batch(self):
        payload = await self._payload()
        result = await asyncio.to_thread(
            self.store.batch,
            payload.get("ids"),
            payload.get("action"),
            value=payload.get("value"),
        )
        return json_response(result, headers=NO_CACHE)

    async def import_image(self):
        upload = (await request.files()).get("file")
        if upload is None or not callable(getattr(upload, "read", None)):
            raise GalleryError("请选择需要导入的图片")
        # Read at most limit+1; multipart Content-Length is not a trustworthy file size.
        try:
            content = await upload.read(self.store.max_upload_bytes + 1)
        finally:
            await upload.close()
        if len(content) > self.store.max_upload_bytes:
            raise GalleryError(
                f"图片超过 {self.store.max_upload_bytes // 1024 // 1024} MB 限制"
            )
        result = await asyncio.to_thread(
            self.store.add_image,
            content,
            source="imported",
            filename=upload.filename or "",
            metadata={"user_name": request.username},
        )
        return json_response(result, headers=NO_CACHE)

    async def export(self):
        raw = request.query.get("ids", "")
        if len(raw) > self.store.MAX_BATCH * 33:
            raise GalleryError("导出图片数量过多")
        ids = self.store.validate_ids(raw.split(","))
        # Shield the thread so cancellation can close the temporary archive when it finishes.
        task = asyncio.create_task(asyncio.to_thread(self.store.export_zip, ids))
        try:
            archive = await asyncio.shield(task)
        except asyncio.CancelledError:

            def close_finished(finished):
                if not finished.cancelled() and finished.exception() is None:
                    finished.result().close()

            task.add_done_callback(close_finished)
            raise

        async def chunks():
            try:
                while True:
                    chunk = await asyncio.to_thread(archive.read, 256 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                archive.close()

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return stream_response(
            chunks(),
            content_type="application/zip",
            headers={
                **NO_CACHE,
                "Content-Disposition": f'attachment; filename="ai-images-{stamp}.zip"',
            },
        )

    async def save_album(self):
        payload = await self._payload()
        return json_response(
            await asyncio.to_thread(
                self.store.save_album, payload.get("name"), payload.get("id")
            ),
            headers=NO_CACHE,
        )

    async def delete_album(self):
        payload = await self._payload()
        return json_response(
            await asyncio.to_thread(self.store.delete_album, payload.get("id")),
            headers=NO_CACHE,
        )
