"""Scan JS bundles for hardcoded API routes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from rebrowse.reverse.extractor import registrable_domain

# Embeds, ads and analytics: their bundles describe their own APIs, not the page's.
THIRD_PARTY_SCRIPT_DOMAINS = frozenset({
    "youtube.com", "youtube-nocookie.com", "google.com", "gstatic.com", "googleapis.com",
    "googletagmanager.com", "google-analytics.com", "doubleclick.net", "googlesyndication.com",
    "facebook.net", "facebook.com", "twitter.com", "x.com", "linkedin.com", "hotjar.com",
    "intercom.io", "stripe.com", "stripe.network", "cloudflareinsights.com", "sentry-cdn.com",
    "segment.com", "segment.io", "jsdelivr.net", "unpkg.com", "cloudflare.com", "recaptcha.net",
    "hcaptcha.com", "vimeo.com", "tiktok.com", "spotify.com",
})


def is_third_party_bundle(url: str, page_domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host or registrable_domain(host) == registrable_domain(page_domain):
        return False
    return registrable_domain(host) in THIRD_PARTY_SCRIPT_DOMAINS


@dataclass
class BundleRoute:
    path: str
    url: str
    method: str
    source_bundle: str
    match_type: str  # "string_literal" | "call" | "template"


_PATH_CHARS = r"[a-zA-Z0-9/_\-]"

API_PATH_RE = re.compile(rf"""["'`](/api/{_PATH_CHARS}{{2,80}})(?:[?#][^"'`]*)?["'`]""")
VERSIONED_API_RE = re.compile(rf"""["'`](/v[0-9]+/{_PATH_CHARS}{{2,80}})(?:[?#][^"'`]*)?["'`]""")
CALL_RE = re.compile(
    rf"""(fetch|axios|\.get|\.post|\.put|\.patch|\.delete)\s*\(\s*["'`](/{_PATH_CHARS}{{3,80}})(?:[?#][^"'`]*)?["'`]"""
)
# Template literal such as `/api/users/${id}/posts` -> /api/users/{id}/posts
TEMPLATE_RE = re.compile(r"`(/(?:api|v[0-9]+)/(?:[a-zA-Z0-9/_\-]|\$\{[^}`]{1,40}\}){2,120})`")
_TEMPLATE_VAR = re.compile(r"\$\{\s*([^}]*?)\s*\}")

_CALL_METHODS = {".post": "POST", ".put": "PUT", ".patch": "PATCH", ".delete": "DELETE"}

SKIP_PREFIXES = {
    "_next", "__next", "__webpack", "__vite", "static",
    "assets", "public", "favicon", "manifest", "service-worker", "workbox",
}

SKIP_EXTENSIONS = (
    ".js", ".json", ".html", ".xml", ".svg", ".png", ".jpg",
    ".css", ".woff2", ".ttf", ".map", ".ts", ".tsx", ".jsx", ".vue", ".md",
)


def _should_skip(path: str) -> bool:
    if len(path) < 4:
        return True
    segments = path.strip("/").split("/")
    if segments and segments[0] in SKIP_PREFIXES:
        return True
    if path.endswith(SKIP_EXTENSIONS):
        return True
    return path.strip("/").count("/") == 0 and len(path.strip("/")) <= 3


def _template_to_path(raw: str) -> str:
    def name(m: re.Match) -> str:
        ident = re.sub(r"[^a-zA-Z0-9_]", "_", m.group(1).split(".")[-1]).strip("_")
        return "{" + (ident or "param") + "}"
    return _TEMPLATE_VAR.sub(name, raw)


def scan_bundles_for_routes(bundles: dict[str, str], page_origin: str) -> list[BundleRoute]:
    found: dict[tuple[str, str], BundleRoute] = {}

    def add(path: str, method: str, bundle_url: str, match_type: str) -> None:
        if _should_skip(path):
            return
        key = (method, path)
        if key in found:
            return
        if method != "GET":
            guess = found.get(("GET", path))
            if guess and guess.match_type != "call":
                del found[("GET", path)]
        elif match_type != "call" and any(p == path for (_, p) in found):
            return
        found[key] = BundleRoute(path, f"{page_origin}{path}", method, bundle_url, match_type)

    for bundle_url, code in bundles.items():
        for m in CALL_RE.finditer(code):
            add(m.group(2), _CALL_METHODS.get(m.group(1), "GET"), bundle_url, "call")
        for m in TEMPLATE_RE.finditer(code):
            add(_template_to_path(m.group(1)), "GET", bundle_url, "template")
        for pattern in (API_PATH_RE, VERSIONED_API_RE):
            for m in pattern.finditer(code):
                add(m.group(1), "GET", bundle_url, "string_literal")

    return list(found.values())
