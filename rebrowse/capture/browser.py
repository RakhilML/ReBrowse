"""Headless browser capture — record all network traffic via Playwright."""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from rebrowse import config
from rebrowse.capture.steps import Step, parse_steps, run_steps
from rebrowse.models import CaptureResult, RawRequest

STATIC_EXTENSIONS = (
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".webp", ".avif",
)

_browser_sem = asyncio.Semaphore(config.MAX_CONCURRENT_BROWSERS)


def _safe_headers(headers: dict) -> dict[str, str]:
    return {k: v for k, v in headers.items() if isinstance(v, str)}


async def _all_headers(obj) -> dict:
    try:
        return await obj.all_headers()
    except PlaywrightError:
        return obj.headers


async def _text_body(response, content_type: str) -> str | None:
    if "json" not in content_type and "text" not in content_type:
        return None
    try:
        raw = await response.body()
    except PlaywrightError:
        return None
    if len(raw) > config.MAX_BODY_SIZE:
        return None
    return raw.decode("utf-8", errors="replace")


async def _scroll_page(page: Page, scrolls: int = 3, delay: float = 1.0) -> None:
    for _ in range(scrolls):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await asyncio.sleep(delay)
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)


async def capture_session(
    url: str,
    timeout_ms: int | None = None,
    steps: str | list[Step] | None = None,
) -> CaptureResult:
    timeout_ms = timeout_ms or config.CAPTURE_TIMEOUT_MS
    parsed_steps = parse_steps(steps)
    requests: list[RawRequest] = []
    js_bundles: dict[str, str] = {}
    final_url = url
    page_html: str | None = None
    cookies: list[dict] = []

    async with _browser_sem, async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        async def on_response(response):
            req = response.request
            path = urlparse(req.url).path.lower()
            if path.endswith(STATIC_EXTENSIONS):
                if path.endswith(".js") and len(js_bundles) < config.MAX_JS_BUNDLES:
                    try:
                        body = await response.text()
                    except (PlaywrightError, ValueError):
                        return
                    if len(body) < config.MAX_BUNDLE_CHARS:
                        js_bundles[req.url] = body
                return
            try:
                req_headers = await _all_headers(req)
                resp_headers = await _all_headers(response)
                requests.append(RawRequest(
                    url=req.url,
                    method=req.method,
                    request_headers=_safe_headers(req_headers),
                    request_body=req.post_data or None,
                    response_status=response.status,
                    response_headers=_safe_headers(resp_headers),
                    response_body=await _text_body(response, resp_headers.get("content-type", "")),
                ))
            except (PlaywrightError, ValueError):
                pass

        page.on("response", on_response)
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightError:
                pass
            if page.url and page.url != "about:blank":
                final_url = page.url
            await asyncio.sleep(config.CAPTURE_SETTLE_S)
            try:
                await _scroll_page(page)
            except PlaywrightError:
                pass
            if parsed_steps:
                await run_steps(page, parsed_steps)
            await asyncio.sleep(config.CAPTURE_SETTLE_S)
            try:
                page_html = await page.content()
            except PlaywrightError:
                pass
            cookies = await context.cookies()
        finally:
            await browser.close()

    return CaptureResult(
        requests=requests,
        domain=urlparse(final_url).netloc,
        final_url=final_url,
        cookies=cookies,
        html=page_html,
        js_bundles=js_bundles,
    )
