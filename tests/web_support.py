"""Integration harness using AstrBot's real public web helper.

ASTRBOT_WEB_API may point at astrbot/api/web.py in an AstrBot source checkout;
this permits testing the latest Page API without upgrading an installed bot.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_web_api():
    source = os.environ.get("ASTRBOT_WEB_API")
    if source:
        spec = importlib.util.spec_from_file_location(
            "_astrbot_web_integration", source
        )
        web = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(web)
        return web
    # Avoid importing an old AstrBot's entire api package just to detect support.
    spec = importlib.util.find_spec("astrbot")
    if not spec or not spec.origin:
        raise ImportError("AstrBot is not installed")
    source = Path(spec.origin).parent / "api" / "web.py"
    if not source.exists():
        raise ImportError("This AstrBot version does not include astrbot.api.web")
    spec = importlib.util.spec_from_file_location("_astrbot_web_integration", source)
    web = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web)
    return web


def make_app(store, web, token="local-test-session"):
    from fastapi import FastAPI, Request

    import gallery_store

    package = types.ModuleType("_gallery_integration")
    package.__path__ = [str(ROOT)]
    aliases = {
        "_gallery_integration": package,
        "_gallery_integration.gallery_store": gallery_store,
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.web": web,
    }
    with patch.dict(sys.modules, aliases):
        spec = importlib.util.spec_from_file_location(
            "_gallery_integration.gallery_api", ROOT / "gallery_api.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    class Context:
        def __init__(self):
            self.registered_web_apis = []

        def register_web_api(self, route, handler, methods, desc):
            for index, item in enumerate(self.registered_web_apis):
                if item[0] == route and item[2] == methods:
                    self.registered_web_apis[index] = (route, handler, methods, desc)
                    return
            self.registered_web_apis.append((route, handler, methods, desc))

    context = Context()
    api = module.GalleryAPI(context, store)
    app = FastAPI()

    # An explicit annotation avoids a local Request forward reference in FastAPI.
    async def dispatch(request_):
        path = request_.url.path.removeprefix("/api")
        for route, handler, methods, _desc in context.registered_web_apis:
            pattern = re.sub(r"<([a-z_]+)>", r"(?P<\1>[^/]+)", route)
            match = re.fullmatch(pattern, path)
            if match and request_.method in methods:
                username = (
                    "preview-admin"
                    if request_.headers.get("authorization") == f"Bearer {token}"
                    else None
                )
                incoming = web.PluginRequest(
                    request_,
                    plugin_name="astrbot_plugin_ai_image",
                    username=username,
                    path_params=match.groupdict(),
                )
                with web.bind_request_context(incoming):
                    return await handler(**match.groupdict())
        return web.error_response("Route not found", status_code=404)

    dispatch.__annotations__["request_"] = Request
    app.add_api_route("/api/{endpoint:path}", dispatch, methods=["GET", "POST"])
    return app, context, api, module.GalleryAPI
