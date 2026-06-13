"""
AI Image Generation Module
封装 Gemini/OpenAI/Vertex AI 多 API 图像生成功能（单提供商，失败重试）
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import traceback
from io import BytesIO
from dataclasses import dataclass

import aiohttp
from PIL import Image

from astrbot.api import logger


@dataclass
class ProviderConfig:
    """提供商配置数据结构"""
    name: str
    api_type: str  # 'gemini' or 'openai' or 'vertex'
    base_url: str
    api_key: str
    model: str
    api_version: str = "v1beta1"
    location: str = "us-central1"
    vertex_keys: list[str] | None = None


class AIImageGenerator:
    """AI 图像生成器（支持 Gemini/OpenAI/Vertex）"""

    # 统一的宽高比映射表，供尺寸计算 / 比例推断复用
    RATIO_WH: dict[str, tuple[int, int]] = {
        "1:1": (1, 1),
        "16:9": (16, 9),
        "9:16": (9, 16),
        "4:3": (4, 3),
        "3:4": (3, 4),
        "3:2": (3, 2),
        "2:3": (2, 3),
        "4:5": (4, 5),
        "5:4": (5, 4),
        "21:9": (21, 9),
        "9:21": (9, 21),
    }

    # 这些 HTTP 状态码属于请求本身的问题（参数/鉴权/内容策略等），
    # 重试同样的请求不会成功，遇到时直接停止重试以节省超时等待。
    NON_RETRYABLE_CODES: frozenset[str] = frozenset(
        {"400", "401", "403", "404", "413", "415", "422"}
    )

    # Gemini/Vertex 返回 200 但因内容安全策略未产出图片时的 finishReason，
    # 属于内容被拦截，重试同样会被拦，故视为不可重试。
    GEMINI_BLOCK_REASONS: frozenset[str] = frozenset(
        {
            "SAFETY",
            "IMAGE_SAFETY",
            "PROHIBITED_CONTENT",
            "IMAGE_PROHIBITED_CONTENT",
            "BLOCKLIST",
            "RECITATION",
            "SPII",
        }
    )

    def __init__(
        self,
        main_config: ProviderConfig | None,
        timeout: int = 120,
        session: aiohttp.ClientSession | None = None,
        max_retries: int = 3,
        retry_delay: float = 1,
        vertex_start_idx: int = 0,
    ):
        self.main_config = main_config
        self.timeout = timeout
        self._session = session
        # 仅当会话由本实例创建时才负责关闭；注入的共享会话不在此关闭
        self._owns_session = session is None
        self.max_retries = max(1, int(max_retries))
        self.retry_delay = max(0, float(retry_delay))
        # 由调用方传入起始 Key 索引，实现 Vertex 多 Key 跨请求轮换
        self._vertex_idx = max(0, int(vertex_start_idx))

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close_session(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # =========================
    # Error / Response Helpers
    # =========================

    # HTTP 状态码 -> 用户可读说明
    CODE_DESC: dict[str, str] = {
        "400": "请求参数有误 (400)",
        "401": "API Key 无效或未授权 (401)",
        "403": "无访问权限或 Key 被拒 (403)",
        "404": "接口或模型不存在 (404)",
        "408": "请求超时 (408)",
        "409": "请求冲突 (409)",
        "413": "请求体过大 (413)",
        "415": "不支持的媒体类型 (415)",
        "422": "请求无法处理 (422)",
        "429": "请求过于频繁或配额已用尽 (429)",
        "500": "服务端内部错误 (500)",
        "502": "网关错误 (502)",
        "503": "服务暂不可用 (503)",
        "504": "网关超时 (504)",
    }

    def _short_api_error(self, error: str | None) -> str:
        """
        将详细错误压缩成用户可读的短错误，避免暴露完整 URL / 响应体。
        """
        err_str = str(error or "")

        # 内容安全拦截类消息已是可读文案，直接透传
        if "安全策略拦截" in err_str:
            return err_str

        status_match = (
            re.search(r"\bAPI\s+(\d{3})\b", err_str, re.IGNORECASE)
            or re.search(r"\bHTTP\s+(\d{3})\b", err_str, re.IGNORECASE)
            or re.search(r'"code"\s*:\s*(\d{3})', err_str)
            or re.search(r"'code'\s*:\s*(\d{3})", err_str)
        )

        if status_match:
            code = status_match.group(1)
            return self.CODE_DESC.get(code, f"API {code}")

        for code, desc in self.CODE_DESC.items():
            if code in err_str:
                return desc

        if "API 未返回图片" in err_str or "响应中未找到图片数据" in err_str:
            return "API 未返回图片"

        if (
            "SSL" in err_str
            or "ssl" in err_str
            or "SSLError" in err_str
            or "SSLV3_ALERT" in err_str
            or "CERTIFICATE_VERIFY_FAILED" in err_str
            or "WRONG_VERSION_NUMBER" in err_str
            or "DECRYPTION_FAILED_OR_BAD_RECORD_MAC" in err_str
            or "BAD_RECORD_MAC" in err_str
        ):
            return "SSL 连接失败"

        if (
            "Cannot connect" in err_str
            or "Connection reset" in err_str
            or "Connection refused" in err_str
            or "Connection closed" in err_str
            or "Server disconnected" in err_str
            or "ClientConnectorError" in err_str
            or "Name or service not known" in err_str
            or "Temporary failure in name resolution" in err_str
            or "getaddrinfo failed" in err_str
        ):
            return "网络连接失败"

        if (
            "请求超时" in err_str
            or "Timeout" in err_str
            or "timeout" in err_str
            or "asyncio.exceptions.TimeoutError" in err_str
        ):
            return "API 请求超时"

        if "NoneType" in err_str:
            return "API 响应解析失败"

        if "JSON解析失败" in err_str or "json" in err_str.lower():
            return "API 响应解析失败"

        if "未配置提供商" in err_str:
            return "未配置提供商"

        if "Vertex keys 未配置" in err_str:
            return "Vertex Key 未配置"

        return "API 请求失败"

    def _no_image_error(self, data: object | None = None) -> tuple[None, str]:
        """
        API 成功响应但没有图片时统一返回。
        """
        if data is not None:
            logger.warning(f"API 未返回图片，原始响应摘要: {str(data)[:300]}")
        return None, "API 未返回图片"

    def _is_image_bytes(self, b: bytes | None) -> bool:
        if not b:
            return False
        return (
            b.startswith(b"\xff\xd8")
            or b.startswith(b"\x89PNG")
            or b.startswith(b"GIF")
            or (b.startswith(b"RIFF") and len(b) > 12 and b[8:12] == b"WEBP")
            or b.startswith(b"\x00\x00\x00")  # 部分 heic/heif 容器
        )

    async def _read_response_payload(
        self,
        response: aiohttp.ClientResponse,
    ) -> tuple[object | None, str, bool, bytes]:
        """
        统一读取响应：
        返回:
          (data, raw_text, parse_ok, raw_bytes)

        支持：
        - 标准 JSON
        - text/event-stream / SSE: data: {...}
        - 非 JSON 文本
        - 直接图片 bytes
        """
        raw_bytes = await response.read()

        if not raw_bytes:
            return None, "", False, b""

        raw_text = raw_bytes.decode("utf-8", errors="replace")

        # 1) 标准 JSON
        try:
            return json.loads(raw_text), raw_text, True, raw_bytes
        except Exception:
            pass

        # 2) SSE / event-stream: data: {...}
        sse_items = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue

            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue

            try:
                sse_items.append(json.loads(payload))
            except Exception:
                continue

        if sse_items:
            return sse_items, raw_text, True, raw_bytes

        return None, raw_text, False, raw_bytes

    def _try_decode_image_base64(self, value: str) -> bytes | None:
        """
        尝试把字符串当作图片 base64 解码。
        支持：
        - 纯 base64
        - data:image/png;base64,...
        """
        if not value or not isinstance(value, str):
            return None

        s = value.strip()

        m = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", s, re.DOTALL)
        if m:
            s = m.group(2).strip()

        if len(s) < 100:
            return None

        try:
            clean = (
                s.replace("\n", "")
                .replace("\r", "")
                .replace("\t", "")
                .replace(" ", "")
                .replace("-", "+")
                .replace("_", "/")
            )
            pad = (4 - len(clean) % 4) % 4
            clean += "=" * pad

            b = base64.b64decode(clean, validate=False)
            return b if self._is_image_bytes(b) else None
        except Exception:
            return None

    async def _extract_images_from_text(self, text: str) -> list[bytes]:
        """
        从非标准文本里提取图片：
        - data:image/...;base64,...
        - markdown 图片 URL
        - 普通 http(s) 图片 URL
        """
        images: list[bytes] = []
        if not text:
            return images

        data_url_pattern = re.compile(
            r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=_\-\s\r\n]+)",
            re.IGNORECASE,
        )
        for m in data_url_pattern.finditer(text):
            full = f"data:{m.group(1)};base64,{m.group(2)}"
            b = self._try_decode_image_base64(full)
            if b:
                images.append(b)

        md_urls = re.findall(r"!\[[^\]]*?\]\((https?://[^\s)]+)\)", text)
        for url in md_urls:
            if d := await self._download_url(url):
                images.append(d)

        urls = re.findall(r"https?://[^\s\"'<>)]+", text)
        for url in urls:
            if url in md_urls:
                continue

            lower = url.lower()
            if any(x in lower for x in [".png", ".jpg", ".jpeg", ".webp", ".gif", "image", "img"]):
                if d := await self._download_url(url):
                    images.append(d)

        return images

    async def _extract_any_image(
        self,
        data: object | None,
        raw_text: str = "",
    ) -> list[bytes] | None:
        """
        尽可能从任意返回结构中提取图片。
        兼容：
        - OpenAI images: data[].b64_json / data[].url
        - OpenAI chat: choices[].message.content
        - Gemini: candidates[].content.parts[].inline_data
        - Responses API 风格: output[].content[]
        - 中转自定义字段: image/url/base64/images/result/output 等
        - SSE 数组
        - 原始文本中的 data URL / markdown URL
        """
        images: list[bytes] = []
        seen: set[tuple[int, bytes]] = set()

        def add_image_bytes(b: bytes):
            if not b:
                return
            key = (len(b), b[:128])
            if key in seen:
                return
            seen.add(key)
            images.append(b)

        async def walk(obj: object):
            if obj is None:
                return

            if isinstance(obj, list):
                for item in obj:
                    await walk(item)
                return

            if isinstance(obj, str):
                b = self._try_decode_image_base64(obj)
                if b:
                    add_image_bytes(b)
                    return

                if obj.startswith("http://") or obj.startswith("https://"):
                    if d := await self._download_url(obj):
                        add_image_bytes(d)
                    return

                text_imgs = await self._extract_images_from_text(obj)
                for tb in text_imgs:
                    add_image_bytes(tb)
                return

            if not isinstance(obj, dict):
                return

            # 先走现有 OpenAI / Gemini 解析器
            try:
                oai_imgs = await self._extract_openai_image(obj)
                if oai_imgs:
                    for b in oai_imgs:
                        add_image_bytes(b)
            except Exception:
                pass

            try:
                gem_imgs = self._extract_gemini_image(obj)
                if gem_imgs:
                    for b in gem_imgs:
                        add_image_bytes(b)
            except Exception:
                pass

            for key, value in obj.items():
                lk = str(key).lower()

                if lk in {"inline_data", "inlinedata"} and isinstance(value, dict):
                    b64 = value.get("data")
                    if isinstance(b64, str):
                        b = self._try_decode_image_base64(b64)
                        if b:
                            add_image_bytes(b)
                    continue

                if lk in {"image_url", "imageurl"}:
                    if isinstance(value, dict):
                        url = value.get("url")
                        if isinstance(url, str):
                            await walk(url)
                    else:
                        await walk(value)
                    continue

                if lk in {
                    "b64_json",
                    "base64",
                    "image_base64",
                    "imagebase64",
                    "image",
                    "url",
                    "uri",
                    "file_uri",
                    "fileurl",
                    "file_url",
                }:
                    await walk(value)
                    continue

                if lk in {
                    "data",
                    "result",
                    "results",
                    "output",
                    "outputs",
                    "images",
                    "choices",
                    "message",
                    "content",
                    "parts",
                    "candidates",
                }:
                    await walk(value)
                    continue

                if isinstance(value, (dict, list)):
                    await walk(value)

        if raw_text:
            text_imgs = await self._extract_images_from_text(raw_text)
            for b in text_imgs:
                add_image_bytes(b)

        await walk(data)

        return images if images else None

    async def _extract_images_from_response(
        self,
        response: aiohttp.ClientResponse,
    ) -> tuple[list[bytes] | None, object | None, str | None]:
        """
        从 response 里统一提取图片。
        返回:
          (images, data, error)

        error:
          None                  -> 没错误，但可能无图
          "API 响应解析失败"      -> 非 JSON / 非 SSE 且文本里也没图
        """
        data, raw_text, parse_ok, raw_bytes = await self._read_response_payload(response)

        content_type = (response.headers.get("content-type") or "").lower()
        if "image/" in content_type or self._is_image_bytes(raw_bytes):
            return [raw_bytes], data, None

        images = await self._extract_any_image(data, raw_text)
        if images:
            return images, data, None

        if not parse_ok:
            logger.warning(f"API 响应解析失败，原始响应摘要: {raw_text[:300]}")
            return None, None, "API 响应解析失败"

        return None, data, None

    # =========================
    # Image Format / Ratio
    # =========================

    def _sync_convert_image_format(
        self,
        image_data: bytes,
        mime_type: str,
    ) -> tuple[bytes, str]:
        try:
            img = Image.open(BytesIO(image_data))

            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode in ("P", "LA"):
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[3])
                img = background

            output = BytesIO()
            img.save(output, format="JPEG", quality=95)
            return output.getvalue(), "image/jpeg"

        except Exception as e:
            logger.error(f"图片格式转换失败: {e}")
            return image_data, mime_type

    async def _convert_image_format(
        self,
        image_data: bytes,
        mime_type: str,
    ) -> tuple[bytes, str]:
        if image_data.startswith(b"\xff\xd8"):
            mime = "image/jpeg"
        elif image_data.startswith(b"\x89PNG"):
            mime = "image/png"
        elif image_data.startswith(b"GIF"):
            mime = "image/gif"
        elif image_data.startswith(b"RIFF") and image_data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            mime = "application/octet-stream"

        supported_formats = [
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/heic",
            "image/heif",
        ]

        if mime in supported_formats:
            return image_data, mime

        return await asyncio.to_thread(
            self._sync_convert_image_format,
            image_data,
            mime_type,
        )

    def _ratio_to_wh(self, ratio: str | None) -> tuple[int, int] | None:
        if not ratio:
            return None

        return self.RATIO_WH.get(ratio.strip())

    def _sync_pad_image_to_ratio(
        self,
        image_data: bytes,
        target_ratio: str,
        out_format: str = "PNG",
    ) -> tuple[bytes, str]:
        wh = self._ratio_to_wh(target_ratio)
        if not wh:
            if image_data.startswith(b"\x89PNG"):
                return image_data, "image/png"
            if image_data.startswith(b"\xff\xd8"):
                return image_data, "image/jpeg"
            return image_data, "application/octet-stream"

        rw, rh = wh
        img = Image.open(BytesIO(image_data)).convert("RGBA")
        w, h = img.size

        if w <= 0 or h <= 0:
            return image_data, "image/png"

        src_ratio = w / h
        dst_ratio = rw / rh

        if abs(src_ratio - dst_ratio) < 1e-6:
            output = BytesIO()
            img.save(output, format=out_format)
            return output.getvalue(), "image/png"

        if src_ratio > dst_ratio:
            new_w = w
            new_h = int(round(w / dst_ratio))
        else:
            new_h = h
            new_w = int(round(h * dst_ratio))

        canvas = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))
        x = (new_w - w) // 2
        y = (new_h - h) // 2
        canvas.paste(img, (x, y), img)

        output = BytesIO()
        canvas.save(output, format=out_format)
        return output.getvalue(), "image/png"

    async def _pad_images_to_ratio_if_needed(
        self,
        images_data: list[tuple[bytes, str]],
        aspect_ratio: str | None,
    ) -> list[tuple[bytes, str]]:
        if not aspect_ratio or not images_data:
            return images_data

        padded: list[tuple[bytes, str]] = []
        for img_bytes, _mime in images_data:
            try:
                b, m = await asyncio.to_thread(
                    self._sync_pad_image_to_ratio,
                    img_bytes,
                    aspect_ratio,
                    "PNG",
                )
                padded.append((b, m))
            except Exception:
                padded.append((img_bytes, _mime))

        return padded

    def _sync_fit_output_to_ratio(
        self,
        image_data: bytes,
        target_ratio: str,
        mode: str = "crop",
    ) -> bytes:
        wh = self._ratio_to_wh(target_ratio)
        if not wh:
            return image_data

        rw, rh = wh
        dst_ratio = rw / rh

        img = Image.open(BytesIO(image_data)).convert("RGBA")
        w, h = img.size

        if w <= 0 or h <= 0:
            return image_data

        src_ratio = w / h
        if abs(src_ratio - dst_ratio) < 1e-6:
            return image_data

        if mode == "crop":
            if src_ratio > dst_ratio:
                new_w = int(round(h * dst_ratio))
                x1 = (w - new_w) // 2
                img = img.crop((x1, 0, x1 + new_w, h))
            else:
                new_h = int(round(w / dst_ratio))
                y1 = (h - new_h) // 2
                img = img.crop((0, y1, w, y1 + new_h))
        else:
            if src_ratio > dst_ratio:
                new_w = w
                new_h = int(round(w / dst_ratio))
            else:
                new_h = h
                new_w = int(round(h * dst_ratio))

            canvas = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))
            x = (new_w - w) // 2
            y = (new_h - h) // 2
            canvas.paste(img, (x, y), img)
            img = canvas

        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    async def _post_fix_images_ratio(
        self,
        images: list[bytes],
        aspect_ratio: str | None,
        mode: str = "crop",
    ) -> list[bytes]:
        if not images or not aspect_ratio:
            return images

        fixed: list[bytes] = []
        for b in images:
            try:
                nb = await asyncio.to_thread(
                    self._sync_fit_output_to_ratio,
                    b,
                    aspect_ratio,
                    mode,
                )
                fixed.append(nb)
            except Exception:
                fixed.append(b)

        return fixed

    def _infer_ratio_from_images(
        self,
        images_data: list[tuple[bytes, str]],
    ) -> str | None:
        if not images_data:
            return None

        try:
            img_bytes, _ = images_data[0]
            with Image.open(BytesIO(img_bytes)) as img:
                w, h = img.size
                if not w or not h:
                    return None

            r = w / h
            candidates = {
                k: rw / rh for k, (rw, rh) in self.RATIO_WH.items()
            }

            best_ratio = None
            best_diff = 10**9

            for k, v in candidates.items():
                diff = abs(r - v)
                if diff < best_diff:
                    best_diff = diff
                    best_ratio = k

            return best_ratio

        except Exception:
            return None

    def _build_openai_size(
        self,
        image_size: str | None,
        aspect_ratio: str | None,
    ) -> str:
        base_map = {"1K": 1024, "2K": 1536, "4K": 2048}
        base = base_map.get((image_size or "1K").upper(), 1024)

        rw, rh = self.RATIO_WH.get((aspect_ratio or "1:1").strip(), (1, 1))

        if rw == rh:
            w = h = base
        elif rw > rh:
            w = base
            h = max(256, int(base * rh / rw))
        else:
            h = base
            w = max(256, int(base * rw / rh))

        def round64(x: int) -> int:
            return max(256, int(round(x / 64) * 64))

        w, h = round64(w), round64(h)
        return f"{w}x{h}"

    # =========================
    # Vertex Helpers
    # =========================

    def _next_vertex_cred(self, config: ProviderConfig) -> tuple[str, str] | None:
        keys = config.vertex_keys or []
        if not keys:
            return None

        raw = keys[self._vertex_idx % len(keys)]
        self._vertex_idx += 1

        if not isinstance(raw, str) or "|" not in raw:
            return None

        api_key, project_id = raw.split("|", 1)
        api_key = api_key.strip()
        project_id = project_id.strip()

        if not api_key or not project_id:
            return None

        return api_key, project_id

    # =========================
    # Main Generate
    # =========================

    def _is_non_retryable(self, error: str | None) -> bool:
        """根据错误信息判断是否为不可重试的错误。

        包括 4xx 客户端错误，以及内容被安全策略拦截（重试同样会被拦）。
        """
        if not error:
            return False
        err = str(error)
        if "安全策略拦截" in err:
            return True
        m = re.search(r"\bAPI\s+(\d{3})\b", err)
        return bool(m and m.group(1) in self.NON_RETRYABLE_CODES)

    def _gemini_block_reason(self, data: object | None) -> str | None:
        """从 Gemini/Vertex 响应中提取内容安全拦截原因（无则返回 None）。

        命中时说明请求因安全策略被拒，重试无意义，返回可直接展示的文案。
        """
        if not isinstance(data, dict):
            return None

        pf = data.get("promptFeedback")
        if isinstance(pf, dict) and pf.get("blockReason"):
            return f"提示词被安全策略拦截（{pf.get('blockReason')}），请调整后再试"

        candidates = data.get("candidates")
        if isinstance(candidates, list):
            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                fr = cand.get("finishReason")
                if fr and str(fr) in self.GEMINI_BLOCK_REASONS:
                    return f"图片被安全策略拦截（{fr}），请调整提示词后再试"

        return None

    @staticmethod
    def _augment_prompt_for_ratio(
        prompt: str,
        aspect_ratio: str | None,
        images_data: list,
    ) -> str:
        """带参考图且指定比例时，给提示词追加比例约束（Gemini/Vertex 共用）。"""
        if aspect_ratio and images_data:
            return (
                f"输出比例必须为 {aspect_ratio}，并填满画面，不要黑边，不要留白。\n"
                f"{prompt}"
            )
        return prompt

    async def generate_image(
        self,
        prompt: str,
        images_data: list[tuple[bytes, str]] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        task_id: str | None = None,
    ) -> tuple[list[bytes] | None, str | None]:
        prefix = f"[{task_id}] " if task_id else ""

        if not self.main_config:
            return None, "未配置提供商"

        converted_images = []
        if images_data:
            for img_data, mime_type in images_data:
                c_data, c_mime = await self._convert_image_format(img_data, mime_type)
                converted_images.append((c_data, c_mime))

        retry_queue: list[ProviderConfig] = [self.main_config] * self.max_retries
        last_error_short = "API 请求失败"

        for i, provider in enumerate(retry_queue):
            logger.info(
                f"{prefix}尝试第 {i + 1}/{len(retry_queue)} 次生成 "
                f"(提供商: {provider.name}, 模型: {provider.model}, 类型: {provider.api_type})"
            )

            try:
                if provider.api_type == "gemini":
                    images, error = await self._generate_gemini(
                        provider,
                        prompt,
                        converted_images,
                        aspect_ratio,
                        image_size,
                    )
                elif provider.api_type == "vertex":
                    images, error = await self._generate_vertex(
                        provider,
                        prompt,
                        converted_images,
                        aspect_ratio,
                        image_size,
                    )
                else:
                    images, error = await self._generate_openai(
                        provider,
                        prompt,
                        converted_images,
                        aspect_ratio,
                        image_size,
                    )

                if images:
                    if aspect_ratio and converted_images:
                        images = await self._post_fix_images_ratio(
                            images,
                            aspect_ratio,
                            mode="crop",
                        )
                    return images, None

                last_error_short = self._short_api_error(error)
                logger.warning(f"{prefix}生成失败: {last_error_short}")

                if self._is_non_retryable(error):
                    logger.info(f"{prefix}错误不可重试（{last_error_short}），停止重试")
                    return None, last_error_short

            except Exception as e:
                logger.error(f"{prefix}异常: {e}\n{traceback.format_exc()}")
                last_error_short = self._short_api_error(str(e))

            if i < len(retry_queue) - 1:
                await asyncio.sleep(self.retry_delay)

        return None, last_error_short

    # =========================
    # OpenAI
    # =========================

    async def _generate_openai(
        self,
        config: ProviderConfig,
        prompt: str,
        images_data: list[tuple[bytes, str]],
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> tuple[list[bytes] | None, str | None]:
        model_name = (config.model or "").strip().lower()

        if "gpt-image-2" in model_name:
            return await self._generate_openai_image_api(
                config=config,
                prompt=prompt,
                images_data=images_data,
                image_size=image_size,
                aspect_ratio=aspect_ratio,
            )

        try:
            payload = self._build_openai_payload(
                config,
                prompt,
                images_data,
                aspect_ratio,
                image_size,
            )

            url = f"{config.base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            }

            session = self._get_session()
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    return None, f"API {response.status}: {body[:300]}"

                images, data, parse_error = await self._extract_images_from_response(response)
                if images:
                    return images, None

                if parse_error:
                    return None, parse_error

                if isinstance(data, dict) and "error" in data:
                    err = data.get("error")
                    if isinstance(err, dict):
                        return None, f"API Error: {err.get('message')}"
                    return None, f"API Error: {err}"

                return self._no_image_error(data)

        except asyncio.TimeoutError:
            return None, "请求超时"
        except Exception as e:
            return None, str(e)

    async def _generate_openai_image_api(
        self,
        config: ProviderConfig,
        prompt: str,
        images_data: list[tuple[bytes, str]],
        image_size: str | None,
        aspect_ratio: str | None,
    ) -> tuple[list[bytes] | None, str | None]:
        try:
            session = self._get_session()
            headers_auth = {"Authorization": f"Bearer {config.api_key}"}

            final_ratio = aspect_ratio
            if not final_ratio and images_data:
                final_ratio = self._infer_ratio_from_images(images_data)

            size = self._build_openai_size(image_size, final_ratio) if final_ratio else None

            logger.info(
                f"OpenAI images route: aspect_ratio={aspect_ratio}, "
                f"final_ratio={final_ratio}, size={size}, refs={len(images_data)}"
            )

            if not images_data:
                url = f"{config.base_url}/images/generations"
                payload = {
                    "model": config.model,
                    "prompt": prompt,
                    "response_format": "b64_json",
                }

                if size:
                    payload["size"] = size

                async with session.post(
                    url,
                    json=payload,
                    headers={**headers_auth, "Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as response:
                    if response.status != 200:
                        body = await response.text()
                        return None, f"API {response.status}: {body[:300]}"

                    images, data, parse_error = await self._extract_images_from_response(response)
                    if images:
                        return images, None

                    if parse_error:
                        return None, parse_error

                    return self._no_image_error(data)

            if aspect_ratio:
                images_data = await self._pad_images_to_ratio_if_needed(
                    images_data,
                    aspect_ratio,
                )

            url = f"{config.base_url}/images/edits"
            form = aiohttp.FormData()
            form.add_field("model", config.model)
            form.add_field("prompt", prompt)
            form.add_field("response_format", "b64_json")

            if size:
                form.add_field("size", size)

            for idx, (img_bytes, mime) in enumerate(images_data):
                ext = "png" if "png" in mime else "jpg"
                form.add_field(
                    "image",
                    img_bytes,
                    filename=f"ref_{idx}.{ext}",
                    content_type=mime,
                )

            async with session.post(
                url,
                data=form,
                headers=headers_auth,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    return None, f"API {response.status}: {body[:300]}"

                images, data, parse_error = await self._extract_images_from_response(response)
                if images:
                    return images, None

                if parse_error:
                    return None, parse_error

                return self._no_image_error(data)

        except asyncio.TimeoutError:
            return None, "请求超时"
        except Exception as e:
            return None, str(e)

    def _build_openai_payload(
        self,
        config: ProviderConfig,
        prompt: str,
        images_data: list,
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> dict:
        content = [{"type": "text", "text": f"Generate an image: {prompt}"}]

        if images_data:
            for img_bytes, mime in images_data:
                b64 = base64.b64encode(img_bytes).decode("utf-8")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    }
                )

        payload = {
            "model": config.model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],
            "stream": False,
        }

        img_cfg = {}
        if aspect_ratio:
            img_cfg["aspectRatio"] = aspect_ratio
        if image_size:
            img_cfg["imageSize"] = image_size
        if img_cfg:
            payload["generationConfig"] = {"imageConfig": img_cfg}

        return payload

    async def _extract_openai_image(self, data: dict) -> list[bytes] | None:
        images = []

        if not isinstance(data, dict):
            return None

        try:
            if "data" in data:
                data_list = data.get("data")
                if isinstance(data_list, list):
                    for item in data_list:
                        if isinstance(item, dict):
                            if b64 := item.get("b64_json"):
                                try:
                                    images.append(base64.b64decode(b64))
                                except Exception:
                                    pass
                            elif url := item.get("url"):
                                if d := await self._download_url(url):
                                    images.append(d)

            if "choices" in data:
                choices = data.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue

                        message = choice.get("message")
                        if not isinstance(message, dict):
                            continue

                        content = message.get("content", "")

                        if isinstance(content, str):
                            urls = re.findall(r"!\[.*?\]\((https?://.*?)\)", content)
                            for url in urls:
                                if d := await self._download_url(url):
                                    images.append(d)

                        elif isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "image_url":
                                    img_obj = part.get("image_url")
                                    if isinstance(img_obj, dict):
                                        if url := img_obj.get("url"):
                                            if d := await self._download_url(url):
                                                images.append(d)

        except Exception as e:
            logger.error(f"解析OpenAI响应失败: {e}")

        return images if images else None

    # =========================
    # Gemini / Vertex
    # =========================

    async def _generate_gemini(
        self,
        config: ProviderConfig,
        prompt: str,
        images_data: list[tuple[bytes, str]],
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> tuple[list[bytes] | None, str | None]:
        try:
            url = f"{config.base_url}/v1beta/models/{config.model}:generateContent"
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": config.api_key,
            }

            final_prompt = self._augment_prompt_for_ratio(
                prompt, aspect_ratio, images_data
            )

            payload = self._build_gemini_payload(
                final_prompt,
                images_data,
                aspect_ratio,
                image_size,
            )

            session = self._get_session()
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    return None, f"API {response.status}: {body[:300]}"

                images, data, parse_error = await self._extract_images_from_response(response)
                if images:
                    return images, None

                if parse_error:
                    return None, parse_error

                block_reason = self._gemini_block_reason(data)
                if block_reason:
                    logger.warning(f"内容被拦截: {block_reason}")
                    return None, block_reason

                return self._no_image_error(data)

        except asyncio.TimeoutError:
            return None, "请求超时"
        except Exception as e:
            return None, str(e)

    async def _generate_vertex(
        self,
        config: ProviderConfig,
        prompt: str,
        images_data: list[tuple[bytes, str]],
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> tuple[list[bytes] | None, str | None]:
        try:
            cred = self._next_vertex_cred(config)
            if not cred:
                return None, "Vertex keys 未配置或格式错误（需 API_KEY|PROJECT_ID）"

            api_key, project_id = cred

            base = config.base_url.rstrip("/")
            ver = (config.api_version or "v1beta1").strip()
            loc = (config.location or "us-central1").strip()
            model = (config.model or "").strip()

            final_prompt = self._augment_prompt_for_ratio(
                prompt, aspect_ratio, images_data
            )

            payload = self._build_gemini_payload(
                final_prompt,
                images_data,
                aspect_ratio,
                image_size,
            )

            url = (
                f"{base}/{ver}/projects/{project_id}/locations/{loc}/publishers/google/models/"
                f"{model}:generateContent?key={api_key}"
            )
            headers = {"Content-Type": "application/json"}

            session = self._get_session()
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    return None, f"API {response.status}: {body[:300]}"

                images, data, parse_error = await self._extract_images_from_response(response)
                if images:
                    return images, None

                if parse_error:
                    return None, parse_error

                block_reason = self._gemini_block_reason(data)
                if block_reason:
                    logger.warning(f"内容被拦截: {block_reason}")
                    return None, block_reason

                return self._no_image_error(data)

        except asyncio.TimeoutError:
            return None, "请求超时"
        except Exception as e:
            return None, str(e)

    def _build_gemini_payload(
        self,
        prompt: str,
        images_data: list,
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> dict:
        parts = [{"text": prompt}]

        if images_data:
            for img_bytes, mime in images_data:
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": mime,
                            "data": base64.b64encode(img_bytes).decode("utf-8"),
                        }
                    }
                )

        gen_cfg = {"responseModalities": ["IMAGE"]}

        img_cfg = {}
        if aspect_ratio:
            img_cfg["aspectRatio"] = aspect_ratio
        if image_size:
            img_cfg["imageSize"] = image_size
        if img_cfg:
            gen_cfg["imageConfig"] = img_cfg

        return {
            "contents": [
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
            "generationConfig": gen_cfg,
        }

    def _extract_gemini_image(self, data: dict) -> list[bytes] | None:
        images = []

        if not isinstance(data, dict):
            return None

        candidates = data.get("candidates", [])
        if not isinstance(candidates, list):
            return None

        for cand in candidates:
            if not isinstance(cand, dict):
                continue

            content = cand.get("content", {})
            if not isinstance(content, dict):
                continue

            parts = content.get("parts", [])
            if not isinstance(parts, list):
                continue

            for part in parts:
                if not isinstance(part, dict):
                    continue

                inline = part.get("inline_data") or part.get("inlineData")
                if isinstance(inline, dict):
                    b64 = inline.get("data")
                    if b64:
                        try:
                            images.append(base64.b64decode(b64))
                        except Exception:
                            pass

        return images if images else None

    # =========================
    # Misc
    # =========================

    async def _download_url(self, url: str) -> bytes | None:
        try:
            if not url:
                return None

            if url.startswith("data:"):
                return self._try_decode_image_base64(url)

            session = self._get_session()
            async with session.get(url, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if self._is_image_bytes(data):
                        return data
        except Exception:
            pass

        return None
