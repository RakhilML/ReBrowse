"""Headless browser capture — record all network traffic via Playwright."""

from __future__ import annotations
import asyncio
import json
from urllib.parse import urlparse
from playwright.async_api import async_playwright, Page
from rebrowse import config
from rebrowse.models import RawRequest, CaptureResult


CHROME_UA = config.CHROME_UA

STATIC_EXTENSIONS = {
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".webp", ".avif",
}

_browser_sem = asyncio.Semaphore(config.MAX_CONCURRENT_BROWSERS)


def _is_static(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in STATIC_EXTENSIONS)


def _safe_headers(headers: dict) -> dict[str, str]:
    return {k: v for k, v in headers.items() if isinstance(v, str)}


async def _scroll_page(page: Page, scrolls: int = 3, delay: float = 1.0):
    """Scroll down the page to trigger lazy-loaded content."""
    for _ in range(scrolls):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await asyncio.sleep(delay)
    # Scroll back to top
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)


async def capture_session(
    url: str,
    cookies: list[dict] | None = None,
    timeout_ms: int | None = None,
) -> CaptureResult:
    """Navigate to a URL, record all network requests/responses."""
    timeout_ms = timeout_ms or config.CAPTURE_TIMEOUT_MS
    requests: list[RawRequest] = []
    js_bundles: dict[str, str] = {}
    final_url = url
    page_html: str | None = None

    async with _browser_sem:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--disable-extensions",
                ],
            )
            context = await browser.new_context(
                user_agent=CHROME_UA,
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )

            if cookies:
                await context.add_cookies(cookies)

            page = await context.new_page()

            async def on_response(response):
                req = response.request
                req_url = req.url

                if _is_static(req_url):
                    if req_url.endswith(".js") and len(js_bundles) < config.MAX_JS_BUNDLES:
                        try:
                            body = await response.text()
                            if len(body) < 2_000_000:
                                js_bundles[req_url] = body
                        except Exception:
                            pass
                    return

                try:
                    resp_body = None
                    content_type = response.headers.get("content-type", "")
                    if "json" in content_type or "text" in content_type:
                        try:
                            raw_body = await response.body()
                            if len(raw_body) <= config.MAX_BODY_SIZE:
                                resp_body = raw_body.decode("utf-8", errors="replace")
                        except Exception:
                            pass

                    req_body = req.post_data if req.post_data else None

                    requests.append(RawRequest(
                        url=req_url,
                        method=req.method,
                        request_headers=_safe_headers(req.headers),
                        request_body=req_body,
                        response_status=response.status,
                        response_headers=_safe_headers(response.headers),
                        response_body=resp_body,
                    ))
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                final_url = page.url
            except Exception:
                final_url = page.url

            # Scroll to trigger lazy-loaded content and API calls
            try:
                await _scroll_page(page)
            except Exception:
                pass

            # Wait for lazy-loaded API calls
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(3.0)

            try:
                page_html = await page.content()
            except Exception:
                pass

            await browser.close()

    domain = urlparse(final_url).netloc
    return CaptureResult(
        requests=requests,
        domain=domain,
        final_url=final_url,
        html=page_html,
        js_bundles=js_bundles,
    )
