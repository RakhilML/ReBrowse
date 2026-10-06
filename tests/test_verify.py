from __future__ import annotations

from rebrowse.models import EndpointDescriptor, HttpMethod, SkillManifest, VerificationStatus
from rebrowse.orchestrator import pipeline
from rebrowse.safety import Effect
from rebrowse.store.skills import find_exact_domain, save_skill


def _skill(site: str) -> SkillManifest:
    return SkillManifest(name="fx", domain="127.0.0.1", endpoints=[
        EndpointDescriptor(method=HttpMethod.GET, url_template=f"{site}/api/items", effect=Effect.READ),
        EndpointDescriptor(method=HttpMethod.GET, url_template=f"{site}/api/nope", effect=Effect.READ),
        EndpointDescriptor(method=HttpMethod.GET, url_template=f"{site}/blocked", effect=Effect.READ),
        EndpointDescriptor(method=HttpMethod.POST, url_template=f"{site}/api/echo", effect=Effect.WRITE),
    ])


async def test_verify_marks_reads_and_skips_writes(fixture_site, hits):
    skill = _skill(fixture_site)
    save_skill(skill)

    out = await pipeline.verify(skill.skill_id)

    assert (out["verified"], out["failed"], out["skipped"]) == (1, 2, 1)
    by_path = {r["url"].rsplit("/", 1)[-1]: r for r in out["endpoints"]}
    assert by_path["items"]["status"] == "verified"
    assert by_path["nope"]["status"] == "failed"
    assert by_path["blocked"]["error"].startswith("blocked:")
    assert by_path["echo"]["status"] == "unverified"
    assert hits[("POST", "/api/echo")] == 0


async def test_verify_persists_health(fixture_site):
    skill = _skill(fixture_site)
    save_skill(skill)
    await pipeline.verify("127.0.0.1")

    stored = {e.url_template.rsplit("/", 1)[-1]: e for e in find_exact_domain("127.0.0.1").endpoints}
    assert stored["items"].verification_status == VerificationStatus.VERIFIED
    assert stored["nope"].verification_status == VerificationStatus.FAILED
    assert stored["echo"].verification_status == VerificationStatus.UNVERIFIED
    assert stored["items"].reliability_score > 0.5 > stored["nope"].reliability_score


async def test_verify_unknown_target():
    assert "error" in await pipeline.verify("does-not-exist")
