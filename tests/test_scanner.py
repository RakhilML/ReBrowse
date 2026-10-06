from __future__ import annotations

from rebrowse.reverse.scanner import is_third_party_bundle, scan_bundles_for_routes

BUNDLE = """
fetch("/api/users");
fetch(`/api/users/${user.id}/posts`);
axios.post("/api/comments/delete", body);
client.put('/v2/profile/settings', data);
const LITERAL = "/api/feed/home";
fetch("/api/users");                // duplicate
const ASSET = "/static/logo.png";   // skipped
"""


def _routes():
    return {(r.method, r.path): r for r in scan_bundles_for_routes(
        {"https://site.com/app.js": BUNDLE}, "https://site.com")}


def test_methods_come_from_call_sites():
    routes = _routes()
    assert ("POST", "/api/comments/delete") in routes
    assert ("PUT", "/v2/profile/settings") in routes
    assert ("GET", "/api/comments/delete") not in routes


def test_template_literal_becomes_placeholder():
    routes = _routes()
    assert ("GET", "/api/users/{id}/posts") in routes
    assert routes[("GET", "/api/users/{id}/posts")].url == "https://site.com/api/users/{id}/posts"


def test_literals_and_dedupe():
    routes = _routes()
    assert ("GET", "/api/feed/home") in routes
    assert sum(1 for (_, p) in routes if p == "/api/users") == 1
    assert not any(p.startswith("/static") for (_, p) in routes)


def test_third_party_bundles():
    assert is_third_party_bundle("https://www.youtube.com/s/player/base.js", "dev.to")
    assert is_third_party_bundle("https://cdnjs.cloudflare.com/x.js", "dev.to")
    assert not is_third_party_bundle("https://dev.to/assets/app.js", "dev.to")
    assert not is_third_party_bundle("https://github.githubassets.com/app.js", "github.com")
    assert not is_third_party_bundle("https://www.youtube.com/s/player/base.js", "www.youtube.com")
