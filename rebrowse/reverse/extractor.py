"""Reverse-engineer API endpoints from captured network traffic."""

from __future__ import annotations
import re
import json
from urllib.parse import urlparse, parse_qs, urlencode
from rebrowse.models import RawRequest, EndpointDescriptor, HttpMethod, Idempotency, ResponseSchema


# --- Hosts/paths to skip ---

SKIP_HOSTS = {
    "fonts.googleapis.com", "fonts.gstatic.com", "www.gstatic.com",
    "cdn.jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com",
    "www.google-analytics.com", "www.googletagmanager.com",
    "analytics.google.com", "stats.g.doubleclick.net",
    "connect.facebook.net", "www.facebook.com",
    "platform.twitter.com", "cdn.segment.com",
    "js.stripe.com", "m.stripe.com",
    "challenges.cloudflare.com", "static.cloudflareinsights.com",
    "accounts.google.com", "auth0.com",
    "sentry.io", "browser.sentry-cdn.com",
    "plausible.io", "rum.browser-intake-datadoghq.com",
    "buysellads.com", "carbonads.com", "ethicalads.io",
}

# Substrings in hostname that indicate telemetry/ads/tracking
SKIP_HOST_PATTERNS = [
    "error-tracking.", "pixel.", "metrics.", "collector.",
    "telemetry.", "analytics.", "tracking.", "logging.",
    "fls-na.", "fls-eu.",  # Amazon telemetry
    "edge.ads.", "ads.",  # Ad servers
    "w3-reporting.",  # W3C reporting
    "rover.", "backstory.",  # eBay tracking/analytics
    "devicebind.",  # Device binding
    "ct.pinterest",  # Pinterest conversion tracking
    "beacon.", "events.", "crashlytics.",  # More telemetry patterns
]

SKIP_TELEMETRY_PATHS = {
    "/log", "/logging", "/telemetry", "/analytics", "/beacon",
    "/ping", "/metrics", "/collect", "/track", "/event",
    "/uedata", "/rd/uedata",  # Amazon user event data
    "/generate_204",  # Tracking pixels
    "/batch/1/OP",  # Amazon batch telemetry
    "/envelope",  # Sentry-style error reporting
    "/pixel",  # Tracking pixels (Netflix, etc.)
    "/performance", "/reports/performance",  # Performance telemetry
    "/storage_report",  # Storage telemetry (Pinterest)
    "/ct.html",  # Conversion tracking (Pinterest)
    "/health", "/healthcheck", "/heartbeat",  # Health checks
    "/track/track", "/track_click", "/track_event",  # Tracking (Goodreads)
    "/roverimp/", "/rover/",  # eBay ad tracking
    "/gadget_csm",  # eBay widget telemetry
    "/dfp/",  # DoubleClick for Publishers
    "/ajax/bz", "/ajax/qm",  # Instagram telemetry beacons
    "/useracquisition",  # User acquisition tracking
    "/inflowcomponent",  # Inflow tracking
    "/devicebind.",  # Device binding
}

SKIP_EXTENSIONS = {
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".webp", ".avif",
    ".mp3", ".mp4", ".webm", ".ogg", ".wav",  # Media files
    ".wasm",  # WebAssembly
}

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

STRIP_HEADERS = {
    "cookie", "set-cookie", "authorization",
    "x-csrf-token", "x-xsrf-token", "x-api-key",
}

SENSITIVE_HEADER_PATTERN = re.compile(
    r"token|key|secret|credential|password|session", re.IGNORECASE
)

SAFE_HEADERS = {"content-type", "accept", "accept-language", "user-agent", "referer", "origin"}

SENSITIVE_QUERY_PARAMS = {
    "api_key", "apikey", "access_token", "auth_token", "secret",
    "password", "session_id", "client_secret", "private_key", "bearer",
}

FRAMEWORK_QUERY_PARAMS = {"_rsc", "_next", "__next", "_t", "_hash", "__cf_chl_tk"}

# UUID / ID patterns
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
NUMERIC_RE = re.compile(r"^\d{2,}$")
HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.I)


def _is_sensitive_header(name: str) -> bool:
    low = name.lower()
    if low in STRIP_HEADERS:
        return True
    if low in SAFE_HEADERS:
        return False
    return bool(SENSITIVE_HEADER_PATTERN.search(low))


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if not _is_sensitive_header(k)}


def _sanitize_query(params: dict[str, list[str]]) -> dict[str, str]:
    out = {}
    for k, vals in params.items():
        low = k.lower()
        if low in SENSITIVE_QUERY_PARAMS or low in FRAMEWORK_QUERY_PARAMS:
            continue
        out[k] = vals[0] if vals else ""
    return out


def _looks_like_id(segment: str) -> bool:
    if UUID_RE.match(segment):
        return True
    if NUMERIC_RE.match(segment):
        return True
    if HEX_RE.match(segment):
        return True
    # Comma-separated list
    if "," in segment and len(segment.split(",")) >= 3:
        return True
    return False


def _normalize_url(raw_url: str) -> tuple[str, dict[str, str]]:
    """Templatize IDs in URL path, return (template, path_params)."""
    parsed = urlparse(raw_url)
    segments = parsed.path.strip("/").split("/")
    path_params: dict[str, str] = {}

    new_segments = []
    for seg in segments:
        if _looks_like_id(seg):
            param_name = "id"
            # Try to use previous segment as hint
            if new_segments:
                prev = new_segments[-1].strip("{}")
                if not prev.startswith("{"):
                    param_name = f"{prev}_id"
            path_params[param_name] = seg
            new_segments.append(f"{{{param_name}}}")
        else:
            new_segments.append(seg)

    template_path = "/" + "/".join(new_segments) if new_segments else "/"
    base = f"{parsed.scheme}://{parsed.netloc}{template_path}"
    return base, path_params


def _score_request(req: RawRequest) -> float:
    """Score how likely a request is to be a useful API endpoint."""
    score = 0.0
    url_lower = req.url.lower()
    path = urlparse(req.url).path.lower()

    # Method bonus
    if req.method == "GET":
        score += 2.0
    elif req.method == "POST":
        score += 1.0

    # API path hints — strong signal
    if any(hint in path for hint in ["/api/", "/v1/", "/v2/", "/v3/"]):
        score += 5.0

    # GraphQL — very strong signal
    if "graphql" in path or "gql" in path:
        score += 8.0

    # RPC / data hints
    if any(h in url_lower for h in ["search", "feed", "trending", "batchexecute", "query"]):
        score += 3.0

    # JSON response — strong signal
    resp_ct = req.response_headers.get("content-type", "")
    if "json" in resp_ct:
        score += 5.0

    # Has meaningful response body
    if req.response_body and len(req.response_body) > 50:
        score += 2.0

    # Successful status code
    if 200 <= req.response_status < 300:
        score += 1.0

    # --- Penalties ---
    if len(path) > 200:
        score -= 5.0

    if "_rsc=" in url_lower or "text/x-component" in resp_ct:
        score -= 10.0

    # JS bundle penalty
    if any(h in path for h in ["boq-", "og/_/js/", "_/scs/"]):
        score -= 10.0

    # Weblab/experiment/config endpoints are low value
    if any(h in path for h in ["weblab", "remote-config", "flag-config", "manifest.json"]):
        score -= 3.0

    # SSO/auth/onboarding — low value for data retrieval
    if any(h in path for h in ["sso_init", "onboarding", "autoLogin", "auto_login"]):
        score -= 3.0

    # Bare API root (e.g. /api/v1/ or /api/v2/) with no specific resource
    if re.match(r"^/(?:api/)?v\d+/?$", path):
        score -= 5.0

    # Performance/telemetry in path
    if any(h in path for h in ["clientele", "reports/performance", "storage_report"]):
        score -= 5.0

    return score


def _is_api_like(req: RawRequest) -> bool:
    """Filter out non-API requests."""
    if req.method not in ALLOWED_METHODS:
        return False

    parsed = urlparse(req.url)
    host = parsed.netloc.lower()

    # Skip known bad hosts
    if host in SKIP_HOSTS:
        return False

    # Skip hosts matching telemetry/ads patterns
    if any(pat in host for pat in SKIP_HOST_PATTERNS):
        return False

    # Skip static files
    path_lower = parsed.path.lower()
    if any(path_lower.endswith(ext) for ext in SKIP_EXTENSIONS):
        return False

    # Skip framework internals
    if any(seg in path_lower for seg in [
        "_next/static", "_next/data", "static/chunks", "static/media",
        "cdn-cgi", "__webpack", "__vite",
        "/assets/", "/static/", "/_/js/", "/_/ss/",
    ]):
        return False

    # Skip telemetry paths
    for tpath in SKIP_TELEMETRY_PATHS:
        if tpath in path_lower:
            return False

    # Skip bare homepage (GET / with no query) — it's just HTML, not an API
    if req.method == "GET" and parsed.path in ("", "/") and not parsed.query:
        content_type = req.response_headers.get("content-type", "")
        if "html" in content_type:
            return False

    # Must have a successful-ish response
    if req.response_status == 0:
        return False

    return True


def _is_same_domain(req_url: str, page_domain: str) -> bool:
    """Check if request is from the same registrable domain as the page."""
    req_host = urlparse(req_url).netloc.lower()
    page = page_domain.lower()

    if req_host == page:
        return True
    # Allow api.example.com for example.com
    if req_host.endswith(f".{page}") or page.endswith(f".{req_host}"):
        return True
    # Allow subdomains sharing the same base
    req_parts = req_host.rsplit(".", 2)
    page_parts = page.rsplit(".", 2)
    if len(req_parts) >= 2 and len(page_parts) >= 2:
        if req_parts[-2:] == page_parts[-2:]:
            return True

    return False


def _infer_schema(body: str | None) -> ResponseSchema | None:
    """Try to infer a response schema from a JSON body."""
    if not body:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None

    return _schema_from_value(data)


def _schema_from_value(val, depth: int = 0) -> ResponseSchema:
    if depth > 4:
        return ResponseSchema(type="unknown", inferred_from_samples=1)

    if isinstance(val, dict):
        props = {}
        for k, v in list(val.items())[:30]:  # limit fields
            props[k] = _schema_from_value(v, depth + 1).model_dump()
        return ResponseSchema(
            type="object",
            properties=props,
            required=list(props.keys()),
            inferred_from_samples=1,
        )
    elif isinstance(val, list):
        items = None
        if val:
            items = _schema_from_value(val[0], depth + 1).model_dump()
        return ResponseSchema(type="array", items=items, inferred_from_samples=1)
    elif isinstance(val, bool):
        return ResponseSchema(type="boolean", inferred_from_samples=1)
    elif isinstance(val, int):
        return ResponseSchema(type="integer", inferred_from_samples=1)
    elif isinstance(val, float):
        return ResponseSchema(type="number", inferred_from_samples=1)
    elif isinstance(val, str):
        return ResponseSchema(type="string", inferred_from_samples=1)
    else:
        return ResponseSchema(type="null", inferred_from_samples=1)


def _lookslike_ad_response(body: str | None) -> bool:
    if not body:
        return False
    ad_vocab = {"campaignid", "creativeid", "adunitid", "impressionurl", "clickurl", "adid"}
    body_lower = body.lower()
    matches = sum(1 for word in ad_vocab if word in body_lower)
    return matches >= 3


def extract_endpoints(
    requests: list[RawRequest],
    page_domain: str = "",
) -> list[EndpointDescriptor]:
    """
    Main entry: filter, score, deduplicate, and extract API endpoints
    from raw captured requests.
    """
    # Filter
    api_requests = [r for r in requests if _is_api_like(r)]

    # Domain filter
    if page_domain:
        api_requests = [r for r in api_requests if _is_same_domain(r.url, page_domain)]

    # Ad filter
    api_requests = [r for r in api_requests if not _lookslike_ad_response(r.response_body)]

    # Score and sort
    scored = [(r, _score_request(r)) for r in api_requests]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Filter low-scoring
    scored = [(r, s) for r, s in scored if s > 0]

    # Deduplicate by (method, normalized_url)
    seen: set[str] = set()
    endpoints: list[EndpointDescriptor] = []

    for req, score in scored:
        url_template, path_params = _normalize_url(req.url)
        dedup_key = f"{req.method}:{url_template}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        method = HttpMethod(req.method) if req.method in HttpMethod.__members__ else HttpMethod.GET
        idempotency = Idempotency.SAFE if method == HttpMethod.GET else Idempotency.UNSAFE

        parsed = urlparse(req.url)
        query = _sanitize_query(parse_qs(parsed.query))

        endpoints.append(EndpointDescriptor(
            method=method,
            url_template=url_template,
            headers_template=_sanitize_headers(req.request_headers),
            query=query,
            path_params=path_params,
            body=_try_parse_body(req.request_body) if req.request_body else None,
            idempotency=idempotency,
            reliability_score=min(max(score / 10.0, 0.1), 1.0),
            response_schema=_infer_schema(req.response_body),
            trigger_url=req.url,
        ))

    return endpoints


def extract_auth_headers(requests: list[RawRequest]) -> dict[str, str]:
    """Extract auth-related headers from captured requests."""
    auth_headers: dict[str, str] = {}
    for req in requests:
        for k, v in req.request_headers.items():
            low = k.lower()
            if low in ("authorization", "x-csrf-token", "x-xsrf-token", "x-api-key"):
                auth_headers[k] = v
    return auth_headers


def _try_parse_body(body: str | None):
    if not body:
        return None
    # Strip XSSI prefix
    for prefix in [")]}'\n", ")]}\n"]:
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        pass
    # Try form-urlencoded
    try:
        from urllib.parse import parse_qs as pqs
        parsed = pqs(body)
        if parsed:
            return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
    except Exception:
        pass
    return None
