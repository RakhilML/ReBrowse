"""Scan JS bundles for hardcoded API routes."""

from __future__ import annotations
import re
from urllib.parse import urlparse
from dataclasses import dataclass


@dataclass
class BundleRoute:
    path: str
    url: str
    source_bundle: str
    match_type: str  # "string_literal" | "fetch_call" | "route_def"


# Patterns to find API paths in JS bundles
API_PATH_RE = re.compile(
    r"""["'`](\/api\/[a-zA-Z0-9/_\-]{2,80})(?:[?#][^"'`]*)?["'`]""",
)
FETCH_RE = re.compile(
    r"""(?:fetch|axios|\.get|\.post|\.put|\.patch|\.delete)\s*\(\s*["'`](\/[a-zA-Z0-9/_\-]{3,80})(?:[?#][^"'`]*)?["'`]""",
)
VERSIONED_API_RE = re.compile(
    r"""["'`](\/v[0-9]+\/[a-zA-Z0-9/_\-]{2,80})(?:[?#][^"'`]*)?["'`]""",
)

SKIP_PREFIXES = {
    "_next", "__next", "__webpack", "__vite", "static",
    "assets", "public", "favicon", "manifest", "service-worker", "workbox",
}

SKIP_EXTENSIONS = {
    ".js", ".json", ".html", ".xml", ".svg", ".png", ".jpg",
    ".css", ".woff2", ".ttf", ".map", ".ts", ".tsx", ".jsx", ".vue", ".md",
}


def _should_skip(path: str) -> bool:
    # Too short
    if len(path) < 4:
        return True
    # Framework internal
    segments = path.strip("/").split("/")
    if segments and segments[0] in SKIP_PREFIXES:
        return True
    # File extension
    if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return True
    # Single generic segment like /api
    if path.strip("/").count("/") == 0 and len(path.strip("/")) <= 3:
        return True
    return False


def scan_bundles_for_routes(
    bundles: dict[str, str],
    page_origin: str,
) -> list[BundleRoute]:
    """
    Scan collected JS bundles for hardcoded API paths.
    Returns discovered routes with full URLs.
    """
    seen: set[str] = set()
    routes: list[BundleRoute] = []

    for bundle_url, code in bundles.items():
        for pattern, match_type in [
            (API_PATH_RE, "string_literal"),
            (FETCH_RE, "fetch_call"),
            (VERSIONED_API_RE, "route_def"),
        ]:
            for m in pattern.finditer(code):
                path = m.group(1)
                if _should_skip(path):
                    continue
                if path in seen:
                    continue
                seen.add(path)

                routes.append(BundleRoute(
                    path=path,
                    url=f"{page_origin}{path}",
                    source_bundle=bundle_url,
                    match_type=match_type,
                ))

    return routes
