"""Browser acceptance checks against tools/preview_gallery.py --seed only."""

from __future__ import annotations

import argparse
import json
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.request import urlopen

from PIL import Image
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/gallery"))
    args = parser.parse_args()
    # Never run mutating acceptance checks against a user's real gallery.
    with urlopen(args.url + "/health", timeout=5) as response:
        assert json.load(response).get("mode") == "isolated-preview", (
            "Only an isolated preview server is supported"
        )
    args.artifacts.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000}, accept_downloads=True
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url)
        frame = page.frame_locator("#page")
        cards = frame.locator(".image-card")
        try:
            expect(cards).to_have_count(18)
            expect(frame.locator(".card-art img.loaded").first).to_be_visible()
            page.screenshot(path=args.artifacts / "gallery-desktop.png")
            print(
                "PASS: official bridge, sandboxed iframe, 18 real persisted images",
                flush=True,
            )

            frame.locator("#search").fill("山野来信")
            expect(cards).to_have_count(1)
            frame.locator("#clear-search").click()
            expect(cards).to_have_count(18)
            frame.locator("#filter-toggle").click()
            frame.locator("#orientation-filter").select_option("portrait")
            expect(cards).to_have_count(7)
            frame.locator("#reset-filters").click()
            expect(cards).to_have_count(18)
            frame.locator("#filter-toggle").click()
            frame.locator("#search").fill("no-such-picture-xyz")
            expect(frame.locator("#empty-state")).to_be_visible()
            expect(frame.locator("#empty-title")).to_have_text("还没找到这份灵感")
            frame.locator("#clear-search").click()
            expect(cards).to_have_count(18)
            print(
                "PASS: prompt search, orientation filter, empty result and reset",
                flush=True,
            )

            cards.first.locator(".open-image").click()
            expect(frame.locator("#preview-image")).to_be_visible()
            expect(frame.locator("#detail-title")).not_to_have_text("正在读取图片…")
            page.screenshot(path=args.artifacts / "gallery-detail.png")
            frame.locator("#preview-zoom").click()
            expect(frame.locator("#preview-zoom")).to_have_attribute(
                "aria-pressed", "true"
            )
            frame.locator("#preview-zoom").click()
            original_favorite = frame.locator("#detail-favorite").get_attribute(
                "aria-pressed"
            )
            frame.locator("#detail-favorite").click()
            expect(frame.locator("#detail-favorite")).to_have_attribute(
                "aria-pressed", "false" if original_favorite == "true" else "true"
            )
            frame.locator("#edit-title").fill("联调作品 · 山风")
            frame.locator("#edit-tags").fill("验收, 壁纸")
            frame.locator("#edit-notes").fill(
                '<img src=x onerror="alert(1)"> 只是备注文本'
            )
            frame.locator("#save-detail").click()
            expect(frame.locator("#detail-title")).to_have_text("联调作品 · 山风")
            frame.locator("#close-viewer").click()
            expect(frame.locator("#viewer")).not_to_be_visible()
            frame.locator("#search").fill("验收")
            expect(cards).to_have_count(1)
            cards.first.locator(".open-image").click()
            expect(frame.locator("#edit-notes")).to_have_value(
                '<img src=x onerror="alert(1)"> 只是备注文本'
            )
            assert frame.locator("#detail-content img").count() == 0
            # Exercise clipboard fallback in the restricted iframe.
            frame.locator("#copy-prompt").click()
            if frame.locator("#action-dialog").is_visible():
                expect(frame.locator("#action-fields textarea")).to_contain_text("")
                frame.locator("#action-submit").click()
            frame.locator("#close-viewer").click()
            print(
                "PASS: detail preview, zoom, favorites, metadata editing and safe text rendering",
                flush=True,
            )

            frame.locator("#create-album").click()
            frame.locator('#action-fields input[name="name"]').fill("验收相册")
            frame.locator("#action-submit").click()
            expect(frame.locator("#album-nav")).to_contain_text("验收相册")
            frame.locator("#selection-mode").click()
            cards.first.locator('input[type="checkbox"]').check()
            expect(frame.locator("#batch-bar")).to_be_visible()
            frame.locator('[data-batch="album"]').click()
            frame.locator("#action-fields select").select_option(label="验收相册")
            frame.locator("#action-submit").click()
            expect(frame.locator("#batch-bar")).not_to_be_visible()
            frame.locator("#album-nav button").filter(has_text="验收相册").click()
            expect(cards).to_have_count(1)
            expect(frame.locator("#page-title")).to_have_text("验收相册")
            print("PASS: album creation and batch membership", flush=True)

            frame.locator("#selection-mode").click()
            cards.first.locator('input[type="checkbox"]').check()
            with page.expect_download() as event:
                frame.locator('[data-batch="download"]').click()
            download = event.value
            download.save_as(args.artifacts / "acceptance-export.zip")
            with zipfile.ZipFile(args.artifacts / "acceptance-export.zip") as archive:
                manifest = json.loads(archive.read("metadata.json"))
                assert manifest["images"][0]["title"] == "联调作品 · 山风"
                assert len(archive.namelist()) == 2
            frame.locator('[data-batch="trash"]').click()
            expect(frame.locator("#action-title")).to_contain_text("移入回收站")
            frame.locator("#action-submit").click()
            expect(frame.locator("#empty-state")).to_be_visible()
            frame.locator(".toast button").filter(has_text="撤销").click()
            expect(cards).to_have_count(1)
            print(
                "PASS: browser ZIP download, metadata manifest, trash and undo",
                flush=True,
            )

            frame.locator("#selection-mode").click()
            cards.first.locator('input[type="checkbox"]').check()
            frame.locator('[data-batch="trash"]').click()
            frame.locator("#action-submit").click()
            expect(frame.locator("#empty-state")).to_be_visible()
            frame.locator("#trash-nav").click()
            expect(cards).to_have_count(1)
            frame.locator("#selection-mode").click()
            cards.first.locator('input[type="checkbox"]').check()
            frame.locator('[data-batch="purge"]').click()
            frame.locator("#action-cancel").click()
            expect(cards).to_have_count(1)
            frame.locator('[data-batch="purge"]').click()
            frame.locator("#action-submit").click()
            expect(frame.locator("#empty-title")).to_have_text("回收站空空如也")
            frame.locator('[data-view="all"]').click()
            expect(cards).to_have_count(17)
            print(
                "PASS: permanent deletion requires confirmation and affects only trash",
                flush=True,
            )

            buffer = BytesIO()
            Image.new("RGB", (270, 410), "coral").save(buffer, "PNG")
            upload = {
                "name": "本地导入.png",
                "mimeType": "image/png",
                "buffer": buffer.getvalue(),
            }
            frame.locator("#file-input").set_input_files(upload)
            expect(cards).to_have_count(18)
            frame.locator("#file-input").set_input_files(upload)
            expect(
                frame.locator(".toast").filter(has_text="1 张已存在")
            ).to_be_visible()
            expect(cards).to_have_count(18)
            print(
                "PASS: multipart import and duplicate detection through bridge",
                flush=True,
            )

            # Keyboard navigation and unsaved changes are checked on a real viewer.
            cards.first.locator(".open-image").click()
            expect(frame.locator("#preview-image")).to_be_visible()
            frame.locator("#edit-title").fill("尚未保存")
            frame.locator("#close-viewer").click()
            expect(frame.locator("#action-title")).to_have_text("放弃尚未保存的修改？")
            frame.locator("#action-cancel").click()
            expect(frame.locator("#viewer")).to_be_visible()
            frame.locator("#close-viewer").click()
            frame.locator("#action-submit").click()
            expect(frame.locator("#viewer")).not_to_be_visible()

            page.set_viewport_size({"width": 390, "height": 844})
            expect(cards.first).to_be_visible()
            expect(frame.locator("#toasts .toast")).to_have_count(0, timeout=12000)
            frame.locator("#page-title").scroll_into_view_if_needed()
            page.screenshot(path=args.artifacts / "gallery-mobile.png")
            child = next(f for f in page.frames if "/page/" in f.url)
            assert child.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            ), "Mobile page overflows horizontally"
            page.evaluate("window.previewTheme(true)")
            expect(frame.locator("html")).to_have_attribute("data-theme", "dark")
            page.screenshot(path=args.artifacts / "gallery-mobile-dark.png")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.screenshot(path=args.artifacts / "gallery-dark.png")
            page.evaluate("window.previewTheme(false)")
            assert not errors, errors
            print(
                "PASS: unsaved edits, mobile layout, live light/dark theme, no JavaScript errors",
                flush=True,
            )
            print(f"Artifacts: {args.artifacts.resolve()}", flush=True)
        except BaseException:
            page.screenshot(path=args.artifacts / "failure.png", full_page=True)
            print("JavaScript errors:", errors, flush=True)
            raise
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
