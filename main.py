from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Coroutine
from typing import Any, Tuple, Optional, List

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.io import download_image_by_url, save_temp_img

from .image_generator import AIImageGenerator, ProviderConfig


@dataclass
class SlotConfig:
    slot_name: str
    command: str
    provider_id: str
    provider: ProviderConfig | None
    default_resolution: str


class Gemini_Images(Star):
    """AI 图像生成插件（3提供商槽位 + Vertex 手动双指令双模型）"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or AstrBotConfig()

        self._load_config()
        self.background_tasks: set[asyncio.Task] = set()

        self._quota_lock = asyncio.Lock()
        self._quota_file = Path(__file__).resolve().parent / "daily_quota_usage.json"
        self._quota_data = self._load_quota_data()

        for s in self.slots:
            if s.provider:
                logger.info(
                    f"[{s.slot_name}] 命令=/{s.command}, 提供商={s.provider.name}, "
                    f"类型={s.provider.api_type}, 模型={s.provider.model}, 默认分辨率={s.default_resolution}"
                )
            else:
                logger.warning(
                    f"[{s.slot_name}] 命令=/{s.command}, 提供商ID={s.provider_id or '未设置'}, 未解析到可用提供商"
                )

    async def terminate(self):
        try:
            for task in list(self.background_tasks):
                if not task.done():
                    task.cancel()
            logger.info("插件已卸载")
        except Exception as e:
            logger.error(f"卸载清理出错: {e}")

    # =========================
    # Config
    # =========================

    def _extract_provider_id(self, raw) -> str:
        if raw is None:
            return ""

        if isinstance(raw, str):
            return raw.strip()

        if isinstance(raw, dict):
            for k in ("id", "provider_id", "value", "key"):
                v = raw.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()

        if isinstance(raw, list) and raw:
            first = raw[0]
            if isinstance(first, str):
                return first.strip()
            if isinstance(first, dict):
                for k in ("id", "provider_id", "value", "key"):
                    v = first.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()

        return ""

    def _load_config(self):
        api_config = self.config.get("api_config", {}) or {}
        gen_config = self.config.get("generate_config", {}) or {}
        perm_conf = self.config.get("permission_config", {}) or {}
        quota_conf = self.config.get("quota_config", {}) or {}
        vertex_conf = self.config.get("vertex_manual_config", {}) or {}

        self.timeout = int(gen_config.get("timeout", 180))
        self.max_image_size_mb = int(gen_config.get("max_image_size_mb", 10))

        self.perm_mode = perm_conf.get("mode", "disable")
        self.perm_users = set(perm_conf.get("users", []))
        self.perm_groups = set(perm_conf.get("groups", []))
        self.perm_no_permission_reply = perm_conf.get(
            "no_permission_reply", "❌ 您没有权限使用此功能"
        )
        self.perm_silent = perm_conf.get("silent_on_no_permission", False)

        self.enable_daily_quota = bool(quota_conf.get("enable_daily_quota", True))
        self.daily_free_count = int(quota_conf.get("daily_free_count", 5))
        self.quota_exceeded_reply = quota_conf.get(
            "quota_exceeded_reply", "❌ 今日免费生图次数已用完，请明天再试。"
        )

        # Vertex manual common config
        self.vertex_manual_enabled = bool(vertex_conf.get("enabled", False))
        self.vertex_manual_base_url = (
            vertex_conf.get("base_url", "https://aiplatform.googleapis.com")
            or "https://aiplatform.googleapis.com"
        ).strip()
        self.vertex_manual_api_version = (
            vertex_conf.get("api_version", "v1beta1") or "v1beta1"
        ).strip()
        self.vertex_manual_location = (
            vertex_conf.get("location", "global") or "global"
        ).strip()
        self.vertex_manual_keys = vertex_conf.get("keys", []) or []

        # Vertex model slots, compatible with old flat fields
        v1 = vertex_conf.get("vertex_1", {}) or {}
        v2 = vertex_conf.get("vertex_2", {}) or {}

        self.vertex_1_command = (
            v1.get("command")
            or vertex_conf.get("command")
            or "vertex图"
        ).strip().lstrip("/")
        self.vertex_1_model = (
            v1.get("model")
            or vertex_conf.get("model")
            or "gemini-3-pro-image-preview"
        ).strip()
        self.vertex_1_default_resolution = (
            v1.get("default_resolution")
            or vertex_conf.get("default_resolution")
            or "1K"
        ).strip()

        self.vertex_2_command = (
            v2.get("command")
            or "vertex图2"
        ).strip().lstrip("/")
        self.vertex_2_model = (
            v2.get("model")
            or "gemini-2.5-flash-image-preview"
        ).strip()
        self.vertex_2_default_resolution = (
            v2.get("default_resolution")
            or "1K"
        ).strip()

        legacy_main = (
            self._extract_provider_id(api_config.get("provider_main"))
            or self._extract_provider_id(self.config.get("provider_id"))
        )

        p1 = api_config.get("provider_1", {}) or {}
        p2 = api_config.get("provider_2", {}) or {}
        p3 = api_config.get("provider_3", {}) or {}

        p1_id = self._extract_provider_id(p1.get("id")) or legacy_main
        p2_id = self._extract_provider_id(p2.get("id"))
        p3_id = self._extract_provider_id(p3.get("id"))

        self.slots: list[SlotConfig] = [
            SlotConfig(
                slot_name="provider_1",
                command=(p1.get("command", "生图") or "生图").strip().lstrip("/"),
                provider_id=p1_id,
                provider=self._parse_provider(p1_id),
                default_resolution=(p1.get("default_resolution", "1K") or "1K").strip(),
            ),
            SlotConfig(
                slot_name="provider_2",
                command=(p2.get("command", "动漫图") or "动漫图").strip().lstrip("/"),
                provider_id=p2_id,
                provider=self._parse_provider(p2_id),
                default_resolution=(p2.get("default_resolution", "1K") or "1K").strip(),
            ),
            SlotConfig(
                slot_name="provider_3",
                command=(p3.get("command", "海报图") or "海报图").strip().lstrip("/"),
                provider_id=p3_id,
                provider=self._parse_provider(p3_id),
                default_resolution=(p3.get("default_resolution", "1K") or "1K").strip(),
            ),
        ]

        if self.vertex_manual_enabled:
            vertex_keys = [
                str(x).strip()
                for x in self.vertex_manual_keys
                if isinstance(x, str) and str(x).strip()
            ]

            vertex_provider_1 = ProviderConfig(
                name="manual_vertex_1",
                api_type="vertex",
                base_url=self.vertex_manual_base_url.rstrip("/"),
                api_key="",
                model=self.vertex_1_model,
                api_version=self.vertex_manual_api_version,
                location=self.vertex_manual_location,
                vertex_keys=vertex_keys,
            )

            vertex_provider_2 = ProviderConfig(
                name="manual_vertex_2",
                api_type="vertex",
                base_url=self.vertex_manual_base_url.rstrip("/"),
                api_key="",
                model=self.vertex_2_model,
                api_version=self.vertex_manual_api_version,
                location=self.vertex_manual_location,
                vertex_keys=vertex_keys,
            )

            self.slots.append(
                SlotConfig(
                    slot_name="vertex_manual_1",
                    command=self.vertex_1_command,
                    provider_id="__manual_vertex_1__",
                    provider=vertex_provider_1,
                    default_resolution=self.vertex_1_default_resolution,
                )
            )

            self.slots.append(
                SlotConfig(
                    slot_name="vertex_manual_2",
                    command=self.vertex_2_command,
                    provider_id="__manual_vertex_2__",
                    provider=vertex_provider_2,
                    default_resolution=self.vertex_2_default_resolution,
                )
            )

        self.command_map: dict[str, SlotConfig] = {}
        for slot in self.slots:
            if not slot.command:
                continue
            if slot.command in self.command_map:
                logger.warning(f"命令重复: /{slot.command}，后者将覆盖前者")
            self.command_map[slot.command] = slot

    def _parse_provider(self, provider_id: str) -> ProviderConfig | None:
        if not provider_id:
            return None

        provider = self.context.get_provider_by_id(provider_id)
        if not provider:
            logger.warning(f"找不到提供商 ID: {provider_id}")
            return None

        p_conf = getattr(provider, "provider_config", {}) or {}

        base_url = (
            getattr(provider, "api_base", "")
            or p_conf.get("api_base")
            or p_conf.get("api_base_url")
            or "https://generativelanguage.googleapis.com"
        )
        base_url = base_url.rstrip("/")

        api_key = ""
        for k in ["key", "keys", "api_key", "access_token"]:
            val = p_conf.get(k)
            if isinstance(val, str) and val.strip():
                api_key = val.strip()
                break
            if isinstance(val, list) and val and isinstance(val[0], str) and val[0].strip():
                api_key = val[0].strip()
                break

        model = getattr(provider, "model", "") or p_conf.get("model") or "gemini-1.5-flash"

        api_type = "gemini"
        if "aiplatform.googleapis.com" in base_url:
            api_type = "vertex"
        elif "generativelanguage.googleapis.com" not in base_url:
            api_type = "openai"

        if not api_key and api_type != "vertex":
            logger.warning(f"提供商 {provider_id} 缺少 API Key")
            return None

        return ProviderConfig(
            name=provider_id,
            api_type=api_type,
            base_url=base_url,
            api_key=api_key,
            model=model,
        )

    def _resolve_provider_with_fallback(self, slot: SlotConfig) -> ProviderConfig | None:
        if slot.provider:
            return slot.provider

        if slot.provider_id:
            p = self._parse_provider(slot.provider_id)
            if p:
                slot.provider = p
                return p

        legacy_id = self._extract_provider_id(self.config.get("provider_id"))
        if legacy_id:
            p = self._parse_provider(legacy_id)
            if p:
                return p

        try:
            all_providers = self.context.get_all_providers()
            if all_providers and len(all_providers) > 0:
                first = all_providers[0]
                meta = first.meta() if hasattr(first, "meta") else None
                pid = getattr(meta, "id", None)
                if pid:
                    p = self._parse_provider(pid)
                    if p:
                        return p
        except Exception:
            pass

        return None

    # =========================
    # Permission / Quota
    # =========================

    def _check_permission(self, user_id: str, group_id: str = "") -> bool:
        mode = (self.perm_mode or "disable").strip()

        if mode == "disable":
            return True

        user_id = str(user_id).strip()
        group_id = str(group_id).strip()

        limit_users = {str(u).strip() for u in self.perm_users}
        limit_groups = {str(g).strip() for g in self.perm_groups}

        if mode == "blacklist":
            if user_id in limit_users:
                return False
            if group_id and group_id in limit_groups:
                return False
            return True

        if mode == "whitelist":
            if user_id in limit_users:
                return True
            if group_id and group_id in limit_groups:
                return True
            return False

        return True

    def _is_quota_exempt(self, user_id: str, group_id: str = "") -> bool:
        uid = str(user_id).strip()
        gid = str(group_id).strip()

        mode = (self.perm_mode or "disable").strip()
        if mode == "whitelist":
            limit_users = {str(u).strip() for u in self.perm_users}
            limit_groups = {str(g).strip() for g in self.perm_groups}

            if uid in limit_users:
                return True
            if gid and gid in limit_groups:
                return True

        return False

    def _today_str(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _load_quota_data(self) -> dict:
        try:
            if self._quota_file.exists():
                with open(self._quota_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        if "date" not in data:
                            data["date"] = self._today_str()
                        if "users" not in data or not isinstance(data["users"], dict):
                            data["users"] = {}
                        return data
        except Exception as e:
            logger.warning(f"读取每日次数文件失败: {e}")

        return {
            "date": self._today_str(),
            "users": {},
        }

    def _save_quota_data(self):
        try:
            with open(self._quota_file, "w", encoding="utf-8") as f:
                json.dump(self._quota_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存每日次数文件失败: {e}")

    async def _check_and_consume_quota(
        self, user_id: str, group_id: str = ""
    ) -> tuple[bool, int]:
        if self._is_quota_exempt(user_id, group_id):
            return True, -1

        if not self.enable_daily_quota:
            return True, -1

        if self.daily_free_count <= 0:
            return False, 0

        uid = str(user_id).strip()

        async with self._quota_lock:
            today = self._today_str()

            if self._quota_data.get("date") != today:
                self._quota_data = {
                    "date": today,
                    "users": {},
                }

            users = self._quota_data.setdefault("users", {})
            used = int(users.get(uid, 0))

            if used >= self.daily_free_count:
                return False, 0

            used += 1
            users[uid] = used
            self._save_quota_data()

            remain = self.daily_free_count - used
            return True, remain

    # =========================
    # Text Parsing
    # =========================

    def _extract_plain_text_without_mentions(self, event: AstrMessageEvent) -> str:
        msg_obj = getattr(event, "message_obj", None)

        if not msg_obj or not getattr(msg_obj, "message", None):
            return (event.message_str or "").strip()

        text_parts: list[str] = []

        for comp in msg_obj.message:
            if isinstance(comp, (Comp.At, Comp.Image, Comp.Reply)):
                continue

            if isinstance(comp, Comp.Plain):
                t = getattr(comp, "text", "")
                if t:
                    text_parts.append(t)

        merged = "".join(text_parts).strip()
        return merged if merged else (event.message_str or "").strip()

    def _strip_command_prefix(self, text: str, cmd: str) -> str:
        t = (text or "").strip()
        if not t:
            return ""

        if t.startswith(f"/{cmd}"):
            return t[len(cmd) + 1:].strip()

        if t.startswith(cmd):
            return t[len(cmd):].strip()

        parts = t.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    def _extract_ratio(self, text: str) -> Tuple[str, Optional[str]]:
        patterns = [
            (r"21[:：]9", "21:9"),
            (r"9[:：]21", "9:21"),
            (r"16[:：]9", "16:9"),
            (r"9[:：]16", "9:16"),
            (r"5[:：]4", "5:4"),
            (r"4[:：]5", "4:5"),
            (r"4[:：]3", "4:3"),
            (r"3[:：]4", "3:4"),
            (r"3[:：]2", "3:2"),
            (r"2[:：]3", "2:3"),
            (r"1[:：]1", "1:1"),
            (r"竖屏|竖版|portrait", "9:16"),
            (r"横屏|横版|landscape", "16:9"),
        ]

        found_ratio = None

        for pattern, ratio in patterns:
            p = re.compile(pattern, re.IGNORECASE)
            if p.search(text):
                found_ratio = ratio
                text = p.sub("", text)
                break

        text = re.sub(r",\s*,", ",", text).strip(" ,")
        return text, found_ratio

    def _extract_resolution_keyword(self, text: str) -> tuple[str, str | None]:
        t = text or ""

        p = re.compile(r"\b(1k|2k|4k)\b", re.IGNORECASE)
        m = p.search(t)
        found = m.group(1).upper() if m else None

        if found:
            t = p.sub("", t)
            t = re.sub(r"(画质|分辨率)", "", t, flags=re.IGNORECASE)

        t = re.sub(r"\s{2,}", " ", t).strip(" ,")
        return t, found

    def _resolve_resolution_by_acl(
        self,
        user_id: str,
        group_id: str,
        requested: str | None,
        slot_default: str,
    ) -> str:
        """
        仅 Vertex 渠道使用。
        白名单用户可指定 1K/2K/4K；
        非白名单用户无论群组状态，全部强制 1K。
        """
        uid = str(user_id).strip()
        users = {str(u).strip() for u in self.perm_users}

        req = (requested or slot_default or "1K").upper()
        if req not in {"1K", "2K", "4K"}:
            req = "1K"

        if uid in users:
            return req

        return "1K"

    def _normalize_config_resolution(self, value: str | None) -> str:
        res = (value or "1K").strip().upper()
        if res not in {"1K", "2K", "4K"}:
            return "1K"
        return res

    # =========================
    # Event Entries
    # =========================

    @filter.command("生图")
    async def entry_cmd_1(self, event: AstrMessageEvent):
        async for x in self._dispatch_generate(event):
            yield x

    @filter.command("gpt")
    async def entry_cmd_2(self, event: AstrMessageEvent):
        async for x in self._dispatch_generate(event):
            yield x

    @filter.command("flow")
    async def entry_cmd_3(self, event: AstrMessageEvent):
        async for x in self._dispatch_generate(event):
            yield x

    @filter.command("vertex图")
    async def entry_cmd_vertex_1(self, event: AstrMessageEvent):
        async for x in self._dispatch_generate(event):
            yield x

    @filter.command("vertex图2")
    async def entry_cmd_vertex_2(self, event: AstrMessageEvent):
        async for x in self._dispatch_generate(event):
            yield x

    @filter.regex(r"^/?[^\s]+")
    async def entry_dynamic_commands(self, event: AstrMessageEvent):
        """
        动态命令入口。
        用于支持配置面板里自定义的 command。
        """
        user_input = (event.message_str or "").strip()
        if not user_input:
            return

        first = user_input.split(maxsplit=1)[0].lstrip("/")

        static_commands = {"生图", "gpt", "flow", "vertex图", "vertex图2"}
        if first in static_commands:
            return

        slot = self.command_map.get(first)
        if not slot:
            return

        async for x in self._handle_generate(event, slot):
            yield x

    async def _dispatch_generate(self, event: AstrMessageEvent):
        user_input = (event.message_str or "").strip()

        if not user_input:
            return

        first = user_input.split(maxsplit=1)[0].lstrip("/")
        slot = self.command_map.get(first)

        if not slot:
            return

        async for x in self._handle_generate(event, slot):
            yield x

    # =========================
    # Core Handler
    # =========================

    async def _handle_generate(self, event: AstrMessageEvent, slot: SlotConfig):
        user_id = str(event.get_sender_id() or event.unified_msg_origin)
        group_id = event.message_obj.group_id or ""

        mode = (self.perm_mode or "disable").strip()

        if mode == "blacklist":
            if not self._check_permission(user_id, group_id):
                if not self.perm_silent:
                    yield event.plain_result(self.perm_no_permission_reply)
                return
        elif mode == "whitelist":
            pass
        else:
            if not self._check_permission(user_id, group_id):
                if not self.perm_silent:
                    yield event.plain_result(self.perm_no_permission_reply)
                return

        provider = self._resolve_provider_with_fallback(slot)
        if not provider:
            yield event.plain_result(f"❌ 命令 /{slot.command} 未配置可用提供商。")
            return

        ok, remain = await self._check_and_consume_quota(user_id, group_id)
        if not ok:
            yield event.plain_result(self.quota_exceeded_reply)
            return

        masked_uid = user_id[:4] + "****" + user_id[-4:] if len(user_id) > 8 else user_id
        user_input = (event.message_str or "").strip()

        plain_text = self._extract_plain_text_without_mentions(event)
        full_text = self._strip_command_prefix(plain_text, slot.command)

        logger.info(
            f"收到生图指令 - 命令: /{slot.command}, 用户: {masked_uid}, "
            f"原始输入: {user_input}, 纯文本提示词: {full_text}"
        )

        matched_preset_name = None
        raw_preset_text = ""
        raw_extra_text = ""
        has_extra = False

        preset_hub = getattr(self.context, "preset_hub", None)
        matched = False

        if full_text and preset_hub and hasattr(preset_hub, "get_all_keys"):
            all_keys = preset_hub.get_all_keys()
            all_keys.sort(key=len, reverse=True)
            prompt_lower = full_text.lower()

            for key in all_keys:
                key_lower = key.lower()

                if prompt_lower == key_lower or prompt_lower.startswith(key_lower + " "):
                    preset_val = preset_hub.resolve_preset(key)

                    if preset_val:
                        matched_preset_name = key
                        json_ratio = None

                        try:
                            if isinstance(preset_val, str) and preset_val.strip().startswith("{"):
                                preset_data = json.loads(preset_val)
                                if isinstance(preset_data, dict):
                                    raw_preset_text = preset_data.get("prompt", "")
                                    json_ratio = preset_data.get("aspect_ratio")
                                else:
                                    raw_preset_text = str(preset_val)
                            else:
                                raw_preset_text = str(preset_val)
                        except json.JSONDecodeError:
                            raw_preset_text = str(preset_val)

                        if json_ratio:
                            raw_preset_text += f" {json_ratio}"

                        if prompt_lower == key_lower:
                            raw_extra_text = ""
                        else:
                            raw_extra_text = full_text[len(key):].strip()
                            if raw_extra_text:
                                has_extra = True

                        matched = True
                        logger.info(f"命中全局预设: [{key}]")
                        break

        if not matched:
            raw_extra_text = full_text

        if not raw_preset_text and not raw_extra_text:
            yield event.plain_result("❌ 请提供图片生成的提示词或预设名称！")
            return

        images_data = await self._fetch_images_from_event(event)

        if matched_preset_name and not images_data:
            sender_id = event.get_sender_id()
            if sender_id:
                avatar_data = await self.get_avatar(str(sender_id))
                if avatar_data:
                    images_data.append((avatar_data, "image/jpeg"))

        clean_preset_text, preset_ratio = self._extract_ratio(raw_preset_text)
        clean_extra_text, extra_ratio = self._extract_ratio(raw_extra_text)
        final_ratio = extra_ratio if extra_ratio else preset_ratio

        if provider.api_type == "vertex":
            clean_extra_text, extra_res = self._extract_resolution_keyword(clean_extra_text)
            clean_preset_text, preset_res = self._extract_resolution_keyword(clean_preset_text)
            requested_res = extra_res or preset_res

            final_resolution = self._resolve_resolution_by_acl(
                user_id=user_id,
                group_id=group_id,
                requested=requested_res,
                slot_default=slot.default_resolution,
            )
        else:
            final_resolution = self._normalize_config_resolution(slot.default_resolution)

        if matched_preset_name:
            final_prompt = (
                f"{clean_preset_text}, {clean_extra_text}"
                if clean_extra_text
                else clean_preset_text
            )
        else:
            final_prompt = clean_extra_text

        msg = "🎨 正在生图 "

        if matched_preset_name:
            msg += f"「预设：{matched_preset_name}」"

        if has_extra:
            msg += "(已衔接额外提示词)"

        if final_ratio:
            msg += f" [比例: {final_ratio}]"

        if images_data:
            msg += f" [{len(images_data)}张参考图]"

        if remain >= 0:
            msg += f" [今日剩余: {remain}]"
        else:
            msg += " [不限次]"

        msg += f" [模型: {provider.model}]"
        
        # 仅 Vertex 渠道显示分辨率/画质提示
        if provider.api_type == "vertex":
            msg += f" [分辨率: {final_resolution}]"

        yield event.plain_result(msg + "...")

        task_id = hashlib.md5(
            f"{time.time()}{user_id}{slot.command}".encode()
        ).hexdigest()[:8]

        reply_id = (
            str(event.message_obj.message_id)
            if event.message_obj and event.message_obj.message_id
            else None
        )

        self.create_background_task(
            self._generate_and_send_image_async(
                prompt=final_prompt,
                event=event,
                provider=provider,
                images_data=images_data or None,
                aspect_ratio=final_ratio,
                resolution=final_resolution,
                task_id=task_id,
                reply_id=reply_id,
            )
        )

    # =========================
    # Image Fetching
    # =========================

    async def _fetch_images_from_event(
        self,
        event: AstrMessageEvent,
    ) -> list[tuple[bytes, str]]:
        images_data = []

        if not event.message_obj.message:
            return images_data

        for component in event.message_obj.message:
            if isinstance(component, Comp.Image):
                url = component.url or component.file
                if url:
                    data = await self._download_image(url)
                    if data:
                        images_data.append(data)

            elif isinstance(component, Comp.At):
                if component.qq != "all":
                    uid = str(component.qq)
                    self_id = str(event.get_self_id()).strip()

                    if self_id and uid == self_id:
                        continue

                    avatar_data = await self.get_avatar(uid)
                    if avatar_data:
                        images_data.append((avatar_data, "image/jpeg"))

        reply_comp = next(
            (c for c in event.message_obj.message if isinstance(c, Comp.Reply)),
            None,
        )

        if reply_comp and reply_comp.id:
            try:
                if event.bot:
                    resp = await event.bot.api.call_action(
                        "get_msg",
                        message_id=int(reply_comp.id),
                    )
                    message_content = resp.get("message")

                    if isinstance(message_content, list):
                        for seg in message_content:
                            if seg.get("type") == "image":
                                data = seg.get("data", {})
                                url = data.get("url") or data.get("file")

                                if url:
                                    img_data = await self._download_image(url)
                                    if img_data:
                                        images_data.append(img_data)
            except Exception as e:
                logger.debug(f"NapCat get_msg failed: {e}")

        return images_data

    @staticmethod
    async def get_avatar(user_id: str) -> bytes | None:
        url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"

        try:
            path = await download_image_by_url(url)
            if path:
                with open(path, "rb") as f:
                    return f.read()
        except Exception:
            pass

        return None

    async def _download_image(self, url: str) -> tuple[bytes, str] | None:
        try:
            data = None

            if os.path.exists(url) and os.path.isfile(url):
                with open(url, "rb") as f:
                    data = f.read()
            else:
                path = await download_image_by_url(url)
                if path:
                    with open(path, "rb") as f:
                        data = f.read()

            if not data:
                return None

            if len(data) > self.max_image_size_mb * 1024 * 1024:
                logger.warning(f"图片超过大小限制 ({self.max_image_size_mb}MB)")
                return None

            mime = "image/png"

            if data.startswith(b"\xff\xd8"):
                mime = "image/jpeg"
            elif data.startswith(b"GIF"):
                mime = "image/gif"
            elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                mime = "image/webp"

            return data, mime

        except Exception as e:
            logger.error(f"获取图片失败: {e}")

        return None

    # =========================
    # Send / Background
    # =========================

    def create_background_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    async def _reply_error(self, event: AstrMessageEvent, text: str):
        try:
            chain = []

            if event.message_obj and event.message_obj.message_id:
                chain.append(Comp.Reply(id=str(event.message_obj.message_id)))

            chain.append(Comp.Plain(text))
            await event.send(event.chain_result(chain))
        except Exception:
            await event.send(event.plain_result(text))

    async def _safe_send_chain(self, event: AstrMessageEvent, components: List):
        try:
            await event.send(event.chain_result(components))
        except Exception as e:
            msg = str(e)
            if "retcode=1200" in msg or "Timeout" in msg or "ActionFailed" in msg:
                logger.warning("检测到发送超时(retcode=1200)，忽略报错。")
            else:
                logger.error(f"发送异常: {e}")

    async def _generate_and_send_image_async(
        self,
        prompt: str,
        event: AstrMessageEvent,
        provider: ProviderConfig,
        images_data: list[tuple[bytes, str]] | None = None,
        aspect_ratio: str | None = None,
        resolution: str = "1K",
        task_id: str | None = None,
        reply_id: str | None = None,
    ):
        if not task_id:
            task_id = hashlib.md5(f"{time.time()}".encode()).hexdigest()[:8]

        final_ar = aspect_ratio if aspect_ratio != "自动" else None

        generator = AIImageGenerator(
            main_config=provider,
            timeout=self.timeout,
        )

        try:
            results, error = await generator.generate_image(
                prompt=prompt,
                images_data=images_data,
                aspect_ratio=final_ar,
                image_size=resolution,
                task_id=task_id,
            )

            if error:
                await self._reply_error(event, f"❌ 生成失败: {error}")
                return

            if not results:
                return

            logger.info(f"任务完成 [{task_id}] - 生成了 {len(results)} 张图片")

            components = []

            if reply_id:
                components.append(Comp.Reply(id=reply_id))

            for img_bytes in results:
                try:
                    file_path = save_temp_img(img_bytes)
                    components.append(Comp.Image.fromFileSystem(file_path))
                except Exception as e:
                    logger.error(f"保存图片失败: {e}")

            await self._safe_send_chain(event, components)

        except Exception as e:
            logger.error(f"任务失败: {e}", exc_info=True)
            await self._reply_error(event, "❌ 生成过程中发生未知错误")
        finally:
            await generator.close_session()
