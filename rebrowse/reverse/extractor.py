"""Reverse-engineer API endpoints from captured network traffic."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from rebrowse.models import EndpointDescriptor, HttpMethod, Idempotency, RawRequest, ResponseSchema
from rebrowse.reverse.graphql import graphql_ops
from rebrowse.safety import classify_effect, split_xssi

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

# Identity and transport headers are never replayed.
REPLAY_DROP_HEADERS = frozenset({
    "user-agent", "host", "content-length", "connection", "accept-encoding",
    "transfer-encoding", "keep-alive", "upgrade", "te", "trailer", "cookie",
})
REPLAY_DROP_PREFIXES = ("sec-ch-ua", ":", "proxy-")


def is_replay_header(name: str) -> bool:
    low = name.lower()
    return low not in REPLAY_DROP_HEADERS and not low.startswith(REPLAY_DROP_PREFIXES)

SENSITIVE_QUERY_PARAMS = {
    "api_key", "apikey", "access_token", "auth_token", "secret",
    "password", "session_id", "client_secret", "private_key", "bearer",
}

FRAMEWORK_QUERY_PARAMS = {"_rsc", "_next", "__next", "_t", "_hash", "__cf_chl_tk"}

# UUID / ID patterns
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
NUMERIC_RE = re.compile(r"^\d{2,}$")
HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"\{([^{}/]+)\}")
_SURROGATE = re.compile(r"\\u[dD][89a-fA-F]|[\ud800-\udfff]")


def is_sensitive_header(name: str) -> bool:
    low = name.lower()
    if low in STRIP_HEADERS:
        return True
    if low in SAFE_HEADERS:
        return False
    return bool(SENSITIVE_HEADER_PATTERN.search(low))


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if is_replay_header(k) and not is_sensitive_header(k)}


def _sanitize_query(params: dict[str, list[str]]) -> dict[str, str]:
    out = {}
    for k, vals in params.items():
        low = k.lower()
        if low in SENSITIVE_QUERY_PARAMS or low in FRAMEWORK_QUERY_PARAMS:
            continue
        out[k] = vals[0] if vals else ""
    return out


def looks_like_id(segment: str) -> bool:
    return bool(
        UUID_RE.match(segment) or NUMERIC_RE.match(segment) or HEX_RE.match(segment)
        or ("," in segment and len(segment.split(",")) >= 3)
    )


def normalize_url(raw_url: str) -> tuple[str, dict[str, str]]:
    parsed = urlparse(raw_url)
    path_params: dict[str, str] = {}
    new_segments: list[str] = []
    for seg in parsed.path.strip("/").split("/"):
        if not looks_like_id(seg):
            new_segments.append(seg)
            continue
        prev = new_segments[-1] if new_segments else ""
        base = f"{prev}_id" if prev and not prev.startswith("{") else "id"
        name, n = base, 2
        while name in path_params:
            name, n = f"{base}{n}", n + 1
        path_params[name] = seg
        new_segments.append(f"{{{name}}}")

    template_path = "/" + "/".join(new_segments)
    if parsed.path.endswith("/") and template_path != "/":
        template_path += "/"
    return f"{parsed.scheme}://{parsed.netloc}{template_path}", path_params


def canonical_template(template: str) -> str:
    """Erase placeholder names, so /u/{id} and /u/{users_id} compare equal."""
    return PLACEHOLDER_RE.sub("{}", template)


def endpoint_key(ep: EndpointDescriptor) -> tuple[str, str, str]:
    """The identity extraction dedupes on, recomputed from an endpoint's stored fields."""
    ops = "|".join(op.dedup_token() for op in graphql_ops(ep.body, ep.query))
    return ep.method.value, canonical_template(ep.url_template), ops


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
    if any(h in path for h in ["sso_init", "onboarding", "autologin", "auto_login"]):
        score -= 3.0

    # Bare API root (e.g. /api/v1/ or /api/v2/) with no specific resource
    if re.match(r"^/(?:api/)?v\d+/?$", path):
        score -= 5.0

    # Performance/telemetry in path
    if any(h in path for h in ["clientele", "reports/performance", "storage_report"]):
        score -= 5.0

    return score


# Common multi-part public suffixes (not the full Public Suffix List).
_MULTI_PART_SUFFIXES = frozenset({
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
    "co.jp", "or.jp", "ne.jp", "go.jp", "ac.jp",
    "co.in", "net.in", "org.in", "gen.in", "firm.in", "ind.in",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz",
    "co.za", "org.za", "web.za",
    "com.br", "net.br", "org.br", "gov.br",
    "com.cn", "net.cn", "org.cn", "gov.cn",
    "com.mx", "com.tr", "com.sg", "com.hk", "com.tw", "com.ar", "com.sa", "com.ua",
    "co.kr", "or.kr", "co.id", "co.th", "com.my", "com.ph", "com.vn", "co.il",
})


def registrable_domain(host: str) -> str:
    host = host.lower().strip().split(":")[0].strip(".")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(url: str, page_domain: str) -> bool:
    host = urlparse(url).hostname or ""
    page = page_domain.lower().split(":")[0]
    return bool(host and page) and registrable_domain(host) == registrable_domain(page)


def _host_matches_pattern(host: str, pattern: str) -> bool:
    p = pattern.rstrip(".")
    if not p:
        return False
    return host == p or host.startswith(p + ".") or ("." + p + ".") in ("." + host + ".")


def _path_has_run(path_lower: str, needle: str) -> bool:
    needle_segs = [s for s in needle.lower().split("/") if s]
    if not needle_segs:
        return False
    segs = [s for s in path_lower.split("/") if s]
    n = len(needle_segs)
    return any(segs[i:i + n] == needle_segs for i in range(len(segs) - n + 1))


def is_telemetry_path(path: str) -> bool:
    path_lower = path.lower()
    return any(_path_has_run(path_lower, t) for t in SKIP_TELEMETRY_PATHS)


_FRAMEWORK_INTERNALS = (
    "_next/static", "_next/data", "static/chunks", "static/media",
    "cdn-cgi", "__webpack", "__vite", "/assets/", "/static/", "/_/js/", "/_/ss/",
)


def is_api_request(req: RawRequest) -> bool:
    """Filter out non-API requests."""
    if req.method not in ALLOWED_METHODS or req.response_status == 0:
        return False

    parsed = urlparse(req.url)
    host = (parsed.hostname or "").lower()
    if host in SKIP_HOSTS or any(_host_matches_pattern(host, p) for p in SKIP_HOST_PATTERNS):
        return False

    path_lower = parsed.path.lower()
    if path_lower.endswith(tuple(SKIP_EXTENSIONS)):
        return False
    if any(seg in path_lower for seg in _FRAMEWORK_INTERNALS):
        return False
    if is_telemetry_path(path_lower):
        return False

    return not (
        req.method == "GET" and parsed.path in ("", "/") and not parsed.query
        and "html" in req.response_headers.get("content-type", "")
    )


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {_scrub(k): _scrub(v) for k, v in value.items()}
    return value


def _loads(text: str) -> Any:
    value = json.loads(text)
    return _scrub(value) if _SURROGATE.search(text) else value


def _infer_schema(body: str | None) -> ResponseSchema | None:
    """Try to infer a response schema from a JSON body."""
    if not body:
        return None
    try:
        data = _loads(body)
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


def _scalar_type(val) -> str:
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, int):
        return "integer"
    if isinstance(val, float):
        return "number"
    if isinstance(val, str):
        return "string"
    if val is None:
        return "null"
    return "unknown"


def schema_from_values(values: list, depth: int = 0) -> ResponseSchema:
    values = [v for v in values if v is not None]
    n = len(values)
    if depth > 4 or not values:
        return ResponseSchema(type="unknown", inferred_from_samples=max(n, 1))

    if all(isinstance(v, dict) for v in values):
        keys: list[str] = []
        for v in values:
            for k in v:
                if k not in keys:
                    keys.append(k)
                if len(keys) >= 30:
                    break
        props = {}
        required = []
        for k in keys:
            props[k] = schema_from_values([v[k] for v in values if k in v], depth + 1).model_dump()
            if all(k in v for v in values):
                required.append(k)
        return ResponseSchema(type="object", properties=props, required=required, inferred_from_samples=n)

    if all(isinstance(v, list) for v in values):
        items_vals = [item for v in values for item in v]
        items = schema_from_values(items_vals, depth + 1).model_dump() if items_vals else None
        return ResponseSchema(type="array", items=items, inferred_from_samples=n)

    types = {_scalar_type(v) for v in values}
    return ResponseSchema(type=types.pop() if len(types) == 1 else "unknown", inferred_from_samples=n)


def _merge_schemas(bodies: list[str | None]) -> ResponseSchema | None:
    values = []
    for b in bodies:
        if not b:
            continue
        try:
            values.append(_loads(b))
        except (json.JSONDecodeError, TypeError):
            continue
    if not values:
        return None
    return schema_from_values(values)


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
    api_requests = [r for r in requests if is_api_request(r)]

    # Domain filter
    if page_domain:
        api_requests = [r for r in api_requests if same_site(r.url, page_domain)]

    # Ad filter
    api_requests = [r for r in api_requests if not _lookslike_ad_response(r.response_body)]

    # Score and sort
    scored = [(r, _score_request(r)) for r in api_requests]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Filter low-scoring
    scored = [(r, s) for r, s in scored if s > 0]

    samples: dict[str, list[str | None]] = {}
    for r in api_requests:
        skey = f"{r.method}:{normalize_url(r.url)[0]}"
        samples.setdefault(skey, []).append(r.response_body)

    # Deduplicate by (method, normalized_url)
    seen: set[str] = set()
    endpoints: list[EndpointDescriptor] = []

    for req, score in scored:
        url_template, path_params = normalize_url(req.url)
        method = HttpMethod(req.method) if req.method in HttpMethod.__members__ else HttpMethod.GET

        parsed = urlparse(req.url)
        query = _sanitize_query(parse_qs(parsed.query))
        body = parse_body(req.request_body) if req.request_body else None
        reliability = min(max(score / 10.0, 0.1), 1.0)
        headers = _sanitize_headers(req.request_headers)
        schema = _infer_schema(req.response_body)

        ops = graphql_ops(body, query)
        if ops:
            for op in ops:
                dedup_key = f"{req.method}:{url_template}:{op.dedup_token()}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                op_body = op.body if op.body is not None else {"operationName": op.name}
                eff = classify_effect(method.value, url_template, op_body)
                endpoints.append(_make_endpoint(
                    method, url_template, headers, query, path_params, op_body,
                    reliability, schema, req.url, eff, description=op.label(),
                ))
            continue

        dedup_key = f"{req.method}:{url_template}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        eff = classify_effect(method.value, url_template, body)
        merged_schema = _merge_schemas(samples.get(dedup_key, [req.response_body]))
        endpoints.append(_make_endpoint(
            method, url_template, headers, query, path_params, body,
            reliability, merged_schema, req.url, eff,
        ))

    return endpoints


def _make_endpoint(
    method, url_template, headers, query, path_params, body,
    reliability, schema, trigger_url, effect, description=None,
) -> EndpointDescriptor:
    return EndpointDescriptor(
        method=method,
        url_template=url_template,
        description=description,
        headers_template=headers,
        query=query,
        path_params=path_params,
        body=body,
        idempotency=Idempotency.SAFE if effect.value == "read" else Idempotency.UNSAFE,
        reliability_score=reliability,
        response_schema=schema,
        trigger_url=trigger_url,
        effect=effect,
    )


def parse_body(body: str | None):
    if not body:
        return None
    body = split_xssi(body)[1]
    try:
        return _loads(body)
    except (json.JSONDecodeError, TypeError):
        pass
    # Try form-urlencoded
    try:
        parsed = parse_qs(body)
    except ValueError:
        return None
    return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()} or None
