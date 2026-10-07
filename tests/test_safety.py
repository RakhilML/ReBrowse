from __future__ import annotations

import pytest

from rebrowse.execution.executor import execute_endpoint
from rebrowse.models import EndpointDescriptor, HttpMethod, SkillManifest
from rebrowse.safety import REDACTED, Effect, classify_effect, is_secret_name, redact, redact_body


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


@pytest.mark.parametrize("url,body,expected", [
    ("https://x.com/ajax.php?action=delete&id=5", None, Effect.DESTRUCTIVE),
    ("https://x.com/index.php?_method=PUT", None, Effect.WRITE),
    ("https://x.com/ajax.php?action=list&q=delete", None, Effect.READ),
    ("https://x.com/graphql", {"operationName": "Feed",
                               "query": "query Feed { a }\nmutation Save { b }"}, Effect.WRITE),
    ("https://x.com/graphql", {"query": "query { audit { mutationCount } }"}, Effect.READ),
])
def test_query_actions_and_mixed_graphql_documents(url, body, expected):
    method = "POST" if body else "GET"
    assert classify_effect(method, url, body) == expected


def test_word_boundary_no_false_positive():
    assert classify_effect("GET", "https://x.com/v1/soundtracks", None) == Effect.READ


@pytest.mark.parametrize("name,secret", [
    ("password", True), ("access_token", True), ("apiKey", True), ("client_secret", True),
    ("x-auth", True), ("csrfToken", True), ("pw", True), ("new_pw", True),
    ("j_password", True), ("SAMLResponse", True), ("SAMLRequest", True), ("assertion", True),
    ("client_assertion", True), ("Authentication", True), ("X-Authentication", True),
    ("authn", True), ("SAMLart", True),
    ("sort_key", False), ("monkey", False), ("username", False), ("q", False),
    ("operationName", False), ("RelayState", False),
])
def test_is_secret_name(name, secret):
    assert is_secret_name(name) is secret


def test_redact_hides_nested_keys_and_json_encoded_strings():
    value = {"user": "ann", "auth": {"token": "t"}, "rows": [{"apiKey": "k", "n": 1}],
             "variables": '{"password":"p","id":7}'}

    assert redact(value) == {
        "user": "ann", "auth": REDACTED, "rows": [{"apiKey": REDACTED, "n": 1}],
        "variables": f'{{"password":"{REDACTED}","id":7}}'}


def test_redact_hides_values_of_secret_named_pairs():
    pairs = [{"name": "user", "value": "ann"}, {"name": "password", "value": "p"}]
    assert redact(pairs) == [{"name": "user", "value": "ann"},
                             {"name": "password", "value": REDACTED}]


@pytest.mark.parametrize("guard", [
    ")]}'\n", ")]}',\n", ")]}'\r\n", ")]}\n", "while(1);", "for(;;);", "for (;;);",
])
def test_redact_keeps_an_xssi_guard_and_hides_what_follows(guard):
    assert redact(f'{guard}{{"token": "t", "id": 7}}') == f'{guard}{{"token":"{REDACTED}","id":7}}'


def test_redact_returns_unchanged_text_as_is():
    text = '{ "id": 7,  "tags": ["a"] }'
    assert redact(text) is text
    assert redact("not json") == "not json"


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


def test_redact_body_handles_a_bom_jsonp_and_form_responses():
    assert redact('\ufeff{"token": "t"}') == f'\ufeff{{"token":"{REDACTED}"}}'
    assert redact_body('/**/cb({"token": "t"})', "text/javascript") == (
        f'/**/cb({{"token":"{REDACTED}"}})')
    assert redact_body("token=t&n=1", "application/x-www-form-urlencoded") == (
        "token=%3Credacted%3E&n=1")
    assert redact_body("cb(1)") == "cb(1)"
