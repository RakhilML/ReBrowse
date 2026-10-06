"""Turn a HAR 1.2 export into a capture, dropping credentials as it converts."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rebrowse import config
from rebrowse.models import CaptureResult, RawRequest
from rebrowse.reverse.extractor import is_replay_header, is_sensitive_header, registrable_domain
from rebrowse.safety import REDACTED, is_secret_name, redact

FORM = "application/x-www-form-urlencoded"
_CREDENTIAL_VALUE = re.compile(
    r"(bearer|basic|token|digest|negotiate|ntlm|hmac)\s|eyJ[\w-]+\.[\w-]+\.", re.IGNORECASE)
_JSONP_PARAMS = frozenset({"callback", "jsonp", "cb"})
_CAS_TICKETS = ("ST-", "PT-")


class HarError(ValueError):
    pass


def _entries(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError) as e:
        raise HarError(f"Cannot read {path} as JSON: {e}") from e
    log = data.get("log") if isinstance(data, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        raise HarError(f"{path} is not a HAR file: log.entries is missing or not a list")
    return entries


def _dicts(value: Any) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _secret_pair(name: str, value: str, names: set[str]) -> bool:
    low = name.lower()
    return (is_secret_name(name) or (low == "code" and "state" in names)
            or (low == "ticket" and value.startswith(_CAS_TICKETS)))


def _redact_pairs(text: str) -> str:
    pairs = parse_qsl(text, keep_blank_values=True)
    names = {k.lower() for k, _ in pairs}
    redacted = [(redact(k), REDACTED if _secret_pair(k, v, names) else redact(v)) for k, v in pairs]
    return text if redacted == pairs else urlencode(redacted)


def _clean_segment(segment: str) -> str:
    head, *params = segment.split(";")
    return ";".join([head, *(p for p in params if not is_secret_name(p.partition("=")[0]))])


def _clean_url(url: str) -> str | None:
    url = url.partition("#")[0]
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    netloc = parts.netloc.rpartition("@")[2]
    path = "/".join(_clean_segment(s) for s in parts.path.split("/"))
    query = _redact_pairs(parts.query)
    if (netloc, path, query) == (parts.netloc, parts.path, parts.query):
        return url
    return urlunsplit(parts._replace(netloc=netloc, path=path, query=query))


def _exchanges(entries: list) -> Iterator[tuple[str, dict, dict]]:
    for entry in _dicts(entries):
        request, response = entry.get("request"), entry.get("response")
        if not isinstance(request, dict) or not isinstance(response, dict):
            continue
        url, method, status = request.get("url"), request.get("method"), response.get("status")
        if not (isinstance(url, str) and isinstance(method, str) and isinstance(status, int)):
            continue
        if status > 0 and (clean := _clean_url(url)):
            yield clean, request, response


def _headers(raw: Any, keep: Callable[[str], bool]) -> dict[str, str]:
    out: dict[str, str] = {}
    for header in _dicts(raw):
        name, value = header.get("name"), header.get("value")
        if not (isinstance(name, str) and isinstance(value, str)):
            continue
        if keep(name):
            name = name.lower()
            out[name] = f"{out[name]}, {value}" if name in out else value
    return out


def _replayable(name: str) -> bool:
    return is_replay_header(name) and not is_sensitive_header(name) and not is_secret_name(name)


def _request_headers(request: dict) -> dict[str, str]:
    headers = {k: v for k, v in _headers(request.get("headers"), _replayable).items()
               if not _CREDENTIAL_VALUE.match(v.strip())}
    referer = _clean_url(headers.pop("referer", ""))
    return {**headers, "referer": referer} if referer else headers


def _request_body(post: Any, content_type: str) -> str | None:
    if not isinstance(post, dict):
        return None
    mime = str(post.get("mimeType") or content_type).lower()
    text = post.get("text")
    if not isinstance(text, str) or not text:
        params = [(p["name"], "" if p.get("value") is None else str(p["value"]))
                  for p in _dicts(post.get("params")) if isinstance(p.get("name"), str)]
        text, mime = urlencode(params), mime or FORM
    if not text:
        return None
    try:
        json.loads(text)
    except ValueError:
        return _redact_pairs(text) if FORM in mime else None
    return redact(text)


def _content(response: dict) -> dict:
    content = response.get("content")
    return content if isinstance(content, dict) else {}


def _content_type(response: dict) -> str:
    header = _headers(response.get("headers"), lambda name: name.lower() == "content-type")
    mime = _content(response).get("mimeType")
    return header.get("content-type") or (mime if isinstance(mime, str) else "")


def _text(content: dict) -> str | None:
    text = content.get("text")
    if not isinstance(text, str):
        return None
    if content.get("encoding") != "base64":
        return text
    try:
        return base64.b64decode(text).decode("utf-8", errors="replace")
    except ValueError:
        return None


def _response_body(content: dict, content_type: str) -> str | None:
    low = content_type.lower()
    if "json" not in low and "text" not in low:
        return None
    text = _text(content)
    if text is None or len(text.encode("utf-8", errors="replace")) > config.MAX_BODY_SIZE:
        return None
    return text


def _raw_request(url: str, request: dict, response: dict, content_type: str) -> RawRequest:
    headers = _request_headers(request)
    return RawRequest(
        url=url,
        method=request["method"].upper(),
        request_headers=headers,
        request_body=_request_body(request.get("postData"), headers.get("content-type", "")),
        response_status=response["status"],
        response_headers={"content-type": content_type} if content_type else {},
        response_body=_response_body(_content(response), content_type),
    )


def _is_script(url: str, content_type: str) -> bool:
    parts = urlsplit(url)
    if parts.path.lower().endswith((".js", ".mjs")):
        return True
    names = {k.lower() for k, _ in parse_qsl(parts.query)}
    return "javascript" in content_type.lower() and not names & _JSONP_PARAMS


def _netloc(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _host(netloc: str) -> str:
    return urlsplit(f"//{netloc}").hostname or ""


def _same_site(url: str, domain: str) -> bool:
    return registrable_domain(urlsplit(url).hostname or "") == registrable_domain(_host(domain))


def _wanted(domain: str, netlocs: set[str]) -> str:
    wanted = (urlsplit(domain).netloc if "://" in domain else domain).lower()
    wanted = wanted.removesuffix(":443").removesuffix(":80")
    if wanted in netlocs:
        return wanted
    same_host = [n for n in netlocs if _host(n) == wanted]
    if len(same_host) == 1:
        return same_host[0]
    seen = ", ".join(sorted(netlocs)) or "none"
    raise HarError(f"No requests to {wanted} in the HAR (hosts seen: {seen})")


def _site(requests: list[RawRequest], urls: list[str], domain: str | None) -> tuple[str, str]:
    documents = [
        r.url for r in requests
        if r.method == "GET" and "text/html" in r.response_headers.get("content-type", "").lower()
    ]
    if domain:
        domain = _wanted(domain, {_netloc(u) for u in urls})
    else:
        domain = _netloc((documents or urls or [""])[0])
    on_site = [u for u in urls if _netloc(u) == domain]
    if not on_site:
        raise HarError("No http(s) requests in the HAR")
    return domain, next((u for u in documents if _netloc(u) == domain), on_site[0])


def load_har(path: str | Path, domain: str | None = None) -> CaptureResult:
    requests: list[RawRequest] = []
    scripts: dict[str, str] = {}
    urls: list[str] = []
    for url, request, response in _exchanges(_entries(Path(path))):
        urls.append(url)
        content_type = _content_type(response)
        if _is_script(url, content_type):
            text = _text(_content(response))
            if text and len(text) < config.MAX_BUNDLE_CHARS:
                scripts.setdefault(url, text)
            continue
        try:
            requests.append(_raw_request(url, request, response, content_type))
        except RecursionError:
            continue
    domain, final_url = _site(requests, urls, domain)
    same_site = [(url, text) for url, text in scripts.items() if _same_site(url, domain)]
    bundles = dict(same_site[:config.MAX_JS_BUNDLES])
    return CaptureResult(requests=requests, domain=domain, final_url=final_url, js_bundles=bundles)
