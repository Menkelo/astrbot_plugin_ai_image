"""
AI Image Generation Module
封装 Gemini/OpenAI/Vertex AI 多 API 图像生成功能（主提供商失败一次后切备用）
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
from PIL import Image, ImageOps

from astrbot.api import logger

from .image_geometry import (
    gpt_image_2_size,
    is_gpt_image_2_5,
    legacy_gpt_image_size,
    oriented_size,
    pad_to_ratio,
    ratio_label,
    supports_custom_image_size,
)

_IMAGE_OPTION_ERRORS = {
    "quality": "当前接口不支持所选图片质量，请改为 auto 或检查中转站支持",
    "background": "当前接口不支持所选图片背景，请调整背景选项或检查中转站支持",
    "output_format": "当前接口不支持 PNG 输出参数，请检查模型或中转站兼容性",
}


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
    gemini_keys: list[str] | None = None
    proxy: str = ""  # 可选 HTTP(S) 代理，如 http://host:port（透传自服务商配置）


class _ProxiedSession(aiohttp.ClientSession):
    """自动为会话内所有请求附加固定代理的 aiohttp 会话（aiohttp 代理是请求级参数）。

    注意：aiohttp 的 get()/post() 直接调用内部 _request()，不经过 request()，
    因此必须覆写 _request() 才能让代理对所有请求生效。
    """

    def __init__(self, *args, proxy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._fixed_proxy = (proxy or "").strip() or None

    async def _request(self, method, url, **kwargs):
        if self._fixed_proxy and "proxy" not in kwargs:
            kwargs["proxy"] = self._fixed_proxy
        return await super()._request(method, url, **kwargs)


def parse_gemini_models_payload(payload: dict) -> list[str]:
    """解析 /v1beta/models 响应（Gemini 格式 {"models":[{"name":...}]}），
    过滤出支持 generateContent 的生图模型，去重返回短模型 id 列表。"""
    ids: list[str] = []
    try:
        models = payload.get("models")
        if isinstance(models, list):
            for item in models:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                methods = item.get("supportedGenerationMethods")
                if isinstance(methods, list) and "generateContent" not in methods:
                    continue
                mid = name.removeprefix("models/").strip()
                if mid and "image" in mid.lower() and mid not in ids:
                    ids.append(mid)
    except Exception:
        pass
    return ids


async def fetch_gemini_models(
    base_url: str, api_key: str, *, timeout_sec: int = 15
) -> list[str]:
    """从 Gemini 兼容接口 GET {base_url}/v1beta/models 拉取可用生图模型列表。

    官方地址用 x-goog-api-key，其余中转站同时携带 Bearer。
    """
    base = (base_url or "").strip().rstrip("/")
    prefix = "" if base.endswith("/v1beta") else "/v1beta"
    endpoint = f"{base}{prefix}/models"
    headers = {"x-goog-api-key": (api_key or "").strip()}
    if (
        "generativelanguage.googleapis.com" not in base
        and "aiplatform.googleapis.com" not in base
    ):
        headers["Authorization"] = f"Bearer {(api_key or '').strip()}"

    timeout = aiohttp.ClientTimeout(total=max(5, timeout_sec))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            endpoint, params={"pageSize": 1000}, headers=headers
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Gemini HTTP {resp.status}: {text[:200]}")
    try:
        payload = json.loads(text)
    except ValueError as e:
        raise RuntimeError(f"Gemini 模型列表响应非 JSON: {e}") from e
    return parse_gemini_models_payload(payload)


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

    # gpt-image-2 等 OpenAI images 路由支持的精确分辨率映射（1K/2K/4K × 比例）。
    # 插件预设尺寸，满足 GPT Image 2 官方约束（16 的倍数、单边 ≤ 3840、
    # 长短边比 ≤ 3:1、总像素 655360~8294400）；不是所有模型共用的官方枚举表。
    GPT_IMAGE_SIZES: dict[str, dict[str, str]] = {
        "1K": {
            "1:1": "1024x1024",
            "16:9": "1280x720",
            "9:16": "720x1280",
            "5:4": "1040x832",
            "4:5": "832x1040",
            "4:3": "1024x768",
            "3:4": "768x1024",
            "3:2": "1008x672",
            "2:3": "672x1008",
            "21:9": "1344x576",
            "9:21": "576x1344",
        },
        "2K": {
            "1:1": "2048x2048",
            "16:9": "2048x1152",
            "9:16": "1152x2048",
            "5:4": "2080x1664",
            "4:5": "1664x2080",
            "4:3": "2048x1536",
            "3:4": "1536x2048",
            "3:2": "2064x1376",
            "2:3": "1376x2064",
            "21:9": "2016x864",
            "9:21": "864x2016",
        },
        "4K": {
            "1:1": "2880x2880",
            "16:9": "3840x2160",
            "9:16": "2160x3840",
            "5:4": "3200x2560",
            "4:5": "2560x3200",
            "4:3": "3264x2448",
            "3:4": "2448x3264",
            "3:2": "3504x2336",
            "2:3": "2336x3504",
            "21:9": "3808x1632",
            "9:21": "1632x3808",
        },
    }

    # 分辨率档位对应的目标长边（像素）。
    # 用于提供商原生不支持 imageSize 参数时，生成后按目标长边提升尺寸，
    # 保证配置面板/指令指定的 1K/2K/4K 始终生效。
    RESOLUTION_LONG_EDGE: dict[str, int] = {
        "1K": 1024,
        "2K": 2048,
        "4K": 4096,
    }

    # 这些 HTTP 状态码属于请求本身的问题（参数/鉴权/内容策略等），
    # 切备用提供商同样不会成功，遇到时直接返回错误。
    NON_RETRYABLE_CODES: frozenset[str] = frozenset(
        {"400", "413", "415", "422", "451"}
    )

    # Gemini/Vertex 返回 200 但因内容安全策略未产出图片时的 finishReason，
    # 属于内容被拦截，切备用同样会被拦，故直接返回错误。
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
        vertex_start_idx: int = 0,
        gemini_start_idx: int = 0,
        backup_config: ProviderConfig | None = None,
        gpt_image_quality: str = "auto",
        gpt_image_background: str = "auto",
    ):
        self.main_config = main_config
        self.backup_config = backup_config
        self.last_used_provider: ProviderConfig | None = None
        quality = str(gpt_image_quality or "auto").strip().lower()
        background = str(gpt_image_background or "auto").strip().lower()
        self.gpt_image_quality = (
            quality if quality in {"auto", "low", "medium", "high", "xhigh", "max"} else "auto"
        )
        self.gpt_image_background = (
            background if background in {"auto", "opaque", "transparent"} else "auto"
        )
        self.timeout = timeout
        self._session = session
        # 仅当会话由本实例创建时才负责关闭；注入的共享会话不在此关闭
        self._owns_session = session is None
        # 由调用方传入起始 Key 索引，实现 Vertex 多 Key 跨请求轮换
        self._vertex_idx = max(0, int(vertex_start_idx))
        # Gemini 手动配置多 Key 轮换游标（与 Vertex 同机制）
        self._gemini_idx = max(0, int(gemini_start_idx))
        # 按代理地址缓存的会话（不同提供商可各自走不同代理/直连）
        self._proxied_sessions: dict[str, aiohttp.ClientSession] = {}

    def _get_session(
        self, proxy: str | None = None
    ) -> aiohttp.ClientSession:
        proxy = (proxy or "").strip() or None
        if proxy:
            session = self._proxied_sessions.get(proxy)
            if session is None or session.closed:
                session = _ProxiedSession(proxy=proxy)
                self._proxied_sessions[proxy] = session
            return session
        if self._session is None or self._session.closed:
            # 预签名图片 URL（如 S3 accelerate）常见 301/307，
            # aiohttp 各请求方法默认 allow_redirects=True，无需额外配置
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close_session(self):
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        for key in list(self._proxied_sessions):
            s = self._proxied_sessions.pop(key)
            if s and not s.closed:
                await s.close()

    # =========================
    # Error / Response Helpers
    # =========================

    CODE_RULES: dict[str, str] = {
        "400": "内容审核不通过（400）",
        "401": "API Key 未通过验证（401）",
        "403": "访问被拒绝（403）",
        "404": "接口或模型不存在（404）",
        "408": "服务端等待超时（408）",
        "409": "请求冲突（409）",
        "413": "请求体过大（413）",
        "415": "不支持的图片格式（415）",
        "422": "内容审核不通过（422）",
        "451": "内容审核不通过（451）",
        "429": "请求过于频繁或配额已用尽（429）",
        "500": "中转站内部错误（500）",
        "502": "网关错误（502）",
        "503": "服务暂不可用（503）",
        "504": "网关超时（504）",
    }


    def _classify_error(self, error: str | None) -> str | None:
        """把上游错误归类成单行结论。无法归类时返回 None。"""
        err_str = str(error or "")
        if not err_str.strip():
            return None
        if err_str in _IMAGE_OPTION_ERRORS.values():
            return err_str

        if "安全策略拦截" in err_str:
            return err_str.strip()

        status_match = (
            re.search(r"\bAPI\s+(\d{3})\b", err_str, re.IGNORECASE)
            or re.search(r"\bHTTP\s+(\d{3})\b", err_str, re.IGNORECASE)
            or re.search(r"status_?code\s*[=:]\s*(\d{3})", err_str, re.IGNORECASE)
            or re.search(r'"code"\s*:\s*(\d{3})', err_str)
            or re.search(r"'code'\s*:\s*(\d{3})", err_str)
        )
        if status_match:
            code = status_match.group(1)
            if code in self.CODE_RULES:
                return self.CODE_RULES[code]

        return None

    def _format_user_error(self, error: str | None) -> str:
        """构造展示给用户的单行错误结论。"""
        classified = self._classify_error(error)
        if classified:
            return classified
        return "生成过程中发生未知错误"

    def _no_image_error(self, data: object | None = None) -> tuple[None, str]:
        """
        API 成功响应但没有图片时统一返回。

        响应体摘要必须一并带回：不少中转站会以 HTTP 200 返回内容审核提示
        （图片位置换成一段说明文字），若在此处丢弃摘要，上层分类器只能看到
        「API 未返回图片」，既无法给出正确结论，也会把必然失败的请求切到备用提供商。
        """
        if data is None:
            return None, "API 未返回图片"

        summary = str(data)[:300]
        logger.warning(f"API 未返回图片，原始响应摘要: {summary}")
        return None, f"API 未返回图片: {summary}"

    def _is_image_bytes(self, b: bytes | None) -> bool:
        if not b:
            return False
        return (
            b.startswith(b"\xff\xd8")
            or b.startswith(b"\x89PNG")
            or b.startswith(b"GIF")
            or (b.startswith(b"RIFF") and len(b) > 12 and b[8:12] == b"WEBP")
            or (len(b) > 12 and b[4:8] == b"ftyp")  # heic/heif/mp4 等 ISOBMFF 容器
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
                    continue

                if isinstance(value, str):
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

    def _sync_normalize_orientation(self, image_data: bytes) -> bytes:
        """
        在读取画幅和调用模型前应用 EXIF 方向；生成结果也使用相同规则。
        只有非默认方向才重编码为无损 PNG，其余图片保留原始字节。
        """
        try:
            with Image.open(BytesIO(image_data)) as source:
                orientation = source.getexif().get(0x0112)
                if orientation not in (2, 3, 4, 5, 6, 7, 8):
                    return image_data
                img = ImageOps.exif_transpose(source)
                img.info.pop("exif", None)
                img.info.pop("parsed_exif", None)
                if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                    img = img.convert("RGB")
                out = BytesIO()
                img.save(out, format="PNG")
                return out.getvalue()
        except Exception:
            return image_data

    def _sync_convert_image_format(
        self,
        image_data: bytes,
        mime_type: str,
    ) -> tuple[bytes, str]:
        try:
            img = Image.open(BytesIO(image_data))
            img = ImageOps.exif_transpose(img)

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
        image_data = await asyncio.to_thread(self._sync_normalize_orientation, image_data)
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

    @staticmethod
    def _reference_size(
        images_data: list[tuple[bytes, str]],
    ) -> tuple[int, int] | None:
        """第一张参考图决定默认画幅，其余图片只作为辅助。"""
        if not images_data:
            return None
        try:
            return oriented_size(images_data[0][0])
        except Exception as exc:
            logger.warning(f"无法读取第一张参考图尺寸: {exc}")
            return None

    def _named_ratio(self, dimensions: tuple[int, int] | None) -> str | None:
        """仅返回完全匹配的支持比例，不就近吸附。"""
        if dimensions:
            width, height = dimensions
            for name, (rw, rh) in self.RATIO_WH.items():
                if width * rh == height * rw:
                    return name
        return None

    async def _post_fix_images_ratio(
        self,
        images: list[bytes],
        target: tuple[int, int] | None,
        min_long_edge: int = 0,
    ) -> list[bytes]:
        if not images or target is None:
            return images
        fixed: list[bytes] = []
        for image_data in images:
            try:
                output = await asyncio.to_thread(
                    pad_to_ratio, image_data, target, min_long_edge=min_long_edge
                )
                if output is not image_data:
                    logger.info(
                        f"已保留完整生成内容并补边至 {ratio_label(target)}"
                    )
                fixed.append(output)
            except Exception as exc:
                # 无法补边时保留完整结果，不隐式改用裁剪或丢掉图片。
                logger.warning(f"比例适配失败，返回完整生成结果: {exc}")
                fixed.append(image_data)
        return fixed

    def _sync_enforce_resolution(
        self,
        image_data: bytes,
        target_long_edge: int,
    ) -> bytes:
        """将图片放大到目标长边（保持比例）。仅当当前长边低于目标时才缩放。"""
        try:
            img = ImageOps.exif_transpose(Image.open(BytesIO(image_data)))
            w, h = img.size
            long_edge = max(w, h)

            if long_edge < 1 or long_edge >= target_long_edge:
                return image_data

            scale = target_long_edge / long_edge
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))

            if img.mode in ("P", "LA"):
                img = img.convert("RGBA")

            img = img.resize((new_w, new_h), Image.LANCZOS)

            # 存 PNG 而非 JPEG：后续 _ensure_png 会统一转 PNG，
            # 这里若先经 JPEG 会把压缩伪影永久烤进最终 PNG
            output = BytesIO()
            img.save(output, format="PNG")
            return output.getvalue()
        except Exception:
            return image_data

    def _resolution_target_long_edge(
        self,
        image_size: str | None,
        aspect_ratio: str | None,
        provider: ProviderConfig | None = None,
    ) -> int:
        """计算目标分辨率档位的长边像素。

        OpenAI 路由按插件的尺寸预设取对应档位和比例的长边
        （如 4K 1:1=2880、4K 16:9=3840），避免把模型原生 4K 图再放大到
        4096 造成无效插值；表外档位/比例回退到通用档位长边。
        """
        tier = (image_size or "1K").strip().upper()
        ratio = (aspect_ratio or "1:1").strip()

        # Gemini / Vertex 的档位按各自长边处理，不套用 GPT 的像素上限。
        if provider and provider.api_type in ("gemini", "vertex"):
            return self.RESOLUTION_LONG_EDGE.get(tier, 1024)

        tier_map = self.GPT_IMAGE_SIZES.get(tier)
        if tier_map and ratio in tier_map:
            w, h = (int(x) for x in tier_map[ratio].split("x"))
            return max(w, h)

        target = self.RESOLUTION_LONG_EDGE.get(tier, 1024)
        if provider and supports_custom_image_size(provider.model):
            return min(target, 3840)
        return target

    async def _enforce_resolution(
        self,
        images: list[bytes],
        image_size: str | None,
        aspect_ratio: str | None = None,
        provider: ProviderConfig | None = None,
    ) -> list[bytes]:
        """按目标分辨率档位提升生成图片尺寸，保证 1K/2K/4K 生效。

        目标长边按档位+比例精确计算，API 已生成标准尺寸时不再放大。
        """
        if not images:
            return images

        target = self._resolution_target_long_edge(image_size, aspect_ratio, provider)
        if not target:
            return images

        enforced: list[bytes] = []
        for b in images:
            nb = await asyncio.to_thread(
                self._sync_enforce_resolution,
                b,
                target,
            )
            enforced.append(nb)

        return enforced

    def _build_openai_size(
        self,
        image_size: str | None,
        aspect_ratio: str | None,
    ) -> str:
        """构造 OpenAI images 路由的 size 参数。

        优先使用 gpt-image-2 精确 SIZE_MAPPING（GPT_IMAGE_SIZES），确保
        1K/2K/4K 与各比例组合命中官方支持的分辨率，避免自定义像素被
        服务端拒绝后回退 1K。表外比例回退到 16 倍数像素计算。
        """
        tier = (image_size or "1K").upper()
        ratio = (aspect_ratio or "1:1").strip()

        tier_map = self.GPT_IMAGE_SIZES.get(tier)
        if tier_map and ratio in tier_map:
            return tier_map[ratio]

        base_map = {"1K": 1024, "2K": 2048, "4K": 2880}
        base = base_map.get(tier, 1024)

        rw, rh = self.RATIO_WH.get(ratio, (1, 1))

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

    def _next_gemini_key(self, config: ProviderConfig) -> str:
        """Gemini 手动配置多 Key 轮换：每次调用取下一个 Key 并自增游标。

        未配置多 Key 列表时直接回落到单 api_key。
        """
        keys = config.gemini_keys or []
        if keys:
            raw = keys[self._gemini_idx % len(keys)]
            self._gemini_idx += 1
            if isinstance(raw, str) and raw.strip():
                return raw.strip()

        return (config.api_key or "").strip()

    # =========================
    # Main Generate
    # =========================

    def _is_content_block(self, error: str | None) -> bool:
        """判断错误是否属于内容审核拦截。

        这类请求切备用提供商同样会被拒，调用方据此直接返回错误。
        """
        classified = self._classify_error(error)
        return bool(classified and "安全策略拦截" in classified)

    def _is_non_retryable(self, error: str | None) -> bool:
        """根据错误信息判断是否为不可切换备用提供商的错误。

        包括 4xx 客户端错误，以及内容被安全策略拦截（切备用同样会被拦）。
        """
        if not error:
            return False
        err = str(error)
        if err in _IMAGE_OPTION_ERRORS.values():
            return True
        # 内容被拦截时切备用同样会被拦。由统一分类器判定，这样各中转站的
        # 英文拦截文案（如 "filtered out ... Prohibited Use policy"）即使
        # 挂在其他状态码上，也不会切到备用提供商
        if self._is_content_block(err):
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
            return f"提示词被安全策略拦截（{pf.get('blockReason')}）"

        candidates = data.get("candidates")
        if isinstance(candidates, list):
            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                fr = cand.get("finishReason")
                if fr and str(fr) in self.GEMINI_BLOCK_REASONS:
                    return f"图片被安全策略拦截（{fr}）"

        return None

    @staticmethod
    def _augment_prompt_for_ratio(
        prompt: str,
        aspect_ratio: str | None,
        images_data: list,
    ) -> str:
        """指定比例时给提示词追加比例约束（所有提供商共用，文生图/图生图均生效）。"""
        if not aspect_ratio:
            return prompt

        if images_data:
            constraint = (
                f"目标输出画幅为 {aspect_ratio}。请保留第一张参考图的完整主体和构图，"
                "其他参考图只作为辅助；如需适配画幅，请向外扩展背景，"
                "不要裁掉主体、拉伸内容或将图片强制改成正方形。"
            )
        else:
            constraint = (
                f"输出图片比例必须为 {aspect_ratio}，并填满画面，不要黑边，不要留白。"
            )

        return f"{constraint}\n{prompt}"

    async def _normalize_images_orientation(self, images: list[bytes]) -> list[bytes]:
        """对生成结果统一应用 EXIF Orientation 到像素，避免发送后打横。"""
        normalized: list[bytes] = []
        for b in images:
            nb = await asyncio.to_thread(self._sync_normalize_orientation, b)
            normalized.append(nb)
        return normalized

    @staticmethod
    def _sync_to_png(image_data: bytes) -> bytes:
        """将图片统一转为无损 PNG。已是 PNG 时原样返回。"""
        try:
            img = Image.open(BytesIO(image_data))
            if (img.format or "").upper() == "PNG":
                return image_data

            img = ImageOps.exif_transpose(img)
            if img.mode in ("P", "LA"):
                img = img.convert("RGBA")
            elif img.mode != "RGBA":
                img = img.convert("RGB")

            output = BytesIO()
            img.save(output, format="PNG")
            return output.getvalue()
        except Exception:
            return image_data

    async def _ensure_png(self, images: list[bytes]) -> list[bytes]:
        """统一所有渠道输出为 PNG（无损大文件），避免 JPEG 有损压缩导致文件偏小。"""
        if not images:
            return images

        converted: list[bytes] = []
        for b in images:
            nb = await asyncio.to_thread(self._sync_to_png, b)
            converted.append(nb)
        return converted

    async def generate_image(
        self,
        prompt: str,
        images_data: list[tuple[bytes, str]] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        task_id: str | None = None,
    ) -> tuple[list[bytes] | None, str | None]:
        prefix = f"[{task_id}] " if task_id else ""
        self.last_used_provider = None

        if not self.main_config:
            return None, "未配置提供商"

        converted_images = []
        if images_data:
            for img_data, mime_type in images_data:
                c_data, c_mime = await self._convert_image_format(img_data, mime_type)
                converted_images.append((c_data, c_mime))

        # 本地目标与 API 可用的画幅分开保存，备用接口也沿用同一个目标。
        reference_wh = await asyncio.to_thread(self._reference_size, converted_images)
        target_wh = self._ratio_to_wh(aspect_ratio) or reference_wh
        native_ratio = aspect_ratio or self._named_ratio(reference_wh)
        if target_wh:
            logger.info(
                f"{prefix}目标比例={ratio_label(target_wh)}，"
                f"来源={'指令' if aspect_ratio else '第一张参考图'}；比例不符时保留内容并补边"
            )
            # 任意原图比例仅通过提示词表达，不强塞到仅支持离散枚举的参数里。
            if not native_ratio:
                prompt = self._augment_prompt_for_ratio(
                    prompt, ratio_label(target_wh), converted_images
                )

        providers: list[ProviderConfig] = [self.main_config]
        if self.backup_config:
            providers.append(self.backup_config)

        last_error = "API 请求失败"

        for i, provider in enumerate(providers):
            role = "主提供商" if i == 0 else "备用提供商"
            logger.info(
                f"{prefix}请求{role} "
                f"(提供商: {provider.name}, 模型: {provider.model}, 类型: {provider.api_type})"
            )

            try:
                if provider.api_type == "gemini":
                    images, error = await self._generate_gemini(
                        provider,
                        prompt,
                        converted_images,
                        native_ratio,
                        image_size,
                    )
                elif provider.api_type == "vertex":
                    images, error = await self._generate_vertex(
                        provider,
                        prompt,
                        converted_images,
                        native_ratio,
                        image_size,
                    )
                else:
                    images, error = await self._generate_openai(
                        provider,
                        prompt,
                        converted_images,
                        native_ratio,
                        image_size,
                    )

                if images:
                    images = await self._normalize_images_orientation(images)
                    resolution_ratio = self._named_ratio(target_wh)
                    if resolution_ratio is None and target_wh:
                        resolution_ratio = ratio_label(target_wh)
                    if target_wh:
                        # 分辨率与补边一次完成，避免放大小画布的比例舍入误差。
                        images = await self._post_fix_images_ratio(
                            images, target_wh,
                            self._resolution_target_long_edge(
                                image_size, resolution_ratio, provider
                            ),
                        )
                    else:
                        images = await self._enforce_resolution(
                            images, image_size, None, provider
                        )
                    images = await self._ensure_png(images)
                    self.last_used_provider = provider
                    return images, None

                last_error = self._format_user_error(error)
                logger.warning(
                    f"{prefix}{role}生成失败: {last_error}\n原始错误: {error}"
                )

                if self._is_non_retryable(error):
                    logger.info(
                        f"{prefix}错误不可切换备用（{last_error}），直接返回"
                    )
                    return None, last_error

            except Exception as e:
                logger.error(f"{prefix}异常: {e}\n{traceback.format_exc()}")
                last_error = self._format_user_error(str(e))

                if self._is_non_retryable(str(e)):
                    logger.info(
                        f"{prefix}错误不可切换备用（{last_error}），直接返回"
                    )
                    return None, last_error

            if i == 0 and self.backup_config:
                logger.info(f"{prefix}主提供商失败，立即请求备用提供商")

        return None, last_error

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

        if "gpt-image" in model_name:
            return await self._generate_openai_image_api(
                config=config,
                prompt=prompt,
                images_data=images_data,
                image_size=image_size,
                aspect_ratio=aspect_ratio,
            )

        try:
            final_prompt = self._augment_prompt_for_ratio(
                prompt, aspect_ratio, images_data
            )

            payload = await asyncio.to_thread(
                self._build_openai_payload,
                config,
                final_prompt,
                images_data,
                aspect_ratio,
                image_size,
            )

            url = f"{config.base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            }

            session = self._get_session(config.proxy)
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

    def _openai_image_request_size(
        self,
        config: ProviderConfig,
        image_size: str | None,
        aspect_ratio: str | None,
        images_data: list[tuple[bytes, str]],
    ) -> str:
        target = self._ratio_to_wh(aspect_ratio) or self._reference_size(images_data)
        if supports_custom_image_size(config.model):
            named_ratio = self._named_ratio(target)
            if named_ratio or target is None:
                return self._build_openai_size(image_size, named_ratio)
            tier = (image_size or "1K").strip().upper()
            return gpt_image_2_size(target, self.RESOLUTION_LONG_EDGE.get(tier, 1024))
        model = config.model.strip().lower().rsplit("/", 1)[-1]
        if model.startswith("gpt-image-1"):
            return legacy_gpt_image_size(target)
        # 自定义模型别名能力未知时使用 auto，本地目标画幅仍保持不变。
        return "auto"

    def _gpt_image_25_options(self, config: ProviderConfig) -> dict[str, str]:
        """2.5 专属可选参数；auto 使用上游默认值，不改变其他模型的请求。"""
        if not is_gpt_image_2_5(config.model):
            return {}
        options = {"output_format": "png"}
        if self.gpt_image_quality != "auto":
            options["quality"] = self.gpt_image_quality
        if self.gpt_image_background != "auto":
            options["background"] = self.gpt_image_background
        return options

    @staticmethod
    def _rejected_image_parameter(body: str) -> str | None:
        """读取结构化参数错误；未知错误或内容审核错误不会被当作参数错误。"""
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            return None
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return None
        code = str(error.get("code") or "").lower()
        if code not in {
            "unsupported_parameter", "unknown_parameter", "invalid_parameter",
            "invalid_value", "unsupported_value", "invalid_size",
        }:
            return None
        param = error.get("param")
        return param if isinstance(param, str) else None

    @classmethod
    def _unsupported_image_parameters(cls, body: str) -> set[str]:
        """仅尺寸和旧模型的保真参数允许兼容降级一次。"""
        param = cls._rejected_image_parameter(body)
        return {param} if param in {"size", "input_fidelity"} else set()

    @classmethod
    def _image_option_error(cls, status: int, body: str) -> str | None:
        if status not in (400, 422):
            return None
        return _IMAGE_OPTION_ERRORS.get(cls._rejected_image_parameter(body))

    async def _generate_openai_image_api(
        self,
        config: ProviderConfig,
        prompt: str,
        images_data: list[tuple[bytes, str]],
        image_size: str | None,
        aspect_ratio: str | None,
    ) -> tuple[list[bytes] | None, str | None]:
        try:
            session = self._get_session(config.proxy)
            headers_auth = {"Authorization": f"Bearer {config.api_key}"}

            size = self._openai_image_request_size(
                config, image_size, aspect_ratio, images_data
            )
            image_options = self._gpt_image_25_options(config)
            prompt = self._augment_prompt_for_ratio(prompt, aspect_ratio, images_data)

            logger.info(
                f"OpenAI images route: aspect_ratio={aspect_ratio}, "
                f"size={size}, refs={len(images_data)}, "
                f"quality={image_options.get('quality', 'auto')}, "
                f"background={image_options.get('background', 'auto')}"
            )

            if not images_data:
                url = f"{config.base_url}/images/generations"
                payload = {
                    "model": config.model,
                    "prompt": prompt,
                    **image_options,
                }
                # 2.5 原生接口返回 base64，图片编码由 output_format 指定。
                if not is_gpt_image_2_5(config.model):
                    payload["response_format"] = "b64_json"

                if size:
                    payload["size"] = size

                async def _post_generations(p: dict):
                    return await session.post(
                        url,
                        json=p,
                        headers={**headers_auth, "Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    )

                response = await _post_generations(payload)
                if response.status == 400 and size:
                    body = await response.text()
                    response.close()
                    option_error = self._image_option_error(response.status, body)
                    if option_error:
                        return None, option_error
                    err = f"API {response.status}: {body[:300]}"
                    if "size" not in self._unsupported_image_parameters(body):
                        return None, err
                    # 只有明确拒绝 size 时才降级一次；本地目标比例不会被清除。
                    payload.pop("size", None)
                    response = await _post_generations(payload)
                async with response:
                    if response.status != 200:
                        body = await response.text()
                        return None, self._image_option_error(response.status, body) or f"API {response.status}: {body[:300]}"

                    images, data, parse_error = await self._extract_images_from_response(response)
                    if images:
                        return images, None

                    if parse_error:
                        return None, parse_error

                    return self._no_image_error(data)

            # 参考图保持各自原有画幅；补边仅作用于模型返回的结果。
            url = f"{config.base_url}/images/edits"

            async def _post_edits(size_value: str | None, fidelity: bool):
                form = aiohttp.FormData()
                form.add_field("model", config.model)
                form.add_field("prompt", prompt)
                for name, value in image_options.items():
                    form.add_field(name, value)

                if size_value:
                    form.add_field("size", size_value)
                if fidelity:
                    form.add_field("input_fidelity", "high")

                for idx, (img_bytes, mime) in enumerate(images_data):
                    ext = {
                        "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                        "image/heic": "heic", "image/heif": "heif",
                    }.get(mime, "bin")
                    form.add_field(
                        "image",
                        img_bytes,
                        filename=f"ref_{idx}.{ext}",
                        content_type=mime,
                    )

                return await session.post(
                    url,
                    data=form,
                    headers=headers_auth,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                )

            model = config.model.strip().lower().rsplit("/", 1)[-1]
            # GPT Image 2 自动使用高保真输入，官方接口不接受 input_fidelity。
            fidelity = model.startswith("gpt-image-1")
            response = await _post_edits(size, fidelity)
            if response.status == 400:
                body = await response.text()
                response.close()
                option_error = self._image_option_error(response.status, body)
                if option_error:
                    return None, option_error
                err = f"API {response.status}: {body[:300]}"
                unsupported = self._unsupported_image_parameters(body)
                if not unsupported:
                    return None, err
                logger.info(
                    f"OpenAI images edits 参数降级一次: {', '.join(sorted(unsupported))}"
                )
                response = await _post_edits(
                    None if "size" in unsupported else size,
                    False if "input_fidelity" in unsupported else fidelity,
                )
            async with response:
                if response.status != 200:
                    body = await response.text()
                    return None, self._image_option_error(response.status, body) or f"API {response.status}: {body[:300]}"

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
            base = config.base_url.rstrip("/")
            if base.endswith("/v1beta"):
                url = f"{base}/models/{config.model}:generateContent"
            else:
                url = f"{base}/v1beta/models/{config.model}:generateContent"

            api_key = self._next_gemini_key(config)
            if not api_key:
                return None, "Gemini Key 未配置"

            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            }
            # 非 Google 官方域名的中转站，同时携带 Bearer 兜底，兼容不识别 x-goog-api-key 的网关
            if (
                "generativelanguage.googleapis.com" not in base
                and "aiplatform.googleapis.com" not in base
            ):
                headers["Authorization"] = f"Bearer {api_key}"

            final_prompt = self._augment_prompt_for_ratio(
                prompt, aspect_ratio, images_data
            )

            logger.info(
                f"Gemini route: aspect_ratio={aspect_ratio}, "
                f"image_size={image_size}, refs={len(images_data)}"
            )

            payload = await asyncio.to_thread(
                self._build_gemini_payload,
                final_prompt,
                images_data,
                aspect_ratio,
                image_size,
            )

            session = self._get_session(config.proxy)

            async def _post_gemini(p: dict):
                return await session.post(
                    url,
                    json=p,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                )

            response = await _post_gemini(payload)
            if response.status == 400 and "generationConfig" in payload:
                # generationConfig 为可选字段，部分中转站不识别
                # imageConfig/responseModalities 等会返回 400，去掉重试一次，
                # 比例/分辨率由本地后处理（补边/等比放大）兜底
                body = await response.text()
                response.close()

                # 但内容审核类 400 与参数无关，去掉字段重发一样会被拒，
                # 直接返回，避免每次尝试都白白多发一次请求
                err = f"API {response.status}: {body[:300]}"
                if self._is_content_block(err):
                    return None, err

                payload.pop("generationConfig", None)
                response = await _post_gemini(payload)

            async with response:
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

            payload = await asyncio.to_thread(
                self._build_gemini_payload,
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

            session = self._get_session(config.proxy)
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
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=60),
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if self._is_image_bytes(data):
                        return data
                logger.warning(
                    f"下载图片失败 status={resp.status} url={self._mask_url(url)}"
                )
        except Exception as e:
            logger.warning(f"下载图片异常: {e} url={self._mask_url(url)}")
        return None

    @staticmethod
    def _mask_url(url: str, keep: int = 80) -> str:
        if not url:
            return ""
        return url if len(url) <= keep else f"{url[:keep]}..."
