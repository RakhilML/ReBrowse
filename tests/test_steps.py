from __future__ import annotations

import pytest

from rebrowse.capture.steps import Step, parse_steps


def test_parse_all_actions():
    steps = parse_steps("type #q=cats and dogs; click #go; press Enter; wait 800; scroll")
    assert steps == [
        Step("type", selector="#q", text="cats and dogs"),
        Step("click", selector="#go"),
        Step("press", key="Enter"),
        Step("wait", ms=800),
        Step("scroll"),
    ]


def test_parse_empty_and_whitespace():
    assert parse_steps("") == []
    assert parse_steps(None) == []
    assert parse_steps("  ;  ; ") == []


def test_parse_passthrough_list():
    existing = [Step("scroll")]
    assert parse_steps(existing) is existing


@pytest.mark.parametrize("bad", ["click", "type #q", "press", "wait soon", "frobnicate #x"])
def test_parse_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_steps(bad)
