from __future__ import annotations

import pytest

from rebrowse.models import RawRequest
from rebrowse.reverse.extractor import is_api_request, registrable_domain, same_site


def _req(url: str, method: str = "GET") -> RawRequest:
    return RawRequest(
        url=url, method=method, response_status=200,
        response_headers={"content-type": "application/json"},
        response_body='{"ok": true}',
    )


@pytest.mark.parametrize("url", [
    "https://www.threads.net/api/graphql",
    "https://api.example.com/api/login",
    "https://www.eventbrite.com/api/v3/events/search",
    "https://api.example.com/api/healthcare/providers",
    "https://www.landrover.com/api/v1/models",
    "https://api.example.com/api/v1/products",
])
def test_real_apis_survive(url):
    assert is_api_request(_req(url)) is True


@pytest.mark.parametrize("url", [
    "https://ads.doubleclick.net/pagead",
    "https://rover.ebay.com/roverimp/0/0/9",
    "https://api.example.com/v1/collect",
    "https://api.example.com/log",
    "https://api.example.com/api/health",
])
def test_telemetry_is_dropped(url):
    assert is_api_request(_req(url)) is False


@pytest.mark.parametrize("req_url,page,expected", [
    ("https://api.github.com/x", "github.com", True),
    ("https://api.amazon.co.uk/x", "www.amazon.co.uk", True),
    ("https://api-partner.spotify.com/x", "open.spotify.com", True),
    ("https://evil-tracker.co.uk/x", "www.amazon.co.uk", False),
    ("https://example.org/x", "github.com", False),
])
def test_same_domain(req_url, page, expected):
    assert same_site(req_url, page) is expected


def test_registrable_domain():
    assert registrable_domain("api.github.com") == "github.com"
    assert registrable_domain("www.amazon.co.uk") == "amazon.co.uk"
    assert registrable_domain("x.com") == "x.com"


@pytest.mark.parametrize("url,template,params", [
    ("https://x.com/users/12345/repos", "https://x.com/users/{users_id}/repos", {"users_id": "12345"}),
    ("https://x.com/items/123/456", "https://x.com/items/{items_id}/{id}", {"items_id": "123", "id": "456"}),
    ("https://x.com/0123456789abcdef01/0123456789abcdef02/fp", "https://x.com/{id}/{id2}/fp",
     {"id": "0123456789abcdef01", "id2": "0123456789abcdef02"}),
    ("https://x.com/v1/users/", "https://x.com/v1/users/", {}),
    ("https://x.com/", "https://x.com/", {}),
])
def test_normalize_url(url, template, params):
    from rebrowse.reverse.extractor import normalize_url
    assert normalize_url(url) == (template, params)


@pytest.mark.parametrize("guard", [")]}'\n", ")]}',\n", "for (;;);"])
def test_parse_body_strips_xssi_guards(guard):
    from rebrowse.reverse.extractor import parse_body
    assert parse_body(f'{guard}{{"id": 7}}') == {"id": 7}


def test_ip_hosts_compare_whole():
    assert registrable_domain("127.0.0.1") == "127.0.0.1"
    assert not same_site("http://10.0.0.1/x", "127.0.0.1")
    assert same_site("http://127.0.0.1:8080/x", "127.0.0.1:9000")


def test_stored_headers_drop_secrets_and_transport():
    from rebrowse.reverse.extractor import _sanitize_headers
    kept = _sanitize_headers({
        "cookie": "sid=1", "authorization": "Bearer x", "x-csrf-token": "t",
        "content-length": "10", "host": "x.com", "accept-encoding": "br",
        "accept": "application/json", "x-client-version": "7",
    })
    assert kept == {"accept": "application/json", "x-client-version": "7"}
