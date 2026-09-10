"""Persistent image library. All public methods are synchronous; call via to_thread.

Only generated identifiers become filesystem paths. Originals are immutable, and
moving a picture to the trash only changes its database record.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import warnings
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, ClassVar

from PIL import Image, ImageOps, UnidentifiedImageError


class GalleryError(ValueError):
    """A validation or library error safe to show to the page."""


class ImageNotFound(GalleryError):
    pass


class GalleryStore:
    FORMATS: ClassVar = {
        "PNG": ("png", "image/png"),
        "JPEG": ("jpg", "image/jpeg"),
        "WEBP": ("webp", "image/webp"),
        "GIF": ("gif", "image/gif"),
    }
    MAX_PIXELS = 40_000_000
    MAX_BATCH = 100
    MAX_EXPORT_BYTES = 256 * 1024 * 1024
    ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
    SORTS: ClassVar = {
        "newest": "i.created_at DESC, i.id DESC",
        "oldest": "i.created_at ASC, i.id ASC",
        "largest": "i.size_bytes DESC, i.id DESC",
        "name": "i.title COLLATE NOCASE ASC, i.id ASC",
    }

    def __init__(self, root: Path, *, max_storage_mb: int = 0, max_upload_mb: int = 25):
        self.root = Path(root).resolve()
        self.max_storage_bytes = max(0, int(max_storage_mb)) * 1024 * 1024
        self.max_upload_bytes = max(1, min(int(max_upload_mb), 100)) * 1024 * 1024
        self._lock = threading.RLock()
        for name in ("originals", "thumbnails", "previews"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "library.sqlite3"
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    original_name TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    command TEXT NOT NULL DEFAULT '',
                    resolution TEXT NOT NULL DEFAULT '',
                    aspect_ratio TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    user_name TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL CHECK(source IN ('generated','imported')),
                    reference_count INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    derived_bytes INTEGER NOT NULL,
                    animated INTEGER NOT NULL DEFAULT 0,
                    color TEXT NOT NULL,
                    favorite INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    deleted_at REAL
                );
                CREATE INDEX IF NOT EXISTS images_created ON images(deleted_at, created_at);
                CREATE INDEX IF NOT EXISTS images_hash ON images(sha256);
                CREATE INDEX IF NOT EXISTS images_model ON images(model);
                CREATE TABLE IF NOT EXISTS albums (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS image_albums (
                    image_id TEXT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
                    album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                    PRIMARY KEY(image_id, album_id)
                );
                CREATE INDEX IF NOT EXISTS image_albums_album ON image_albums(album_id);
                CREATE TABLE IF NOT EXISTS image_tags (
                    image_id TEXT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
                    tag TEXT NOT NULL,
                    PRIMARY KEY(image_id, tag)
                );
                CREATE INDEX IF NOT EXISTS image_tags_tag ON image_tags(tag);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @classmethod
    def validate_id(cls, value: Any) -> str:
        if not isinstance(value, str) or not cls.ID_PATTERN.fullmatch(value):
            raise GalleryError("无效的图片或相册 ID")
        return value

    @classmethod
    def validate_ids(cls, values: Any, *, limit: int | None = None) -> list[str]:
        if not isinstance(values, list) or not 1 <= len(values) <= (
            limit or cls.MAX_BATCH
        ):
            raise GalleryError(f"请选中 1–{limit or cls.MAX_BATCH} 张图片")
        return list(dict.fromkeys(cls.validate_id(value) for value in values))

    @staticmethod
    def _text(value: Any, name: str, limit: int, *, required: bool = False) -> str:
        if not isinstance(value, str) or len(value) > limit or "\x00" in value:
            raise GalleryError(f"{name}需为不超过 {limit} 字的文本")
        value = value.strip()
        if required and not value:
            raise GalleryError(f"{name}不能为空")
        return value

    @classmethod
    def validate_tags(cls, values: Any) -> list[str]:
        if not isinstance(values, list) or len(values) > 20:
            raise GalleryError("每张图片最多设置 20 个标签")
        return list(
            dict.fromkeys(cls._text(tag, "标签", 40, required=True) for tag in values)
        )

    def _path(self, image_id: str, variant: str, extension: str = "webp") -> Path:
        self.validate_id(image_id)
        if variant not in ("originals", "thumbnails", "previews"):
            raise GalleryError("无效的图片版本")
        if extension not in {item[0] for item in self.FORMATS.values()}:
            raise GalleryError("不支持的图片格式")
        path = self.root / variant / f"{image_id}.{extension}"
        # Also reject a symlink that was introduced outside the plugin.
        if not path.resolve().is_relative_to(self.root):
            raise GalleryError("图片路径不在图库目录内")
        return path

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".tmp", delete=False
            ) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, path)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    @classmethod
    def _inspect_image(cls, data: bytes) -> dict:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    if image.format not in cls.FORMATS:
                        raise GalleryError("仅支持 PNG、JPEG、WebP 和 GIF 图片")
                    if image.width * image.height > cls.MAX_PIXELS:
                        raise GalleryError("图片像素过大，最多支持 4000 万像素")
                    extension, mime_type = cls.FORMATS[image.format]
                    animated = bool(getattr(image, "is_animated", False))
                    image.verify()
                with Image.open(BytesIO(data)) as image:
                    oriented = ImageOps.exif_transpose(image)
                    width, height = oriented.size
                    pixels = oriented.convert(
                        "RGBA"
                        if "A" in oriented.getbands() or "transparency" in oriented.info
                        else "RGB"
                    )
                    rgb = pixels.convert("RGB").resize((1, 1)).getpixel((0, 0))
                    color = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
                    variants = {}
                    for name, size, quality in (
                        ("thumbnail", 480, 78),
                        ("preview", 1800, 88),
                    ):
                        sample = pixels.copy()
                        sample.thumbnail((size, size), Image.Resampling.LANCZOS)
                        output = BytesIO()
                        sample.save(output, format="WEBP", quality=quality, method=4)
                        variants[name] = output.getvalue()
                    return dict(
                        extension=extension,
                        mime_type=mime_type,
                        width=width,
                        height=height,
                        animated=animated,
                        color=color,
                        **variants,
                    )
        except GalleryError:
            raise
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            SyntaxError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            raise GalleryError("无法读取图片，文件可能已损坏或像素过大") from exc

    def add_image(
        self,
        data: bytes,
        metadata: dict | None = None,
        *,
        source: str = "generated",
        filename: str = "",
    ) -> dict:
        if source not in ("generated", "imported"):
            raise GalleryError("无效的图片来源")
        # Generated 4K PNGs may exceed the upload setting; both paths have a hard ceiling.
        max_bytes = self.max_upload_bytes if source == "imported" else 100 * 1024 * 1024
        if not isinstance(data, bytes) or not data or len(data) > max_bytes:
            raise GalleryError(f"图片为空或超过 {max_bytes // 1024 // 1024} MB 限制")
        metadata = metadata or {}
        digest = hashlib.sha256(data).hexdigest()
        with self._lock, self._connect() as db:
            # Re-importing a file is idempotent. Generated results retain each job's metadata.
            if source == "imported":
                existing = db.execute(
                    "SELECT * FROM images WHERE sha256=? ORDER BY deleted_at IS NULL DESC LIMIT 1",
                    (digest,),
                ).fetchone()
                if existing:
                    return {
                        "image": self._serialize(db, [existing], detail=True)[0],
                        "duplicate": True,
                    }
            info = self._inspect_image(data)
            size = len(data) + len(info["thumbnail"]) + len(info["preview"])
            self._check_storage(db, size)
            image_id = uuid.uuid4().hex
            # Keep user filenames as display metadata only, never as disk paths.
            filename = (
                str(filename)
                .replace("\\", "/")
                .rsplit("/", 1)[-1][:240]
                .replace("\x00", "")
            )
            prompt = str(metadata.get("prompt") or "").replace("\x00", "")
            title = str(
                metadata.get("title")
                or (Path(filename).stem if filename else "")
                or prompt.replace("\n", " ")[:60]
                or "未命名图片"
            )[:200]
            fields = {
                "id": image_id,
                "sha256": digest,
                "extension": info["extension"],
                "mime_type": info["mime_type"],
                "title": title,
                "original_name": filename,
                "prompt": prompt,
                "source": source,
                "width": info["width"],
                "height": info["height"],
                "size_bytes": len(data),
                "derived_bytes": size - len(data),
                "animated": int(info["animated"]),
                "color": info["color"],
                "created_at": time.time(),
            }
            for key in (
                "model",
                "provider",
                "command",
                "resolution",
                "aspect_ratio",
                "user_id",
                "user_name",
                "group_id",
            ):
                fields[key] = str(metadata.get(key) or "")[:300].replace("\x00", "")
            for key in ("reference_count", "duration_ms"):
                fields[key] = max(0, int(metadata.get(key) or 0))
            paths = [
                self._path(image_id, "originals", info["extension"]),
                self._path(image_id, "thumbnails"),
                self._path(image_id, "previews"),
            ]
            try:
                for path, content in zip(
                    paths, (data, info["thumbnail"], info["preview"])
                ):
                    self._atomic_write(path, content)
                columns = ",".join(fields)
                placeholders = ",".join("?" for _ in fields)
                db.execute(
                    f"INSERT INTO images ({columns}) VALUES ({placeholders})",
                    tuple(fields.values()),
                )
                # Commit before leaving the cleanup scope so failed commits remove new files.
                db.commit()
            except BaseException:
                for path in paths:
                    path.unlink(missing_ok=True)
                raise
            row = db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
            return {
                "image": self._serialize(db, [row], detail=True)[0],
                "duplicate": False,
            }

    def _check_storage(self, db: sqlite3.Connection, incoming: int) -> None:
        used = db.execute(
            "SELECT COALESCE(SUM(size_bytes + derived_bytes),0) FROM images"
        ).fetchone()[0]
        if self.max_storage_bytes and used + incoming > self.max_storage_bytes:
            raise GalleryError("图库空间已达上限，请清理回收站或调整图库容量配置")

    def _serialize(
        self, db: sqlite3.Connection, rows: list, *, detail: bool = False
    ) -> list[dict]:
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        marks = ",".join("?" for _ in ids)
        tags, albums = defaultdict(list), defaultdict(list)
        for row in db.execute(
            f"SELECT image_id, tag FROM image_tags WHERE image_id IN ({marks}) ORDER BY tag",
            ids,
        ):
            tags[row["image_id"]].append(row["tag"])
        for row in db.execute(
            f"SELECT ia.image_id, a.id, a.name FROM image_albums ia JOIN albums a ON a.id=ia.album_id WHERE ia.image_id IN ({marks}) ORDER BY a.name",
            ids,
        ):
            albums[row["image_id"]].append({"id": row["id"], "name": row["name"]})
        result = []
        for row in rows:
            item = dict(row)
            item.pop("sha256", None)
            item.pop("derived_bytes", None)
            item["favorite"] = bool(item["favorite"])
            item["animated"] = bool(item["animated"])
            item["tags"] = tags[item["id"]]
            item["albums"] = albums[item["id"]]
            if not detail:
                item["prompt_excerpt"] = item.pop("prompt")[:160]
                item.pop("notes", None)
            result.append(item)
        return result

    def get_image(self, image_id: str) -> dict:
        self.validate_id(image_id)
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
            if row is None:
                raise ImageNotFound("图片不存在，可能已被永久删除")
            return self._serialize(db, [row], detail=True)[0]

    @staticmethod
    def _literal_like(value: str) -> str:
        return (
            "%"
            + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )

    def list_images(
        self,
        *,
        page: int = 1,
        page_size: int = 48,
        query: str = "",
        view: str = "all",
        album_id: str = "",
        tag: str = "",
        model: str = "",
        source: str = "",
        orientation: str = "",
        after: float | None = None,
        before: float | None = None,
        sort: str = "newest",
    ) -> dict:
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or not 1 <= page <= 1_000_000
        ):
            raise GalleryError("无效的页码")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise GalleryError("每页图片数量需在 1–100 之间")
        if (
            view not in ("all", "favorites", "trash", "unfiled")
            or sort not in self.SORTS
        ):
            raise GalleryError("无效的视图或排序方式")
        if source not in ("", "generated", "imported") or orientation not in (
            "",
            "landscape",
            "portrait",
            "square",
        ):
            raise GalleryError("无效的筛选条件")
        query = self._text(query, "搜索关键词", 300)
        tag = self._text(tag, "标签", 40)
        model = self._text(model, "模型", 300)
        where = [
            "i.deleted_at IS NOT NULL" if view == "trash" else "i.deleted_at IS NULL"
        ]
        params: list[Any] = []
        if view == "favorites":
            where.append("i.favorite=1")
        if view == "unfiled":
            where.append(
                "NOT EXISTS(SELECT 1 FROM image_albums ia WHERE ia.image_id=i.id)"
            )
        if query:
            pattern = self._literal_like(query)
            cols = (
                "title",
                "prompt",
                "notes",
                "model",
                "user_name",
                "user_id",
                "original_name",
                "provider",
                "group_id",
            )
            search = [f"i.{column} LIKE ? ESCAPE '\\'" for column in cols]
            search.append(
                "EXISTS(SELECT 1 FROM image_tags t WHERE t.image_id=i.id AND t.tag LIKE ? ESCAPE '\\')"
            )
            where.append("(" + " OR ".join(search) + ")")
            params.extend([pattern] * (len(cols) + 1))
        if album_id:
            self.validate_id(album_id)
            where.append(
                "EXISTS(SELECT 1 FROM image_albums ia WHERE ia.image_id=i.id AND ia.album_id=?)"
            )
            params.append(album_id)
        if tag:
            where.append(
                "EXISTS(SELECT 1 FROM image_tags t WHERE t.image_id=i.id AND t.tag=?)"
            )
            params.append(tag)
        for column, value in (("model", model), ("source", source)):
            if value:
                where.append(f"i.{column}=?")
                params.append(value)
        if orientation:
            where.append(
                {
                    "landscape": "i.width > i.height",
                    "portrait": "i.width < i.height",
                    "square": "i.width = i.height",
                }[orientation]
            )
        for bound, op in ((after, ">="), (before, "<")):
            if bound is not None:
                if (
                    not isinstance(bound, (int, float))
                    or not 0 <= bound <= 253402300799
                ):
                    raise GalleryError("无效的日期范围")
                where.append(f"i.created_at {op} ?")
                params.append(bound)
        if after is not None and before is not None and after >= before:
            raise GalleryError("开始日期不能晚于结束日期")
        clause = " AND ".join(where)
        with self._lock, self._connect() as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM images i WHERE {clause}", params
            ).fetchone()[0]
            page = min(page, max(1, (total + page_size - 1) // page_size))
            rows = db.execute(
                f"SELECT i.* FROM images i WHERE {clause} ORDER BY {self.SORTS[sort]} LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return {
                "items": self._serialize(db, rows),
                "total": total,
                "page": page,
                "page_size": page_size,
            }

    def overview(self) -> dict:
        with self._lock, self._connect() as db:
            row = db.execute("""SELECT
                COUNT(CASE WHEN deleted_at IS NULL THEN 1 END) AS total,
                COUNT(CASE WHEN deleted_at IS NULL AND favorite=1 THEN 1 END) AS favorites,
                COUNT(CASE WHEN deleted_at IS NOT NULL THEN 1 END) AS trash,
                COUNT(CASE WHEN deleted_at IS NULL AND source='generated' THEN 1 END) AS generated,
                COUNT(CASE WHEN deleted_at IS NULL AND source='imported' THEN 1 END) AS imported,
                COALESCE(SUM(size_bytes + derived_bytes),0) AS storage_bytes,
                COALESCE(SUM(CASE WHEN deleted_at IS NOT NULL THEN size_bytes + derived_bytes ELSE 0 END),0) AS trash_bytes
                FROM images""").fetchone()
            data = dict(row)
            data["unfiled"] = db.execute(
                "SELECT COUNT(*) FROM images i WHERE deleted_at IS NULL AND NOT EXISTS(SELECT 1 FROM image_albums ia WHERE ia.image_id=i.id)"
            ).fetchone()[0]
            data["albums"] = [
                dict(row)
                for row in db.execute("""SELECT a.id, a.name,
                COUNT(CASE WHEN i.deleted_at IS NULL THEN i.id END) AS count
                FROM albums a LEFT JOIN image_albums ia ON a.id=ia.album_id
                LEFT JOIN images i ON i.id=ia.image_id GROUP BY a.id ORDER BY a.name COLLATE NOCASE""")
            ]
            data["tags"] = [
                dict(row)
                for row in db.execute("""SELECT t.tag AS name, COUNT(*) AS count
                FROM image_tags t JOIN images i ON i.id=t.image_id WHERE i.deleted_at IS NULL
                GROUP BY t.tag ORDER BY count DESC, t.tag LIMIT 100""")
            ]
            data["models"] = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT model FROM images WHERE model<>'' ORDER BY model"
                )
            ]
            data["storage_limit_bytes"] = self.max_storage_bytes
            data["max_upload_bytes"] = self.max_upload_bytes
            data["max_batch"] = self.MAX_BATCH
            return data

    def update_image(self, image_id: str, changes: dict) -> dict:
        self.validate_id(image_id)
        if (
            not isinstance(changes, dict)
            or not changes
            or set(changes) - {"title", "notes", "favorite", "tags", "album_ids"}
        ):
            raise GalleryError("无效的图片编辑字段")
        updates = {}
        for field, limit in (("title", 200), ("notes", 4000)):
            if field in changes:
                updates[field] = self._text(
                    changes[field],
                    "名称" if field == "title" else "备注",
                    limit,
                    required=field == "title",
                )
        if "favorite" in changes:
            if not isinstance(changes["favorite"], bool):
                raise GalleryError("收藏状态需为布尔值")
            updates["favorite"] = int(changes["favorite"])
        tags = self.validate_tags(changes["tags"]) if "tags" in changes else None
        album_ids = changes.get("album_ids")
        if "album_ids" in changes:
            if not isinstance(album_ids, list) or len(album_ids) > 30:
                raise GalleryError("每张图片最多加入 30 个相册")
            album_ids = list(
                dict.fromkeys(self.validate_id(value) for value in album_ids)
            )
        with self._lock, self._connect() as db:
            rows = self._require_images(db, [image_id])
            if rows[0]["deleted_at"] is not None:
                raise GalleryError("请先从回收站恢复图片，再进行编辑")
            if album_ids is not None:
                for album_id in album_ids:
                    self._require_album(db, album_id)
            if updates:
                db.execute(
                    "UPDATE images SET "
                    + ",".join(f"{field}=?" for field in updates)
                    + " WHERE id=?",
                    [*updates.values(), image_id],
                )
            if tags is not None:
                db.execute("DELETE FROM image_tags WHERE image_id=?", (image_id,))
                db.executemany(
                    "INSERT INTO image_tags VALUES (?,?)",
                    [(image_id, tag) for tag in tags],
                )
            if album_ids is not None:
                db.execute("DELETE FROM image_albums WHERE image_id=?", (image_id,))
                db.executemany(
                    "INSERT INTO image_albums VALUES (?,?)",
                    [(image_id, album_id) for album_id in album_ids],
                )
            row = db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
            return self._serialize(db, [row], detail=True)[0]

    @staticmethod
    def _require_images(db: sqlite3.Connection, ids: list[str]) -> list:
        marks = ",".join("?" for _ in ids)
        rows = db.execute(f"SELECT * FROM images WHERE id IN ({marks})", ids).fetchall()
        if len(rows) != len(ids):
            raise ImageNotFound("部分图片已不存在，请刷新图库后重试")
        return rows

    @staticmethod
    def _require_album(db: sqlite3.Connection, album_id: str) -> None:
        if not db.execute("SELECT 1 FROM albums WHERE id=?", (album_id,)).fetchone():
            raise ImageNotFound("相册不存在，请刷新后重试")

    def batch(self, ids: list[str], action: str, *, value: Any = None) -> dict:
        ids = self.validate_ids(ids)
        if action not in (
            "favorite",
            "trash",
            "restore",
            "purge",
            "add_album",
            "remove_album",
            "add_tags",
        ):
            raise GalleryError("不支持的批量操作")
        if action == "favorite" and not isinstance(value, bool):
            raise GalleryError("收藏状态需为布尔值")
        if action in ("add_album", "remove_album"):
            self.validate_id(value)
        if action == "add_tags":
            value = self.validate_tags(value)
        with self._lock, self._connect() as db:
            rows = self._require_images(db, ids)
            if action == "purge" and any(row["deleted_at"] is None for row in rows):
                raise GalleryError("只能永久删除回收站中的图片，请先移入回收站")
            if action not in ("restore", "purge", "trash") and any(
                row["deleted_at"] is not None for row in rows
            ):
                raise GalleryError("请先从回收站恢复图片，再进行编辑")
            marks = ",".join("?" for _ in ids)
            if action == "favorite":
                db.execute(
                    f"UPDATE images SET favorite=? WHERE id IN ({marks})",
                    [int(value), *ids],
                )
            elif action in ("trash", "restore"):
                if action == "trash":
                    db.execute(
                        f"UPDATE images SET deleted_at=COALESCE(deleted_at, ?) WHERE id IN ({marks})",
                        [time.time(), *ids],
                    )
                else:
                    db.execute(
                        f"UPDATE images SET deleted_at=NULL WHERE id IN ({marks})", ids
                    )
            elif action == "purge":
                for row in rows:
                    for variant, extension in (
                        ("originals", row["extension"]),
                        ("thumbnails", "webp"),
                        ("previews", "webp"),
                    ):
                        self._path(row["id"], variant, extension).unlink(
                            missing_ok=True
                        )
                db.execute(f"DELETE FROM images WHERE id IN ({marks})", ids)
            elif action in ("add_album", "remove_album"):
                self._require_album(db, value)
                if action == "add_album":
                    for image_id in ids:
                        count = db.execute(
                            "SELECT COUNT(*) FROM image_albums WHERE image_id=? AND album_id<>?",
                            (image_id, value),
                        ).fetchone()[0]
                        if count >= 30:
                            raise GalleryError("每张图片最多加入 30 个相册")
                    db.executemany(
                        "INSERT OR IGNORE INTO image_albums VALUES (?,?)",
                        [(image_id, value) for image_id in ids],
                    )
                else:
                    db.execute(
                        f"DELETE FROM image_albums WHERE album_id=? AND image_id IN ({marks})",
                        [value, *ids],
                    )
            elif action == "add_tags":
                for image_id in ids:
                    existing = {
                        row[0]
                        for row in db.execute(
                            "SELECT tag FROM image_tags WHERE image_id=?", (image_id,)
                        )
                    }
                    if len(existing | set(value)) > 20:
                        raise GalleryError("部分图片的标签超过 20 个，请减少标签后重试")
                db.executemany(
                    "INSERT OR IGNORE INTO image_tags VALUES (?,?)",
                    [(image_id, tag) for image_id in ids for tag in value],
                )
            return {"affected": len(ids)}

    def save_album(self, name: str, album_id: str | None = None) -> dict:
        name = self._text(name, "相册名称", 60, required=True)
        if album_id is not None:
            self.validate_id(album_id)
        with self._lock, self._connect() as db:
            try:
                if album_id:
                    self._require_album(db, album_id)
                    db.execute("UPDATE albums SET name=? WHERE id=?", (name, album_id))
                else:
                    album_id = uuid.uuid4().hex
                    db.execute(
                        "INSERT INTO albums VALUES (?,?,?)",
                        (album_id, name, time.time()),
                    )
            except sqlite3.IntegrityError as exc:
                raise GalleryError("已存在同名相册") from exc
            return {"id": album_id, "name": name}

    def delete_album(self, album_id: str) -> dict:
        self.validate_id(album_id)
        with self._lock, self._connect() as db:
            self._require_album(db, album_id)
            db.execute("DELETE FROM albums WHERE id=?", (album_id,))
            return {"deleted": True}

    def image_data_url(self, image_id: str, *, preview: bool = False) -> dict:
        with self._lock:
            image = self.get_image(image_id)
            path = self._path(image_id, "previews" if preview else "thumbnails")
            if not path.is_file():
                raise ImageNotFound("预览文件不存在，请下载原图查看")
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return {"id": image["id"], "data_url": "data:image/webp;base64," + encoded}

    def thumbnails(self, ids: list[str]) -> dict:
        ids = self.validate_ids(ids, limit=12)
        items, missing = [], []
        with self._lock:
            for image_id in ids:
                try:
                    items.append(self.image_data_url(image_id))
                except ImageNotFound:
                    missing.append(image_id)
        return {"items": items, "missing": missing}

    def original(self, image_id: str) -> tuple[Path, dict]:
        with self._lock:
            image = self.get_image(image_id)
            path = self._path(image_id, "originals", image["extension"])
            if not path.is_file():
                raise ImageNotFound("原图文件不存在")
            return path, image

    @staticmethod
    def download_name(image: dict) -> str:
        title = (
            re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", image["title"]).strip(" .")[:70]
            or "image"
        )
        return f"{title}-{image['id'][:8]}.{image['extension']}"

    def export_zip(self, ids: list[str]) -> BinaryIO:
        ids = self.validate_ids(ids)
        with self._lock:
            originals = [self.original(image_id) for image_id in ids]
            if (
                sum(image["size_bytes"] for _, image in originals)
                > self.MAX_EXPORT_BYTES
            ):
                raise GalleryError("单次打包最多 256 MB，请分批下载")
            # The bridge transfers binary downloads through the parent, never a public URL.
            output = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 — caller owns and closes the stream
            try:
                with zipfile.ZipFile(
                    output, "w", compression=zipfile.ZIP_STORED
                ) as archive:
                    for path, image in originals:
                        archive.write(path, "images/" + self.download_name(image))
                    manifest = {
                        "version": 1,
                        "exported_at": datetime.now(timezone.utc).isoformat(),
                        "images": [image for _, image in originals],
                    }
                    archive.writestr(
                        "metadata.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2),
                    )
                output.seek(0)
                return output
            except BaseException:
                output.close()
                raise
