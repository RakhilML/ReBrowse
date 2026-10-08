"""Find the API calls a frontend's own JS can make that a recording never exercised."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from rebrowse.drift import answers
from rebrowse.mock import Route, build_routes
from rebrowse.models import CaptureResult
from rebrowse.reverse.extractor import canonical_template, is_telemetry_path, looks_like_id
from rebrowse.reverse.scanner import (
    BundleOperation,
    BundleRoute,
    is_third_party_bundle,
    scan_bundles_for_operations,
    scan_bundles_for_routes,
)
from rebrowse.safety import Effect, classify_effect

ANY = "{}"
_BASE_URL = re.compile(r"""baseURL\s*:\s*["'`](/[^"'`]{1,120})["'`]""")


@dataclass
class _Reference:
    head: dict[str, str]
    effect: Effect
    found_by: str
    bundle: str
    hits: list[int]


def _first_party(capture: CaptureResult) -> dict[str, str]:
    return {url: code for url, code in capture.js_bundles.items()
            if not is_third_party_bundle(url, capture.domain)}


def _statuses(route: Route) -> set[int]:
    return {rec.status for rec in route.recordings}


def _answer_types(route: Route) -> list[str]:
    return [rec.content_type.lower() for rec in route.recordings if 200 <= rec.status < 300]


def _recorded_prefixes(routes: list[Route]) -> set[str]:
    """'/first/' of every route answered with JSON whose literal first segment has more after it."""
    prefixes = set()
    for route in routes:
        first, *rest = route.template.strip("/").split("/")
        if rest and first not in ("", ANY) and any("json" in t for t in _answer_types(route)):
            prefixes.add(f"/{first}/")
    return prefixes


def _segment_fits(part: str, seg: str) -> bool:
    if part == ANY:
        return seg != ""
    return part == seg or (seg == ANY and looks_like_id(part))


def _fits(reference: list[str], recorded: list[str]) -> bool:
    return all(_segment_fits(part, seg) for part, seg in zip(reference, recorded))


def _path_matches(reference: str, template: str) -> bool:
    """REFERENCE ends TEMPLATE, or, ending in '/', is a run of it with more segments after."""
    ref, rec = canonical_template(reference).split("/")[1:], template.split("/")[1:]
    if ref[-1] != "" and len(rec) > 1 and rec[-1] == "":
        rec = rec[:-1]
    if ref[-1] == "":
        ref = ref[:-1]
        return any(_fits(ref, rec[start:start + len(ref)]) for start in range(len(rec) - len(ref)))
    return len(ref) <= len(rec) and _fits(ref, rec[len(rec) - len(ref):])


def _route_refs(capture: CaptureResult, bundles: dict[str, str],
                routes: list[Route]) -> list[BundleRoute]:
    page = urlsplit(capture.final_url)
    found = scan_bundles_for_routes(bundles, f"{page.scheme}://{page.netloc}",
                                    _recorded_prefixes(routes), wide=True)
    base_urls = {m[1].rstrip("/") for code in bundles.values() for m in _BASE_URL.finditer(code)}
    unique: dict[tuple[str, str], BundleRoute] = {}
    for ref in found:
        if ref.path.rstrip("/") in base_urls and ref.match_type == "string_literal":
            continue
        if not is_telemetry_path(ref.path):
            unique.setdefault((ref.method, canonical_template(ref.path)), ref)
    return sorted(unique.values(), key=lambda ref: (ref.path, ref.method))


def _route_reference(ref: BundleRoute, routes: list[Route]) -> _Reference:
    hits = [index for index, route in enumerate(routes)
            if ref.method in ("GET", route.method) and _path_matches(ref.path, route.template)]
    return _Reference({"method": ref.method, "path": ref.path},
                      classify_effect(ref.method, ref.path), ref.match_type, ref.source_bundle,
                      hits)


def _operation_reference(op: BundleOperation, routes: list[Route]) -> _Reference:
    token = f"gql:{op.name}"
    hits = [index for index, route in enumerate(routes) if token in route.operations]
    document = f"{op.kind} {op.name}"
    effect = classify_effect("POST", "", {"operationName": op.name, "query": document})
    return _Reference({"graphql": document}, effect, op.match_type, op.source_bundle, hits)


def _bundle_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))


def _row(ref: _Reference, routes: list[Route]) -> tuple[bool, dict]:
    matched = [routes[index] for index in ref.hits]
    recorded_as = sorted(route.name for route in matched if answers(_statuses(route)))
    if recorded_as:
        return True, {**ref.head, "found_by": ref.found_by, "recorded_as": recorded_as}
    row = {**ref.head, "effect": ref.effect.value, "found_by": ref.found_by,
           "bundle": _bundle_url(ref.bundle)}
    if statuses := sorted({status for route in matched for status in _statuses(route)}):
        row["statuses"] = statuses
    return False, row


def _unreferenced(routes: list[Route], references: list[_Reference]) -> int:
    seen = {index for ref in references for index in ref.hits}
    return sum(any("html" not in t for t in _answer_types(route))
               for index, route in enumerate(routes) if index not in seen)


def _no_bundles(capture: CaptureResult) -> str:
    if capture.js_bundles:
        return (f"The only JS bundles for {capture.domain} in this recording are ad, embed or "
                "analytics scripts, which coverage does not scan.")
    return (f"No JS bundles for {capture.domain} in this recording. A baseline keeps no JS and a "
            "HAR keeps it only when saved with content, and only scripts served by the site "
            "itself, not from a CDN on another domain: pass the HAR or capture the baseline was "
            "written from, or a DevTools \"Save all as HAR with content\" export.")


def coverage_report(capture: CaptureResult) -> dict:
    """Check the API calls in CAPTURE's own JS against its recorded calls; raises ValueError."""
    bundles = _first_party(capture)
    if not bundles:
        raise ValueError(_no_bundles(capture))
    routes = build_routes(capture, redirects=True)
    operations = sorted(scan_bundles_for_operations(bundles), key=lambda op: (op.name, op.kind))
    references = [_route_reference(ref, routes) for ref in _route_refs(capture, bundles, routes)]
    references += [_operation_reference(op, routes) for op in operations]
    if not references:
        raise ValueError(f"The JS bundles of {capture.domain} reference no API route or GraphQL "
                         "operation that rebrowse can find.")
    rows = [_row(ref, routes) for ref in references]
    covered = [row for found, row in rows if found]
    return {
        "domain": capture.domain,
        "bundles": len(bundles),
        "referenced": len(references),
        "coverage": round(100 * len(covered) / len(references), 1),
        "unreferenced": _unreferenced(routes, references),
        "unrecorded": [row for found, row in rows if not found],
        "covered": covered,
    }
