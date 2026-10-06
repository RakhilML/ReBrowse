from __future__ import annotations

import pytest

from rebrowse import config
from rebrowse.llm import client


@pytest.fixture(autouse=True)
def no_dspy(monkeypatch):
    monkeypatch.setitem(client._dspy_state, "tried", True)
    monkeypatch.setitem(client._dspy_state, "intent", None)
    monkeypatch.setitem(client._dspy_state, "picker", None)


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Sure, here it is: [{"x": 2}] hope that helps', [{"x": 2}]),
])
def test_loads_json(raw, expected):
    assert client.loads_json(raw) == expected


def test_refuses_api_key_over_plain_http(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-test")
    with pytest.raises(client.LLMConfigError):
        client._check_transport("http://203.0.113.7:1234/v1/chat/completions")
    client._check_transport("http://localhost:1234/v1/chat/completions")
    client._check_transport("https://api.openai.com/v1/chat/completions")


def test_warns_once_about_plain_http(monkeypatch, capsys):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(client, "_warned_insecure", False)
    client._check_transport("http://203.0.113.7/v1/x")
    client._check_transport("http://203.0.113.7/v1/x")
    assert capsys.readouterr().err.count("unencrypted") == 1


def test_base_url_resolution(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", client.DEFAULT_BASE_URL)
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    assert client._base_url().startswith("https://generativelanguage.googleapis.com")
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://box:9000/v1/")
    assert client._base_url() == "http://box:9000/v1"


async def test_parse_intent_strips_reasoning(monkeypatch):
    async def fake_post(path, body):
        return {"choices": [{"message": {"content": '<think>hmm</think>\n{"domain": "github.com"}'}}]}
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(config, "LLM_PROVIDER", "local")
    assert await client.parse_intent("trending on github") == {"domain": "github.com"}


async def test_pick_endpoint_marks_endpoints_untrusted(monkeypatch):
    seen = {}

    async def fake_post(path, body):
        seen.update(body)
        return {"choices": [{"message": {"content": '{"endpoint_id": "e1"}'}}]}
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(config, "LLM_PROVIDER", "local")
    out = await client.pick_endpoint("list", [{"endpoint_id": "e1", "url_template": "/x"}])
    assert out == {"endpoint_id": "e1"}
    system = seen["messages"][0]["content"]
    assert "untrusted" in system and "<endpoints>" in seen["messages"][1]["content"]


async def test_claude_request_shape(monkeypatch):
    seen = {}

    async def fake_post(path, body):
        seen.update(path=path, body=body)
        return {"content": [{"text": "ok"}]}
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(config, "LLM_PROVIDER", "claude")
    assert await client.chat("hi", system="sys") == "ok"
    assert seen["path"] == "/messages"
    assert seen["body"]["system"] == "sys"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]


async def test_describe_endpoints_returns_list(monkeypatch):
    async def fake_chat(**kwargs):
        return '[{"url_template": "/a", "method": "GET", "description": "A"}]'
    monkeypatch.setattr(client, "chat", fake_chat)
    assert (await client.describe_endpoints([{"url_template": "/a", "method": "GET"}]))[0][
        "description"] == "A"
