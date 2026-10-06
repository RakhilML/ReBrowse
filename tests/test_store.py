from __future__ import annotations

from rebrowse.models import EndpointDescriptor, SkillManifest
from rebrowse.store import skills as store
from rebrowse.store.skills import (
    delete_skill,
    find_by_domain,
    find_exact_domain,
    list_all_skills,
    resolve_skill,
    save_skill,
    search_skills,
)


def test_long_text_is_embedded_in_windows(monkeypatch):
    seen, inner = [], store._encode

    def spy(texts):
        seen.append(texts)
        return inner(texts)
    monkeypatch.setattr(store, "_encode", spy)
    store._embed(" ".join(f"w{i}" for i in range(300)))
    assert len(seen[0]) == 3


def test_lookup_by_alias_exact_and_substring():
    save_skill(SkillManifest(name="gh", domain="github.com"))
    assert find_by_domain("github").domain == "github.com"
    assert find_by_domain("GITHUB.COM ").domain == "github.com"
    assert find_by_domain("hub").domain == "github.com"
    assert find_by_domain("_ub") is None
    assert find_by_domain("") is None


def test_save_stamps_schema_version():
    skill = SkillManifest(name="a", domain="a.com")
    assert skill.schema_version == 1
    save_skill(skill)
    assert find_exact_domain("a.com").schema_version == store.SKILL_SCHEMA_VERSION


def test_search_ranks_by_similarity():
    save_skill(SkillManifest(name="books", domain="books.example",
                             endpoints=[EndpointDescriptor(url_template="https://books.example/api/books")]))
    save_skill(SkillManifest(name="weather", domain="weather.example",
                             endpoints=[EndpointDescriptor(url_template="https://weather.example/api/forecast")]))
    top, score = search_skills("weather forecast", limit=1)[0]
    assert top.domain == "weather.example" and score > 0.25


def test_delete():
    skill = SkillManifest(name="d", domain="d.com")
    save_skill(skill)
    assert delete_skill(skill.skill_id) is True
    assert delete_skill(skill.skill_id) is False
    assert list_all_skills() == []


def test_resolve_skill_prefers_id_over_domain_match():
    by_domain = SkillManifest(name="a", domain="abc.example")
    by_id = SkillManifest(skill_id="abc", name="b", domain="other.example")
    save_skill(by_domain)
    save_skill(by_id)
    assert resolve_skill("abc").skill_id == "abc"
    assert resolve_skill("abc.example").skill_id == by_domain.skill_id
    assert resolve_skill("nowhere") is None
