"""Run the gallery locally with the real AstrBot bridge and isolated sample data.

Requires fastapi, uvicorn and an AstrBot checkout containing api/web.py and
dashboard/plugin_page_bridge.js. Nothing is written to a running bot's data.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import secrets
import sys
import tempfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw

from gallery_store import GalleryStore
from tests.web_support import load_web_api, make_app

HOST_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>映像 · 本地功能预览</title>
<style>html,body{margin:0;height:100%;font-family:system-ui;background:#f3f5f1}header{height:25px;box-sizing:border-box;padding:5px 16px;color:#687b6d;font-size:10px;letter-spacing:.4px;display:flex;justify-content:space-between}iframe{display:block;width:100%;height:calc(100% - 25px);border:0}</style></head>
<body><header><span>本地功能预览 · 示例图片 · 数据与 AstrBot 隔离</span><span>官方 Plugin Page Bridge / 受限 iframe</span></header><iframe id="page" sandbox="allow-scripts allow-forms allow-downloads" title="图片管理" src="/page/index.html"></iframe>
<script>
const frame = document.getElementById('page');
const token = '__TOKEN__';
const channel = 'astrbot-plugin-page';
window.previewTheme = isDark => frame.contentWindow.postMessage({channel, kind:'context', context:{isDark}}, '*');
window.addEventListener('message', async event => {
  if(event.source !== frame.contentWindow || event.data?.channel !== channel) return;
  const message = event.data;
  const send = body => event.source.postMessage({channel, ...body}, '*');
  if(message.kind === 'ready') {
    send({kind:'context',context:{pluginName:'astrbot_plugin_ai_image',pageName:'gallery',pageTitle:'图片管理',locale:'zh-CN',isDark:new URLSearchParams(location.search).get('theme')==='dark',i18n:{}}});
    return;
  }
  if(message.kind !== 'request') return;
  try {
    if(!/^[\w/-]+$/.test(message.endpoint) || message.endpoint.includes('..')) throw new Error('Invalid endpoint');
    const url = new URL('/api/astrbot_plugin_ai_image/' + message.endpoint, location.origin);
    for(const [key,value] of Object.entries(message.params || {})) url.searchParams.set(key, value);
    const init = {headers:{Authorization:'Bearer ' + token}};
    if(message.action === 'api:post') {init.method='POST';init.headers['Content-Type']='application/json';init.body=JSON.stringify(message.body);}
    else if(message.action === 'files:upload') {init.method='POST';const form=new FormData();form.set('file',new Blob([message.fileBuffer],{type:message.fileType}),message.fileName);init.body=form;}
    else if(!['api:get','files:download'].includes(message.action)) throw new Error('Unsupported bridge action');
    const response = await fetch(url, init);
    if(!response.ok) {const result=await response.json();throw new Error(result.message || 'Request failed');}
    let data;
    if(message.action === 'files:download') {
      const blob=await response.blob();const objectUrl=URL.createObjectURL(blob);const a=document.createElement('a');
      const header=response.headers.get('content-disposition') || '';
      const utf=header.match(/filename\*=utf-8''([^;]+)/i), plain=header.match(/filename="?([^";]+)"?/i);
      const filename=message.filename || (utf?decodeURIComponent(utf[1]):plain?.[1]) || 'download';
      a.href=objectUrl;a.download=filename;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(objectUrl),1000);data={filename};
    } else { data=await response.json();if(data.status==='error')throw new Error(data.message);if(data.status==='ok' && 'data' in data)data=data.data; }
    send({kind:'response',requestId:message.requestId,ok:true,data});
  } catch(error) {send({kind:'response',requestId:message.requestId,ok:false,error:error.message});}
});
</script></body></html>"""


def seed(store):
    """Procedural fixtures, not user images or AI provider calls."""
    if store.overview()["total"]:
        return
    palettes = [
        ((191, 209, 187), (35, 76, 58)),
        ((227, 201, 172), (134, 90, 66)),
        ((177, 207, 211), (40, 93, 105)),
        ((217, 204, 188), (83, 93, 70)),
        ((213, 184, 166), (133, 83, 73)),
        ((180, 187, 212), (65, 71, 110)),
    ]
    titles = [
        "山野来信",
        "海风经过的地方",
        "在云端散步",
        "慢慢生长",
        "日落收藏家",
        "一场蓝色的梦",
        "窗外，春天",
        "沙丘上的光",
        "不期而遇的绿",
        "温柔的边界",
        "晚风来信",
        "远山如黛",
        "给明天的风景",
        "晨光与旧时光",
        "夏日备忘录",
        "星河慢行",
        "一片安静",
        "森林的呼吸",
    ]
    albums = [store.save_album(name) for name in ["山野与远方", "色彩实验", "日常灵感"]]
    for index, title in enumerate(titles):
        width, height = [
            (900, 1120),
            (1000, 740),
            (900, 900),
            (1000, 1200),
            (1050, 760),
        ][index % 5]
        light, dark = palettes[index % len(palettes)]
        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)
        for y in range(height):
            blend = y / height * 0.34
            color = tuple(
                round(a * (1 - blend) + b * blend) for a, b in zip(light, dark)
            )
            draw.line((0, y, width, y), fill=color)
        sun_x, sun_y = int(width * (0.25 if index % 2 else 0.7)), int(height * 0.23)
        radius = int(width * (0.11 + 0.03 * (index % 3)))
        draw.ellipse(
            (sun_x - radius, sun_y - radius, sun_x + radius, sun_y + radius),
            fill=(244, 234, 207),
        )
        for layer in range(4):
            blend = 0.25 + layer * 0.19
            color = tuple(
                round(a * (1 - blend) + b * blend) for a, b in zip(light, dark)
            )
            points = [(0, height)]
            for x in range(0, width + 1, 5):
                y = (
                    height * (0.49 + layer * 0.135)
                    + math.sin(x / width * (4 + index % 4) + index + layer)
                    * height
                    * 0.085
                )
                points.append((x, int(y)))
            points.append((width, height))
            draw.polygon(points, fill=color)
        output = BytesIO()
        image.save(output, "PNG")
        record = store.add_image(
            output.getvalue(),
            {
                "title": title,
                "prompt": f"{title}，远山与柔和的光线，低饱和色彩，宁静、自然的画面。",
                "model": [
                    "gemini-3.1-flash-image",
                    "gpt-image-2",
                    "gemini-3-pro-image",
                ][index % 3],
                "provider": "示例提供商",
                "resolution": "2K" if index % 3 else "1K",
                "command": "生图",
                "user_name": "示例创作者",
                "duration_ms": 18300 + index * 650,
                "reference_count": 1 if index % 5 == 0 else 0,
            },
            source="imported" if index in (4, 9, 14) else "generated",
            filename="sample.png",
        )["image"]
        store.update_image(
            record["id"],
            {
                "favorite": index in (0, 2, 6, 11),
                "tags": [["风景", "壁纸"], ["色彩", "灵感"], ["森林", "自然"]][
                    index % 3
                ],
                "album_ids": [albums[index % 3]["id"]] if index < 12 else [],
            },
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--bridge-sdk", type=Path)
    args = parser.parse_args()
    bridge_path = args.bridge_sdk
    if not bridge_path:
        spec = importlib.util.find_spec("astrbot")
        if spec and spec.origin:
            bridge_path = (
                Path(spec.origin).parent / "dashboard" / "plugin_page_bridge.js"
            )
    if not bridge_path or not bridge_path.is_file():
        parser.error("Provide --bridge-sdk pointing to AstrBot's plugin_page_bridge.js")
    web = load_web_api()
    import uvicorn
    from fastapi.responses import FileResponse, HTMLResponse

    with tempfile.TemporaryDirectory(prefix="ai-image-preview-") as temp:
        store = GalleryStore(Path(temp))
        if args.seed:
            seed(store)
        token = secrets.token_hex(24)
        app, _context, _api, _api_class = make_app(store, web, token)

        @app.get("/")
        async def home():
            return HTMLResponse(HOST_PAGE.replace("__TOKEN__", token))

        @app.get("/health")
        async def health():
            return {"status": "ok", "mode": "isolated-preview"}

        @app.get("/bridge-sdk.js")
        async def sdk():
            return FileResponse(
                bridge_path,
                media_type="text/javascript",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        @app.get("/page/{asset:path}")
        async def asset(asset: str):
            page_root = ROOT / "pages" / "gallery"
            path = (page_root / asset).resolve()
            if not path.is_relative_to(page_root) or not path.is_file():
                return web.error_response("Not found", status_code=404)
            headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}
            if path.name == "index.html":
                html = path.read_text(encoding="utf-8").replace(
                    "</head>", '<script src="/bridge-sdk.js"></script></head>'
                )
                return HTMLResponse(html, headers=headers)
            media_type = (
                "text/javascript"
                if path.suffix == ".js"
                else "text/css"
                if path.suffix == ".css"
                else None
            )
            return FileResponse(path, media_type=media_type, headers=headers)

        print(f"Isolated gallery preview: http://127.0.0.1:{args.port}", flush=True)
        uvicorn.run(
            app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning"
        )


if __name__ == "__main__":
    main()
