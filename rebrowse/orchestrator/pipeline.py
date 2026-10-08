"""build, import-har, run and verify pipelines."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from rebrowse.auth.vault import store_cookies
from rebrowse.capture.browser import capture_session
from rebrowse.capture.har import HarError, load_har
from rebrowse.capture.store import read_recording, save_capture
from rebrowse.execution.executor import execute_endpoint
from rebrowse.llm.client import LLMError, describe_endpoints, parse_intent, pick_endpoint
from rebrowse.models import CaptureResult, EndpointDescriptor, SkillManifest, VerificationStatus
from rebrowse.reverse.extractor import (
    canonical_template,
    endpoint_key,
    extract_endpoints,
    is_telemetry_path,
)
from rebrowse.reverse.scanner import is_third_party_bundle, scan_bundles_for_routes
from rebrowse.safety import Effect, classify_effect
from rebrowse.selection import usable_endpoints
from rebrowse.store.skills import (
    find_by_domain,
    find_exact_domain,
    resolve_skill,
    save_skill,
    search_skills,
)

MIN_SEARCH_SCORE = 0.25
MAX_DESCRIBE = 25


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _elapsed(t0: float) -> float:
    return round((time.time() - t0) * 1000, 1)


def _str_dict(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


def _bundle_endpoints(capture, known: set[tuple[str, str]]) -> list[EndpointDescriptor]:
    parsed = urlparse(capture.final_url)
    bundles = {
        u: c for u, c in capture.js_bundles.items()
        if not is_third_party_bundle(u, capture.domain)
    }
    out = []
    for route in scan_bundles_for_routes(bundles, f"{parsed.scheme}://{parsed.netloc}"):
        key = (route.method, canonical_template(route.url))
        if key in known or is_telemetry_path(route.path):
            continue
        known.add(key)
        out.append(EndpointDescriptor(
            method=route.method,
            url_template=route.url,
            reliability_score=0.3,
            effect=classify_effect(route.method, route.url),
        ))
    return out


def _bundle_description(ep: EndpointDescriptor) -> str:
    return f"JS bundle route: {urlparse(ep.url_template).path}"


def _inherit(ep: EndpointDescriptor, old: EndpointDescriptor) -> None:
    ep.endpoint_id = old.endpoint_id
    if not ep.description and old.description != _bundle_description(old):
        ep.description = old.description
    tested = old.verification_status is not VerificationStatus.UNVERIFIED
    same_source = (old.trigger_url is None) == (ep.trigger_url is None)
    if tested and same_source and old.get_effect() == ep.get_effect():
        ep.verification_status = old.verification_status
        ep.reliability_score = old.reliability_score


def _carry_forward(
    endpoints: list[EndpointDescriptor], previous: list[EndpointDescriptor],
) -> tuple[dict[str, EndpointDescriptor], list[EndpointDescriptor]]:
    unused = [(endpoint_key(old), old) for old in previous]
    matched: dict[str, EndpointDescriptor] = {}
    for ep in endpoints:
        key = endpoint_key(ep)
        match = next((i for i, (old_key, _) in enumerate(unused) if old_key == key), None)
        if match is not None:
            old = unused.pop(match)[1]
            _inherit(ep, old)
            matched[ep.endpoint_id] = old
    return matched, [old for _, old in unused]


async def _describe(endpoints: list[EndpointDescriptor], tag: str) -> int:
    pending = [ep for ep in endpoints if not ep.description][:MAX_DESCRIBE]
    if not pending:
        return 0
    try:
        descriptions = await describe_endpoints(
            [{"url_template": ep.url_template, "method": ep.method.value} for ep in pending]
        )
    except LLMError as e:
        _log(f"[{tag}] LLM description failed: {e}")
        return 0
    by_key = {
        (d.get("method", "").upper(), d.get("url_template", "")): d.get("description", "")
        for d in descriptions if isinstance(d, dict)
    }
    described = 0
    for ep in pending:
        text = by_key.get((ep.method.value, ep.url_template)) or by_key.get(("", ep.url_template))
        if text:
            ep.description = text
            described += 1
    return described


def _endpoint_row(ep: EndpointDescriptor) -> dict:
    return {
        "id": ep.endpoint_id,
        "method": ep.method.value,
        "url": ep.url_template,
        "effect": ep.get_effect().value,
        "description": ep.description or "(no description)",
    }


async def _learn(capture: CaptureResult, intent_signature: str, tag: str) -> dict:
    endpoints = extract_endpoints(capture.requests, page_domain=capture.domain)
    known = {(ep.method.value, canonical_template(ep.url_template)) for ep in endpoints}
    endpoints += _bundle_endpoints(capture, known)
    if not endpoints:
        _log(f"[{tag}] 0 endpoints")
        return {
            "error": f"No API endpoints found on {capture.domain}",
            "requests_captured": len(capture.requests),
        }

    existing = find_exact_domain(capture.domain)
    previous = existing.endpoints if existing else []
    matched, dropped = _carry_forward(endpoints, previous)
    kept = len(matched)
    effects = [
        {"id": ep.endpoint_id, "from": old.get_effect().value, "to": ep.get_effect().value}
        for ep in endpoints
        if (old := matched.get(ep.endpoint_id)) and old.get_effect() != ep.get_effect()
    ]
    changed = f", {len(effects)} changed effect" if effects else ""
    _log(f"[{tag}] {len(endpoints)} endpoints: {len(endpoints) - kept} added, {kept} kept, "
         f"{len(dropped)} dropped{changed}")

    described = await _describe(endpoints, tag)
    for ep in endpoints:
        if not ep.description and ep.trigger_url is None:
            ep.description = _bundle_description(ep)

    skill = SkillManifest(
        name=f"{capture.domain} API",
        domain=capture.domain,
        description=f"Auto-discovered APIs from {capture.domain}",
        intent_signature=intent_signature,
        endpoints=endpoints,
    )
    if existing:
        skill.skill_id = existing.skill_id
        skill.created_at = existing.created_at
    save_skill(skill)
    if capture.cookies:
        store_cookies(capture.domain, capture.cookies)
    _log(f"[{tag}] Saved skill {skill.skill_id} ({'replaced' if existing else 'new'})")

    return {
        "skill_id": skill.skill_id,
        "domain": skill.domain,
        "name": skill.name,
        "replaced": bool(existing),
        "endpoints_count": len(endpoints),
        "changes": {
            "added": len(endpoints) - kept,
            "kept": kept,
            "dropped": len(dropped),
            "described": described,
        },
        "endpoints": [
            {**_endpoint_row(ep), "change": "kept" if ep.endpoint_id in matched else "added"}
            for ep in endpoints
        ],
        "dropped": [_endpoint_row(ep) for ep in dropped],
        "effect_changes": effects,
    }


async def build(url: str, steps: str | None = None) -> dict:
    t0 = time.time()
    _log(f"[build] Capturing {url} ...")
    capture = await capture_session(url, steps=steps)
    _log(f"[build] Captured {len(capture.requests)} requests from {capture.domain}")
    if not capture.requests:
        return {"error": f"No traffic captured from {url}", "timing_ms": _elapsed(t0)}

    try:
        _log(f"[build] Saved capture to {save_capture(capture)}")
    except OSError as e:
        _log(f"[build] Could not save capture: {e}")

    return {**await _learn(capture, url, "build"), "timing_ms": _elapsed(t0)}


def _read_hars(path: Path, domain: str | None) -> CaptureResult:
    """Read the HAR file PATH, or every HAR file under the directory PATH; raises HarError."""
    if not path.is_dir():
        return load_har(path, domain)
    capture, count = read_recording(path, domain)
    _log(f"[import-har] read {count} HAR file{'' if count == 1 else 's'} under {path}")
    return capture


async def import_har(path: Path, domain: str | None = None) -> dict:
    t0 = time.time()
    try:
        capture = _read_hars(path, domain)
    except HarError as e:
        return {"error": str(e), "timing_ms": _elapsed(t0)}
    _log(f"[import-har] read {len(capture.requests)} requests for {capture.domain} from {path}")
    if not capture.requests:
        return {"error": f"No requests to import from {path}", "timing_ms": _elapsed(t0)}
    return {
        **await _learn(capture, capture.final_url, "import-har"),
        "source": str(path.resolve()),
        "timing_ms": _elapsed(t0),
    }


def _find_skill_for(domain: str | None, action: str) -> tuple[SkillManifest | None, dict | None]:
    if domain:
        skill = find_by_domain(domain)
        if skill:
            return skill, None
    results = search_skills(f"{domain or ''} {action}", limit=3)
    best = results[0][1] if results else 0.0
    if results and best >= MIN_SEARCH_SCORE:
        return results[0][0], None
    return None, {
        "error": (
            "No skills stored. Run 'build' on the target website first." if not results
            else f"Best match scored {best:.3f}, below {MIN_SEARCH_SCORE}; refusing to guess."
        ),
        "best_score": round(best, 3),
        "hint": f"python -m rebrowse build https://{domain}" if domain else "Build a skill first.",
    }


async def run(prompt: str, dry_run: bool = False, assume_yes: bool = False) -> dict:
    t0 = time.time()
    domain, action, params = None, prompt, {}
    try:
        intent = await parse_intent(prompt)
        domain = intent.get("domain") or None
        action = intent.get("action") or prompt
        params = _str_dict(intent.get("params"))
    except LLMError as e:
        _log(f"[run] Intent parsing failed: {e}")
    _log(f"[run] domain={domain} action={action} params={params}")

    skill, miss = _find_skill_for(domain, action)
    if miss:
        return {**miss, "timing_ms": _elapsed(t0)}
    if not skill.endpoints:
        return {"error": "Skill has no endpoints.", "timing_ms": _elapsed(t0)}

    ranked = usable_endpoints(skill.endpoints)
    candidates = [
        {
            "endpoint_id": ep.endpoint_id,
            "method": ep.method.value,
            "url_template": ep.url_template,
            "effect": ep.get_effect().value,
            "description": ep.description or "",
            "path_params": sorted(ep.path_params),
            "query_params": sorted(ep.query),
        }
        for ep in ranked
    ]
    intent_text = f"{action} {json.dumps(params)}" if params else action

    chosen, path_params, query = None, {}, {}
    try:
        choice = await pick_endpoint(intent_text, candidates)
        chosen = next((ep for ep in ranked if ep.endpoint_id == choice.get("endpoint_id")), None)
        path_params = _str_dict(choice.get("params"))
        query = _str_dict(choice.get("query"))
    except LLMError as e:
        _log(f"[run] Endpoint selection failed: {e}")
    if chosen is None:
        chosen = next((ep for ep in ranked if ep.method.value == "GET"), ranked[0])
        path_params, query = {}, {}
        _log(f"[run] Fallback: {chosen.method.value} {chosen.url_template}")

    ep = chosen.model_copy(update={
        "path_params": {**chosen.path_params, **path_params},
        "query": {**chosen.query, **query},
    })
    eff = ep.get_effect()
    plan = {
        "skill_id": skill.skill_id,
        "domain": skill.domain,
        "endpoint_id": ep.endpoint_id,
        "method": ep.method.value,
        "url_template": ep.url_template,
        "effect": eff.value,
        "description": ep.description or "",
        "path_params": ep.path_params,
        "query": ep.query,
    }

    if dry_run:
        return {"dry_run": True, "plan": plan, "timing_ms": _elapsed(t0)}
    if eff is not Effect.READ and not assume_yes:
        return {
            "confirmation_required": True,
            "effect": eff.value,
            "plan": plan,
            "hint": "This call changes state on the site. Re-run with --yes to execute it.",
            "timing_ms": _elapsed(t0),
        }

    _log(f"[run] Executing {ep.method.value} {ep.url_template}")
    trace = await execute_endpoint(skill, ep, confirmed=True)
    return {
        "success": trace.success,
        "status_code": trace.status_code,
        "endpoint": f"{ep.method.value} {ep.url_template}",
        "effect": eff.value,
        "description": ep.description or "",
        "result": trace.result,
        "truncated": trace.truncated,
        "error": trace.error,
        "timing_ms": _elapsed(t0),
    }


async def verify(target: str) -> dict:
    t0 = time.time()
    skill = resolve_skill(target)
    if not skill:
        return {"error": f"No skill matching '{target}'.", "timing_ms": _elapsed(t0)}

    results = []
    for ep in skill.endpoints:
        eff = ep.get_effect()
        row = {"endpoint_id": ep.endpoint_id, "method": ep.method.value,
               "url": ep.url_template, "effect": eff.value}
        if eff is not Effect.READ:
            ep.verification_status = VerificationStatus.UNVERIFIED
            results.append({**row, "status": "unverified", "reason": "not a read; skipped"})
            continue

        trace = await execute_endpoint(skill, ep)
        ok = bool(trace.success)
        ep.verification_status = VerificationStatus.VERIFIED if ok else VerificationStatus.FAILED
        ep.reliability_score = round(
            ep.reliability_score + (1.0 - ep.reliability_score) * 0.5 if ok
            else ep.reliability_score * 0.5, 3)
        results.append({**row, "status": "verified" if ok else "failed",
                        "status_code": trace.status_code, "error": trace.error,
                        "reliability": ep.reliability_score})

    save_skill(skill)
    verified = sum(r["status"] == "verified" for r in results)
    failed = sum(r["status"] == "failed" for r in results)
    return {
        "skill_id": skill.skill_id,
        "domain": skill.domain,
        "verified": verified,
        "failed": failed,
        "skipped": len(results) - verified - failed,
        "endpoints": results,
        "timing_ms": _elapsed(t0),
    }
