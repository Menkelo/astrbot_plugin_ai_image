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
from PIL import Image, ImageOps

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
    gemini_keys: list[str] | None = None


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
    # 值来自 gpt-image-2 官方 SIZE_MAPPING（16 的倍数、单边 ≤ 3840、长短边比 ≤ 3:1、
    # 总像素 655360~8294400）。其中 9:21 为 21:9 的对称补全。
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
        gemini_start_idx: int = 0,
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
        # Gemini 手动配置多 Key 轮换游标（与 Vertex 同机制）
        self._gemini_idx = max(0, int(gemini_start_idx))

    def _get_session(self) -> aiohttp.ClientSession:
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

    # =========================
    # Error / Response Helpers
    # =========================

    # 统一错误分类表：(匹配正则, 结论, 处理建议)
    #
    # 各中转站的报错文案千差万别（英文/中文/自定义 JSON），这里把它们归一到
    # 固定的几类结论上，保证用户看到的永远是同一套中文说明。
    # 按顺序匹配，先命中先返回。排序原则：先放特征词唯一、不会误伤的规则
    # （Key / 渠道 / 余额 / 限流 / 模型），再放措辞宽泛的内容审核规则，
    # 最后才是 HTTP 状态码兜底——因为 400/422 这类状态码本身无法区分
    # 「参数不支持」和「内容被拦截」，必须优先靠文案判断。
    ERROR_RULES: list[tuple[str, str, str]] = [
        (
            r"invalid[_ -]?api[_ -]?key|incorrect api key|API_KEY_INVALID"
            r"|UNAUTHENTICATED|invalid[_ -]?token|api[_ -]?key.*(invalid|not valid)"
            r"|令牌(验证失败|无效|不存在|状态不可用)|无效的(令牌|密钥)",
            "API Key 无效或已失效",
            "请检查配置面板里的 Key 是否填写完整、有无多余空格，或是否已被中转站禁用",
        ),
        (
            r"无可用渠道|没有可用的渠道|no available channel|当前分组.*无可用",
            "中转站没有可用的上游渠道",
            "对方暂时没有能跑这个模型的后端，请更换模型或更换中转站",
        ),
        (
            r"PERMISSION_DENIED|permission denied|not allowed"
            r"|无权限|无权访问|没有权限|权限不足",
            "API Key 无权访问该模型",
            "请确认这个 Key 已开通对应模型的权限，或更换渠道",
        ),
        (
            r"RESOURCE_EXHAUSTED|insufficient_quota|out of quota"
            r"|quota.*(exceeded|exhausted)|billing"
            r"|余额不足|额度不足|配额|欠费|没有足够",
            "账户余额或配额不足",
            "请到中转站充值，或确认免费额度是否已经用完",
        ),
        (
            r"rate.?limit|RATE_LIMITED|too many requests"
            r"|请求过于频繁|请求太频繁|请求频率",
            "请求过于频繁，被限流",
            "请等待 30 秒后重试；若多人同时使用，可在配置面板调低重试次数",
        ),
        (
            r"model\s*['\"]?[^\s'\"]+['\"]?\s+(not found|does not exist)"
            r"|model[_ -]?not[_ -]?found|unknown model"
            r"|模型.*不存在|不支持的模型|无效的模型",
            "模型名不存在，或该渠道不提供此模型",
            "请核对配置面板里的模型名；Gemini 渠道可用「自动获取模型」确认可用列表",
        ),
        (
            # 内容审核的措辞各家差异极大，这里尽量覆盖常见说法：
            # Google "violated ... Prohibited Use policy" / "filtered out" /
            # "Try rephrasing the prompt"，OpenAI "content_policy_violation"，
            # 以及各中转站的中文文案。
            r"content[_ -]?policy|content[_ -]?filter|prohibited[_ -]?(use|content)"
            r"|image[_ -]?safety|safety[_ -]?(filter|policy|setting)"
            r"|blocklist|recitation|generative ai prohibited"
            r"|usage polic|acceptable use"
            r"|filtered out|try rephrasing|rephras\w*\s+the\s+prompt"
            r"|(image|images|content|prompt|request)s?\s+(was|were|is|are)?\s*"
            r"(filtered|blocked|rejected)"
            r"|nsfw|sexually explicit|explicit content"
            r"|违规|敏感内容|色情|涉黄|不良信息|违反.{0,6}政策|内容审核",
            "内容被安全策略拦截",
            "请调整提示词，避免暴力、色情、政治或真实人物等敏感内容；"
            "图生图时参考图同样会被审核",
        ),
        (
            r"payload too large|request entity too large"
            r"|image.*(too large|size limit)|文件过大|图片过大|超过大小",
            "上传的图片过大，被服务端拒绝",
            "请压缩参考图或减少垫图数量",
        ),
        (
            r"INVALID_ARGUMENT|invalid[_ -]?request|invalid[_ -]?parameter"
            r"|unsupported.*(parameter|value)|unknown (name|field)|cannot find field"
            r"|参数.*(有误|错误|无效)|不支持的参数",
            "请求参数被服务端拒绝",
            "多为中转站不支持所选的比例或分辨率，请试着改回「自动」比例和 1K 分辨率",
        ),
        (
            r"请求超时|timed out|timeout|TimeoutError|ETIMEDOUT",
            "请求超时",
            "中转站响应太慢或图片过大，可在配置面板调大超时时间后重试",
        ),
        (
            r"SSLError|SSLV3|CERTIFICATE_VERIFY_FAILED|WRONG_VERSION_NUMBER"
            r"|BAD_RECORD_MAC|\[SSL:|ssl\.SSL|certificate",
            "SSL/TLS 连接失败",
            "多为接口地址写错（如 http 写成 https、端口不对）或证书异常，请检查 base_url",
        ),
        (
            r"Cannot connect|Connection (reset|refused|closed|aborted)"
            r"|Server disconnected|ClientConnectorError|getaddrinfo"
            r"|Name or service not known|Temporary failure in name resolution"
            r"|ENOTFOUND|ECONNREFUSED",
            "无法连接到 API 服务器",
            "请检查 base_url 是否正确、服务器能否访问外网、代理是否正常",
        ),
        (
            r"SERVER_ERROR|internal server error|bad gateway"
            r"|service unavailable|服务端内部错误|网关",
            "中转站服务端故障",
            "这是对方服务器的问题，请稍后重试；持续出现请更换中转站",
        ),
        (
            r"API 未返回图片|响应中未找到图片数据",
            "接口调用成功，但返回内容里没有图片",
            "该模型可能不支持生图，或中转站把图片放在了非常规字段；"
            "请确认模型名，或更换渠道",
        ),
        (
            r"响应解析失败|JSON ?解析失败|JSONDecodeError|Expecting value",
            "无法解析接口返回的内容",
            "中转站返回了非标准格式（可能是 HTML 错误页或空响应），"
            "请检查 base_url 是否指向正确的接口",
        ),
        (
            r"未配置提供商",
            "插件尚未配置可用的提供商",
            "请在配置面板填写 base_url、API Key 和模型名",
        ),
        (
            r"Vertex keys 未配置",
            "Vertex Key 未配置或格式错误",
            "Vertex Key 需填成 API_KEY|PROJECT_ID 的格式（中间是竖线）",
        ),
        (
            r"Gemini Key 未配置",
            "Gemini Key 未配置",
            "请在配置面板填写 Gemini 渠道的 API Key",
        ),
    ]

    # HTTP 状态码兜底：(结论, 处理建议)。仅在上面的语义规则全部未命中时使用。
    CODE_RULES: dict[str, tuple[str, str]] = {
        "400": (
            "请求被拒绝（400）",
            "多为提示词触发了内容审核，其次才是比例/分辨率参数不被支持。"
            "请先换个说法重试；仍失败再把比例改回「自动」、分辨率改回 1K",
        ),
        "401": (
            "API Key 未通过验证（401）",
            "请检查 Key 是否填写正确、是否已过期",
        ),
        "403": (
            "访问被拒绝（403）",
            "Key 没有该模型的权限，或来源 IP 被中转站限制",
        ),
        "404": (
            "接口或模型不存在（404）",
            "请检查 base_url 结尾是否多写/少写了 /v1beta，以及模型名是否正确",
        ),
        "408": ("服务端等待超时（408）", "请稍后重试"),
        "409": ("请求冲突（409）", "请稍后重试"),
        "413": ("请求体过大（413）", "参考图太大，请压缩后重试"),
        "415": ("不支持的图片格式（415）", "请改用 PNG 或 JPEG 格式的参考图"),
        "422": (
            "请求被拒绝（422）",
            "多为提示词触发了内容审核，其次才是参数组合不被该渠道支持。"
            "请先换个说法重试；仍失败再调整比例或分辨率",
        ),
        "429": (
            "请求过于频繁或配额已用尽（429）",
            "请等待 30 秒后重试；也可能是余额或免费额度已用完",
        ),
        "500": ("中转站内部错误（500）", "对方服务器故障，请稍后重试"),
        "502": ("网关错误（502）", "中转站到上游的连接有问题，请稍后重试"),
        "503": ("服务暂不可用（503）", "对方服务器过载或维护中，请稍后重试"),
        "504": ("网关超时（504）", "上游响应太慢，请稍后重试或调大超时时间"),
    }


    # 脱敏规则：URL / Bearer Token / Key 参数 / 长十六进制串
    _SENSITIVE_PATTERNS: list[tuple[str, str]] = [
        (r"https?://[^\s'\"]+", "[URL]"),
        (r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [KEY]"),
        (r"x-goog-api-key\s*[:=]\s*[^\s'\"]+", "x-goog-api-key: [REDACTED]"),
        (
            r"(api[_-]?key|access[_-]?token|authorization)\s*[:=]\s*[^\s'\"]+",
            r"\1=[REDACTED]",
        ),
        (r"sk-[A-Za-z0-9_-]{8,}", "sk-[REDACTED]"),
        (r"\b[0-9a-fA-F]{32,}\b", "[TOKEN]"),
    ]

    def _classify_error(self, error: str | None) -> tuple[str, str] | None:
        """把任意上游错误归类成统一的 (结论, 处理建议)。无法归类时返回 None。

        匹配顺序：内容安全拦截（已是成品文案）> 语义关键词 > HTTP 状态码。
        """
        err_str = str(error or "")
        if not err_str.strip():
            return None

        # 内容安全拦截的文案由 _gemini_block_reason 生成，已带具体拦截原因，
        # 直接复用为结论，只补一条统一的处理建议
        if "安全策略拦截" in err_str:
            return (
                err_str.strip(),
                "请调整提示词，避免暴力、色情、政治或真实人物等敏感内容",
            )

        for pattern, conclusion, advice in self.ERROR_RULES:
            if re.search(pattern, err_str, re.IGNORECASE):
                return conclusion, advice

        # 状态码只从结构化位置提取，避免把响应体里的普通数字（如尺寸 1408）
        # 误判成 HTTP 状态码
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
            return f"接口返回异常状态码（{code}）", "请查看日志中的完整错误信息"

        return None

    def _sanitize_error_summary(
        self, error: str | None, max_len: int = 200
    ) -> str:
        """脱敏原始错误并截断，得到可用于用户可见的详情摘要。

        过滤 URL / Bearer Token / API Key / 长 token，再压缩空白并截断。
        仅在错误无法归类时使用，为空则返回空串。
        """
        s = str(error or "").strip()
        if not s:
            return ""

        for pattern, repl in self._SENSITIVE_PATTERNS:
            s = re.sub(pattern, repl, s, flags=re.IGNORECASE)

        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > max_len:
            s = s[:max_len].rstrip() + "..."
        return s

    def _format_user_error(self, error: str | None) -> str:
        """构造展示给用户的错误信息：统一为「结论 + 💡 处理建议」两行。

        已归类的错误不再附带上游原文（各中转站文案不一，反而干扰判断），
        完整原始错误由调用方写入日志。仅在无法归类时附上脱敏原文，
        方便用户直接反馈。
        """
        classified = self._classify_error(error)
        if classified:
            conclusion, advice = classified
            return f"{conclusion}\n💡 {advice}"

        detail = self._sanitize_error_summary(error)
        if detail:
            return f"未识别的错误\n💡 请把下面的信息反馈给插件作者\n📋 {detail}"
        return "未识别的错误\n💡 请查看 AstrBot 日志获取完整错误信息"

    def _no_image_error(self, data: object | None = None) -> tuple[None, str]:
        """
        API 成功响应但没有图片时统一返回。

        响应体摘要必须一并带回：不少中转站会以 HTTP 200 返回内容审核提示
        （图片位置换成一段说明文字），若在此处丢弃摘要，上层分类器只能看到
        「API 未返回图片」，既无法给出正确结论，也会把必然失败的请求重试三次。
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
        将 EXIF Orientation 应用到像素并移除方向标签，防止聊天端/浏览器
        按 EXIF 自动旋转导致图片打横。
        仅当 JPEG 携带非默认方向（≠1）时才重新编码，否则原样返回。
        """
        try:
            if not image_data.startswith(b"\xff\xd8"):
                return image_data

            img = Image.open(BytesIO(image_data))
            orientation = img.getexif().get(0x0112)
            if not orientation or orientation == 1:
                return image_data

            img = ImageOps.exif_transpose(img)
            img.info.pop("exif", None)
            img.info.pop("parsed_exif", None)
            out = BytesIO()
            img.save(out, format="JPEG", quality=95)
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
        img = ImageOps.exif_transpose(Image.open(BytesIO(image_data))).convert("RGBA")
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

        img = ImageOps.exif_transpose(Image.open(BytesIO(image_data))).convert("RGBA")
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
    ) -> int:
        """计算目标分辨率档位的长边像素。

        优先按 gpt-image-2 精确 SIZE_MAPPING 取对应档位+比例的标准长边
        （如 4K 1:1=2880、4K 16:9=3840），避免把模型原生 4K 图再放大到
        4096 造成无效插值；表外档位/比例回退到通用档位长边。
        """
        tier = (image_size or "1K").strip().upper()
        ratio = (aspect_ratio or "1:1").strip()

        tier_map = self.GPT_IMAGE_SIZES.get(tier)
        if tier_map and ratio in tier_map:
            w, h = (int(x) for x in tier_map[ratio].split("x"))
            return max(w, h)

        return self.RESOLUTION_LONG_EDGE.get(tier, 1024)

    async def _enforce_resolution(
        self,
        images: list[bytes],
        image_size: str | None,
        aspect_ratio: str | None = None,
    ) -> list[bytes]:
        """按目标分辨率档位提升生成图片尺寸，保证 1K/2K/4K 生效。

        目标长边按档位+比例精确计算，API 已生成标准尺寸时不再放大。
        """
        if not images:
            return images

        target = self._resolution_target_long_edge(image_size, aspect_ratio)
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

        这类请求换参数重发、或原样重试都必然再次被拒，调用方据此跳过重试。
        """
        classified = self._classify_error(error)
        return bool(classified and "安全策略拦截" in classified[0])

    def _is_non_retryable(self, error: str | None) -> bool:
        """根据错误信息判断是否为不可重试的错误。

        包括 4xx 客户端错误，以及内容被安全策略拦截（重试同样会被拦）。
        """
        if not error:
            return False
        err = str(error)
        # 内容被拦截时重试同样会被拦。由统一分类器判定，这样各中转站的
        # 英文拦截文案（如 "filtered out ... Prohibited Use policy"）即使
        # 挂在可重试的状态码上，也不会白白重试三次
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
                f"参考图的比例为 {aspect_ratio}，输出图片必须保持该比例构图，"
                f"并填满画面，不要黑边，不要留白。"
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

        if not self.main_config:
            return None, "未配置提供商"

        converted_images = []
        if images_data:
            for img_data, mime_type in images_data:
                c_data, c_mime = await self._convert_image_format(img_data, mime_type)
                converted_images.append((c_data, c_mime))

        # 图生图智能比例识别：未显式指定比例时，根据第一张参考图推断比例。
        # 对所有提供商统一生效（原先仅 OpenAI images 路由会推断）。
        if not aspect_ratio and converted_images:
            inferred = self._infer_ratio_from_images(converted_images)
            if inferred:
                aspect_ratio = inferred
                logger.info(f"{prefix}未指定比例，根据参考图推断: {aspect_ratio}")

        retry_queue: list[ProviderConfig] = [self.main_config] * self.max_retries
        last_error = "API 请求失败"

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
                    # 比例兜底：无论文生图/图生图、模型是否遵守 aspectRatio，
                    # 统一裁剪到目标比例，保证比例始终生效
                    if aspect_ratio:
                        images = await self._post_fix_images_ratio(
                            images,
                            aspect_ratio,
                            mode="crop",
                        )
                    # 分辨率落地：模型未按 imageSize 达到目标时，放大到目标长边
                    images = await self._enforce_resolution(
                        images,
                        image_size,
                        aspect_ratio,
                    )
                    images = await self._normalize_images_orientation(images)
                    images = await self._ensure_png(images)
                    return images, None

                last_error = self._format_user_error(error)
                logger.warning(
                    f"{prefix}生成失败: {last_error}\n原始错误: {error}"
                )

                if self._is_non_retryable(error):
                    logger.info(
                        f"{prefix}错误不可重试（{last_error}），停止重试"
                    )
                    return None, last_error

            except Exception as e:
                logger.error(f"{prefix}异常: {e}\n{traceback.format_exc()}")
                last_error = self._format_user_error(str(e))

                # 异常路径同样要判定可重试性，否则内容拦截等必然失败的情况
                # 会在这里被无条件重试
                if self._is_non_retryable(str(e)):
                    logger.info(
                        f"{prefix}错误不可重试（{last_error}），停止重试"
                    )
                    return None, last_error

            if i < len(retry_queue) - 1:
                await asyncio.sleep(self.retry_delay)

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

            size = self._build_openai_size(image_size, final_ratio)

            if final_ratio:
                prompt = self._augment_prompt_for_ratio(
                    prompt, final_ratio, images_data
                )

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

                async def _post_generations(p: dict):
                    return await session.post(
                        url,
                        json=p,
                        headers={**headers_auth, "Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    )

                response = await _post_generations(payload)
                if response.status == 400 and size:
                    # size 不被当前端点支持（严格 OpenAI 枚举值）时去掉重试，
                    # 由生成后长边放大兜底保证 2K/4K 输出
                    body = await response.text()
                    response.close()

                    # 内容审核类 400 与 size 无关，去掉也会被拒，直接返回
                    err = f"API {response.status}: {body[:300]}"
                    if self._is_content_block(err):
                        return None, err

                    payload.pop("size", None)
                    response = await _post_generations(payload)
                async with response:
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

            async def _post_edits(include_size: bool):
                form = aiohttp.FormData()
                form.add_field("model", config.model)
                form.add_field("prompt", prompt)

                if include_size and size:
                    form.add_field("size", size)

                for idx, (img_bytes, mime) in enumerate(images_data):
                    ext = "png" if "png" in mime else "jpg"
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

            response = await _post_edits(True)
            if response.status == 400 and size:
                # size 不被当前端点支持时去掉重试（auto），由本地长边放大兜底
                body = await response.text()
                response.close()

                # 内容审核类 400 与 size 无关，去掉也会被拒，直接返回
                err = f"API {response.status}: {body[:300]}"
                if self._is_content_block(err):
                    return None, err

                response = await _post_edits(False)
            async with response:
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

            session = self._get_session()

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
                # 比例/分辨率由本地后处理（裁剪/长边放大）兜底保证生效
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
