from __future__ import annotations

import json
import re
import threading
import zlib
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import numpy as np
import pytest

from rebrowse import config
from rebrowse.store import skills as store

HITS: Counter = Counter()

INDEX = b"""<!doctype html><html><head><title>fx</title>
<script src="/static/app.js?v=3"></script>
</head><body><h1>fx</h1>
<input id="q" value="">
<button id="go" onclick="search()">go</button>
<script>
document.cookie = "sid=abc123; path=/";
fetch("/api/items");
fetch("/api/echo");
fetch("/api/notes", {method: "POST", headers: {"content-type": "application/json"},
                     body: JSON.stringify({text: "hi"})});
function search(){ fetch("/api/search?q=" + encodeURIComponent(document.getElementById('q').value)); }
</script></body></html>"""

APP_JS = b"""
function recs(){ return fetch("/api/recommendations"); }
function posts(id){ return fetch(`/api/users/${id}/posts`); }
function remove(c){ return axios.post("/api/comments/delete", c); }
function beacon(){ return fetch("/api/collect"); }
"""

CHALLENGE = b"<html><head><title>Just a moment...</title></head><body>checking</body></html>"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict):
        self._send(code, "application/json", json.dumps(payload).encode())

    def do_GET(self):
        path = urlparse(self.path).path
        HITS[("GET", path)] += 1
        if path == "/":
            return self._send(200, "text/html", INDEX)
        if path == "/static/app.js":
            return self._send(200, "application/javascript", APP_JS)
        if path == "/api/items":
            return self._json(200, {"items": [{"id": i} for i in range(3)], "next": "cur"})
        if path == "/api/echo":
            return self._json(200, {
                "method": "GET",
                "cookie": self.headers.get("cookie"),
                "user_agent": self.headers.get("user-agent"),
                "authorization": self.headers.get("authorization"),
                "query": urlparse(self.path).query,
            })
        if path == "/api/search":
            return self._json(200, {"results": [{"id": 1, "title": "match"}]})
        if path == "/blocked":
            return self._send(403, "text/html", CHALLENGE)
        if path == "/big":
            return self._send(200, "text/html", b"<p>" + b"x" * 50_000 + b"</p>")
        if path == "/api/fail":
            return self._json(503, {"error": "unavailable"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        HITS[("POST", path)] += 1
        raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        if path == "/api/fail":
            return self._json(503, {"error": "unavailable"})
        return self._json(200, {
            "method": "POST",
            "received": raw.decode("utf-8", "replace"),
            "content_type": self.headers.get("content-type"),
        })


@pytest.fixture(scope="session")
def fixture_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()


@pytest.fixture
def hits() -> Counter:
    HITS.clear()
    return HITS


def fake_encode(texts: list[str]) -> np.ndarray:
    out = np.zeros((len(texts), 512), dtype=np.float32)
    for i, text in enumerate(texts):
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            out[i, zlib.crc32(tok.encode()) % 512] += 1.0
        norm = np.linalg.norm(out[i])
        if norm:
            out[i] /= norm
    return out


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    data = tmp_path / "rebrowse-data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "CAPTURES_DIR", data / "captures")
    monkeypatch.setattr(config, "DB_PATH", data / "skills.db")
    monkeypatch.setattr(config, "VAULT_DIR", data / "vault")
    monkeypatch.setattr(config, "HOST_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(store, "_encode", fake_encode)
    config.ensure_dirs()
    return data
