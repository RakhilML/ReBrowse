"""Two pipelines: build (capture + reverse-engineer + save) and run (search + execute)."""

from __future__ import annotations
import time
import json
from urllib.parse import urlparse
from rebrowse.models import SkillManifest, EndpointDescriptor, ExecutionTrace, _now
from rebrowse.capture.browser import capture_session
from rebrowse.reverse.extractor import extract_endpoints, extract_auth_headers
from rebrowse.reverse.scanner import scan_bundles_for_routes
from rebrowse.store.skills import save_skill, find_by_domain, search_skills
from rebrowse.execution.executor import execute_endpoint
from rebrowse.auth.vault import store_cookies
from rebrowse.llm.client import describe_endpoints, parse_intent, pick_endpoint


async def build(url: str) -> dict:
    """
    BUILDER: Given a URL, capture the site, reverse-engineer all APIs,
    use LLM to describe them, save as a skill. Returns the skill summary.
    """
    t0 = time.time()
    print(f"[build] Capturing {url} ...")

    # 1. Capture
    capture = await capture_session(url)
    print(f"[build] Captured {len(capture.requests)} requests from {capture.domain}")

    if not capture.requests:
        return {"error": f"No traffic captured from {url}", "timing_ms": _elapsed(t0)}

    # 2. Reverse-engineer endpoints
    endpoints = extract_endpoints(capture.requests, page_domain=capture.domain)
    print(f"[build] Extracted {len(endpoints)} API endpoints")

    # 3. Scan JS bundles
    if capture.js_bundles:
        origin = f"{urlparse(capture.final_url).scheme}://{urlparse(capture.final_url).netloc}"
        bundle_routes = scan_bundles_for_routes(capture.js_bundles, origin)
        print(f"[build] Found {len(bundle_routes)} routes in {len(capture.js_bundles)} JS bundles")
        for route in bundle_routes:
            endpoints.append(EndpointDescriptor(
                method="GET",
                url_template=route.url,
                description=f"JS bundle route: {route.path}",
                reliability_score=0.3,
            ))

    if not endpoints:
        return {
            "error": f"No API endpoints found on {capture.domain}",
            "requests_captured": len(capture.requests),
            "timing_ms": _elapsed(t0),
        }

    # 4. LLM describes endpoints
    print(f"[build] Asking LLM to describe {len(endpoints)} endpoints ...")
    try:
        ep_data = [
            {"url_template": ep.url_template, "method": ep.method.value}
            for ep in endpoints[:25]
        ]
        descriptions_raw = await describe_endpoints(json.dumps(ep_data))
        descriptions = json.loads(descriptions_raw)
        if isinstance(descriptions, list):
            for desc in descriptions:
                url_t = desc.get("url_template", "")
                d = desc.get("description", "")
                for ep in endpoints:
                    if ep.url_template == url_t and d:
                        ep.description = d
        described = sum(1 for ep in endpoints if ep.description)
        print(f"[build] LLM described {described}/{len(endpoints)} endpoints")
    except Exception as e:
        print(f"[build] LLM description failed: {e}")

    # 5. Save skill
    skill = SkillManifest(
        name=f"{capture.domain} API",
        domain=capture.domain,
        description=f"Auto-discovered APIs from {capture.domain}",
        intent_signature=url,
        endpoints=endpoints,
    )
    save_skill(skill)
    print(f"[build] Saved skill '{skill.name}' ({skill.skill_id}) with {len(endpoints)} endpoints")

    # 6. Store auth if found
    auth_headers = extract_auth_headers(capture.requests)
    if capture.cookies:
        store_cookies(capture.domain, capture.cookies)

    return {
        "skill_id": skill.skill_id,
        "domain": skill.domain,
        "name": skill.name,
        "endpoints_count": len(endpoints),
        "endpoints": [
            {
                "id": ep.endpoint_id,
                "method": ep.method.value,
                "url": ep.url_template,
                "description": ep.description or "(no description)",
            }
            for ep in endpoints
        ],
        "timing_ms": _elapsed(t0),
    }


async def run(prompt: str) -> dict:
    """
    EXECUTOR: Given a natural language prompt, search stored skills,
    LLM picks the right endpoint + params, execute it, return data.
    """
    t0 = time.time()
    print(f"[run] Parsing intent: {prompt}")

    # 1. Parse intent
    domain = None
    action = prompt
    try:
        intent = await parse_intent(prompt)
        domain = intent.get("domain")
        action = intent.get("action", prompt)
        params = intent.get("params", {})
        print(f"[run] Intent: domain={domain}, action={action}, params={params}")
    except Exception as e:
        print(f"[run] Intent parsing failed: {e}")
        params = {}

    # 2. Search skills
    skill = None

    # Try exact domain match
    if domain:
        skill = find_by_domain(domain)
        if skill:
            print(f"[run] Found skill by domain: {skill.name} ({len(skill.endpoints)} endpoints)")

    # Try semantic search
    if not skill:
        results = search_skills(f"{domain or ''} {action}", limit=3)
        if results:
            skill, score = results[0]
            print(f"[run] Found skill by search: {skill.name} (score={score:.3f})")
        else:
            return {
                "error": "No matching skills found. Run 'build' on the target website first.",
                "hint": f"Try: python -m rebrowse build https://{domain}" if domain else "Try building a skill first.",
                "timing_ms": _elapsed(t0),
            }

    if not skill or not skill.endpoints:
        return {"error": "Skill has no endpoints.", "timing_ms": _elapsed(t0)}

    # 3. LLM picks the best endpoint
    print(f"[run] Asking LLM to pick best endpoint for: {action}")
    ep_list = [
        {
            "endpoint_id": ep.endpoint_id,
            "method": ep.method.value,
            "url_template": ep.url_template,
            "description": ep.description or "",
            "path_params": ep.path_params,
            "query": ep.query,
        }
        for ep in skill.endpoints
    ]

    chosen_ep = None
    extra_params = {}
    extra_query = {}

    try:
        choice = await pick_endpoint(action, ep_list)
        chosen_id = choice.get("endpoint_id", "")
        extra_params = choice.get("params", {})
        extra_query = choice.get("query", {})
        reason = choice.get("reason", "")
        print(f"[run] LLM chose: {chosen_id} — {reason}")

        for ep in skill.endpoints:
            if ep.endpoint_id == chosen_id:
                chosen_ep = ep
                break
    except Exception as e:
        print(f"[run] LLM endpoint selection failed: {e}")

    # Fallback: just pick the first GET endpoint
    if not chosen_ep:
        for ep in skill.endpoints:
            if ep.method.value == "GET":
                chosen_ep = ep
                break
        if not chosen_ep:
            chosen_ep = skill.endpoints[0]
        print(f"[run] Fallback: using {chosen_ep.method.value} {chosen_ep.url_template}")

    # Merge params
    if extra_params:
        chosen_ep.path_params.update(extra_params)
    if extra_query:
        chosen_ep.query.update(extra_query)

    # 4. Execute
    print(f"[run] Executing {chosen_ep.method.value} {chosen_ep.url_template} ...")
    trace = await execute_endpoint(skill, chosen_ep, params=extra_params)

    return {
        "success": trace.success,
        "status_code": trace.status_code,
        "endpoint": f"{chosen_ep.method.value} {chosen_ep.url_template}",
        "description": chosen_ep.description or "",
        "result": trace.result,
        "error": trace.error,
        "timing_ms": _elapsed(t0),
    }


def _elapsed(t0: float) -> float:
    return round((time.time() - t0) * 1000, 1)
