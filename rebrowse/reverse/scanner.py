"""Scan JS bundles for hardcoded API routes and GraphQL operations."""

from __future__ import annotations

import re
from collections.abc import Collection
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
    match_type: str  # "string_literal" | "call" | "template" | "recorded_prefix"


@dataclass
class BundleOperation:
    kind: str
    name: str
    source_bundle: str
    match_type: str  # "document" | "compiled"


_PATH_CHARS = r"[a-zA-Z0-9/_\-]"
_DOTTED_PATH_CHARS = r"[a-zA-Z0-9/_.\-]"
_INTERPOLATION = r"\$\{[^}`]{1,40}\}"
_TEMPLATE_PART = rf"(?:{_PATH_CHARS}|{_INTERPOLATION})"
_DOTTED_TEMPLATE_PART = rf"(?:{_DOTTED_PATH_CHARS}|{_INTERPOLATION})"
_CALLER = r"(fetch|axios|\.get|\.post|\.put|\.patch|\.delete)\s*\(\s*"

API_PATH_RE = re.compile(rf"""["'`](/api/{_PATH_CHARS}{{2,80}})(?:[?#][^"'`]*)?["'`]""")
VERSIONED_API_RE = re.compile(rf"""["'`](/v[0-9]+/{_PATH_CHARS}{{2,80}})(?:[?#][^"'`]*)?["'`]""")
CALL_RE = re.compile(rf"""{_CALLER}["'`](/{_PATH_CHARS}{{3,80}})(?:[?#][^"'`]*)?["'`]""")
# Only for coverage: legacy dotted paths such as fetch("/ajax/cart.php"), and calls with a
# template literal: api.delete(`/api/items/${id}`) -> DELETE /api/items/{id}
WIDE_CALL_RE = re.compile(rf"""{_CALLER}["'](/{_DOTTED_PATH_CHARS}{{3,80}})(?:[?#][^"']*)?["']""")
CALL_TEMPLATE_RE = re.compile(rf"{_CALLER}`(/{_DOTTED_TEMPLATE_PART}{{3,120}})(?:[?#][^`]*)?`")
# Template literal such as `/api/users/${id}/posts` -> /api/users/{id}/posts
TEMPLATE_RE = re.compile(rf"`(/(?:api|v[0-9]+)/{_TEMPLATE_PART}{{2,120}})`")
_TEMPLATE_VAR = re.compile(r"\$\{\s*([^}]*?)\s*\}")

_OPERATION_KIND = r"(query|mutation|subscription)"
_OPERATION_NAME = r"([_A-Za-z][_0-9A-Za-z]*)"
_GAP = r"(?:\s|\\n)*"
_VARIABLE = r"\$[_A-Za-z]"
# A selection set, not an i18n placeholder: `{ cart {`, `{ a b`, `{ ...Fields`, `{ me: viewer`.
_SELECTION = rf"\{{{_GAP}(?:\.\.\.|[_A-Za-z]\w*(?:{_GAP}[({{:@]|(?:\s|\\n)+[_A-Za-z]))"
# `query GetCart($id: ID!) {` in a gql template, or "\nmutation RemoveItem{ cart {" minified.
DOCUMENT_RE = re.compile(
    rf"(?:(?<![\w$])|(?<=\\n)){_OPERATION_KIND}\s+{_OPERATION_NAME}{_GAP}"
    rf"(?:\({_GAP}{_VARIABLE}|{_SELECTION}|@|(\{{{_GAP}[_A-Za-z]\w*{_GAP}\}}))")


def _key(name: str) -> str:
    return rf"""["']?{name}["']?\s*:\s*"""


# A precompiled AST: operation:"mutation",name:{kind:"Name",value:"Checkout"}
COMPILED_RE = re.compile(
    _key("operation") + rf"""["']{_OPERATION_KIND}["']\s*,\s*""" + _key("name") + r"\{\s*"
    + _key("kind") + r"""["']Name["']\s*,\s*""" + _key("value") + rf"""["']{_OPERATION_NAME}["']"""
)

_CALL_METHODS = {".post": "POST", ".put": "PUT", ".patch": "PATCH", ".delete": "DELETE"}

SKIP_PREFIXES = {
    "_next", "__next", "__webpack", "__vite", "static",
    "assets", "public", "favicon", "manifest", "service-worker", "workbox",
}

SKIP_EXTENSIONS = (
    ".js", ".mjs", ".json", ".html", ".xml", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".webp", ".avif", ".css", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".map", ".ts", ".tsx",
    ".jsx", ".vue", ".md", ".txt", ".csv", ".yml", ".yaml", ".wasm", ".webmanifest", ".pdf",
    ".mp3", ".mp4", ".webm", ".zip",
)


def _should_skip(path: str) -> bool:
    if len(path) < 4:
        return True
    segments = path.strip("/").split("/")
    if segments and segments[0] in SKIP_PREFIXES:
        return True
    if path.lower().endswith(SKIP_EXTENSIONS):
        return True
    if all(not seg or (seg.startswith("{") and seg.endswith("}")) for seg in segments):
        return True
    return path.strip("/").count("/") == 0 and len(path.strip("/")) <= 3


def _template_to_path(raw: str) -> str:
    def name(m: re.Match) -> str:
        ident = re.sub(r"[^a-zA-Z0-9_]", "_", m.group(1).split(".")[-1]).strip("_")
        return "{" + (ident or "param") + "}"
    return _TEMPLATE_VAR.sub(name, raw)


def _prefix_re(prefixes: Collection[str]) -> re.Pattern[str] | None:
    if not prefixes:
        return None
    heads = "|".join(re.escape(prefix) for prefix in sorted(prefixes))
    return re.compile(
        rf"""["'`]((?:{heads}){_DOTTED_TEMPLATE_PART}{{1,120}})(?:[?#][^"'`]*)?["'`]""")


def scan_bundles_for_routes(
    bundles: dict[str, str], page_origin: str, prefixes: Collection[str] = (), wide: bool = False,
) -> list[BundleRoute]:
    """WIDE adds dotted legacy paths and template-literal calls, which only coverage reads."""
    found: dict[tuple[str, str], BundleRoute] = {}
    prefixed = _prefix_re(prefixes)

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
        if wide:
            for m in WIDE_CALL_RE.finditer(code):
                add(m.group(2), _CALL_METHODS.get(m.group(1), "GET"), bundle_url, "call")
            for m in CALL_TEMPLATE_RE.finditer(code):
                add(_template_to_path(m.group(2)), _CALL_METHODS.get(m.group(1), "GET"),
                    bundle_url, "call")
        for m in TEMPLATE_RE.finditer(code):
            add(_template_to_path(m.group(1)), "GET", bundle_url, "template")
        for pattern in (API_PATH_RE, VERSIONED_API_RE):
            for m in pattern.finditer(code):
                add(m.group(1), "GET", bundle_url, "string_literal")
        if prefixed:
            for m in prefixed.finditer(code):
                add(_template_to_path(m.group(1)), "GET", bundle_url, "recorded_prefix")

    return list(found.values())


def scan_bundles_for_operations(bundles: dict[str, str]) -> list[BundleOperation]:
    found: dict[tuple[str, str], BundleOperation] = {}
    for bundle_url, code in bundles.items():
        for m in DOCUMENT_RE.finditer(code):
            if m[3] and not m[2][:1].isupper():
                continue
            found.setdefault((m[1], m[2]), BundleOperation(m[1], m[2], bundle_url, "document"))
        for m in COMPILED_RE.finditer(code):
            found.setdefault((m[1], m[2]), BundleOperation(m[1], m[2], bundle_url, "compiled"))
    return list(found.values())
