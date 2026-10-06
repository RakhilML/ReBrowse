from __future__ import annotations

import pytest

from rebrowse.execution.executor import execute_endpoint
from rebrowse.models import EndpointDescriptor, HttpMethod, SkillManifest
from rebrowse.safety import Effect, classify_effect


@pytest.mark.parametrize("method,url,body,expected", [
    ("GET", "https://x.com/api/items", None, Effect.READ),
    ("GET", "https://api.spotify.com/v1/tracks/11dFghVX", None, Effect.READ),
    ("GET", "https://dev.to/api/videos", None, Effect.READ),
    ("POST", "https://x.com/api/comments", None, Effect.WRITE),
    ("PUT", "https://x.com/api/profile", None, Effect.WRITE),
    ("DELETE", "https://x.com/api/comments/1", None, Effect.DESTRUCTIVE),
    ("GET", "https://goodreads.com/comment/destroy", None, Effect.DESTRUCTIVE),
    ("GET", "https://goodreads.com/user/sign_out", None, Effect.WRITE),
    ("GET", "https://goodreads.com/user/update_preferences", None, Effect.WRITE),
    ("GET", "https://amazon.com/cart/add-to-cart/patc-config", None, Effect.WRITE),
    ("POST", "https://r.com/graphql", {"query": "query Popular { posts { id } }"}, Effect.READ),
    ("POST", "https://r.com/graphql", {"query": "mutation Follow { follow(id: 1) }"}, Effect.WRITE),
    ("POST", "https://r.com/graphql", {"query": "mutation { deleteComment(id: 1) { ok } }"}, Effect.DESTRUCTIVE),
])
def test_classify_effect(method, url, body, expected):
    assert classify_effect(method, url, body) == expected


def test_word_boundary_no_false_positive():
    assert classify_effect("GET", "https://x.com/v1/soundtracks", None) == Effect.READ


def _skill(domain="127.0.0.1"):
    return SkillManifest(name="t", domain=domain)


async def test_write_refused_without_confirmation():
    ep = EndpointDescriptor(method=HttpMethod.POST, url_template="http://127.0.0.1:1/api/create")
    trace = await execute_endpoint(_skill(), ep, confirmed=False)
    assert trace.success is False
    assert trace.status_code is None
    assert "confirmation_required" in (trace.error or "")


async def test_read_executes_against_fixture(fixture_site):
    ep = EndpointDescriptor(method=HttpMethod.GET, url_template=f"{fixture_site}/api/items")
    trace = await execute_endpoint(_skill(), ep)
    assert trace.success is True
    assert trace.status_code == 200
    assert isinstance(trace.result, dict) and "items" in trace.result


async def test_write_executes_when_confirmed(fixture_site):
    ep = EndpointDescriptor(method=HttpMethod.POST, url_template=f"{fixture_site}/api/echo")
    trace = await execute_endpoint(_skill(), ep, confirmed=True)
    assert trace.success is True
    assert trace.result["method"] == "POST"
