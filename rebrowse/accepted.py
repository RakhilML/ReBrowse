"""Breaking API changes a team has reviewed and accepted, kept in a committed JSON file."""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

from rebrowse.capture.har import read_json
from rebrowse.capture.store import write_atomic
from rebrowse.drift import BREAKING

ACCEPTED = "accepted"
ACCEPTABLE = frozenset({"status", "content_type", "body_empty", "field_removed",
                        "field_optional", "field_type"})
ROUTE_KINDS = frozenset({"status", "content_type", "body_empty"})
FAILURES = frozenset({"no_response", "blocked"})
_IDENTITY = ("field", "base", "head")
_KEYS = frozenset({"kind", "route", *_IDENTITY, "reason", "severity"})
_TEXT = ("route", "field", "reason")


class AcceptedError(ValueError):
    pass


def _server_error(statuses: Any) -> bool:
    return isinstance(statuses, list) and any(
        isinstance(status, int) and status >= 500 for status in statuses)


def unsettled(change: dict) -> bool:
    """A failed replay or a new server error: never accepted, and never written to the file."""
    return change.get("kind") in FAILURES or (
        change.get("kind") == "status" and change.get("severity") == BREAKING
        and _server_error(change.get("head")))


def needs_review(change: dict) -> bool:
    """A request that answered and is now refused or redirected: accepted by hand only."""
    if change.get("kind") != "status" or change.get("severity") != BREAKING:
        return False
    base, head = change.get("base") or [], change.get("head") or []
    answered = any(isinstance(s, int) and (200 <= s < 300 or s == 304) for s in base)
    return answered and any(isinstance(s, int) and (s in (401, 403) or 300 <= s < 400)
                            and s != 304 for s in head)


def _problem(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return "not an object"
    if unknown := sorted(entry.keys() - _KEYS):
        return f"unknown key {json.dumps(unknown[0])}"
    kind = entry.get("kind")
    if not isinstance(kind, str) or kind not in ACCEPTABLE:
        return (f"kind must be one of {', '.join(sorted(ACCEPTABLE))}; replay failures and "
                "info changes cannot be accepted")
    if "route" not in entry:
        return "route is missing"
    wrong = [key for key in _TEXT if key in entry and not isinstance(entry[key], str)]
    if wrong:
        return f"{wrong[0]} must be a string"
    if kind in ROUTE_KINDS and "field" in entry:
        return f"field does not apply to {kind}, which is a change to the whole route"
    if kind == "status" and _server_error(entry.get("head")):
        return "a status change to a server error (5xx) is an outage and cannot be accepted"
    return None


def load_accepted(path: Path, missing_ok: bool = False) -> list[dict]:
    """Read and check an accepted-changes file; raises ValueError naming the file and entry."""
    if missing_ok and not path.exists():
        return []
    data = read_json(path)
    if not isinstance(data, list):
        raise AcceptedError(f"{path} must hold a JSON array of accepted changes")
    for index, entry in enumerate(data):
        if problem := _problem(entry):
            raise AcceptedError(f"{path}[{index}]: {problem}")
    return data


def _acceptable(change: dict) -> bool:
    return (change.get("severity") == BREAKING and change.get("kind") in ACCEPTABLE
            and not unsettled(change))


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def matches(entry: dict, change: dict) -> bool:
    return (_acceptable(change) and entry["kind"] == change["kind"]
            and entry["route"] == change["route"]
            and all(key in change and _same(entry[key], change[key])
                    for key in _IDENTITY if key in entry))


def _accept(change: dict, entry: dict) -> dict:
    accepted = {**change, "severity": ACCEPTED}
    if "reason" in entry:
        accepted["reason"] = entry["reason"]
    return accepted


def apply_accepted(
    changes: list[dict], entries: list[dict], uncompared: Collection[str] = (),
) -> tuple[list[dict], list[dict], list[dict]]:
    """Mark accepted changes; split unused entries into stale and uncompared-route ones."""
    used: set[int] = set()
    marked: list[dict] = []
    for change in changes:
        found = [index for index, entry in enumerate(entries) if matches(entry, change)]
        used.update(found)
        marked.append(_accept(change, entries[found[0]]) if found else {**change})
    unused = [entry for index, entry in enumerate(entries) if index not in used]
    return (marked, [entry for entry in unused if entry["route"] not in uncompared],
            [entry for entry in unused if entry["route"] in uncompared])


def _identity(change: dict) -> dict:
    return {key: change[key] for key in ("kind", "route", *_IDENTITY) if key in change}


def update_accepted(
    changes: list[dict], entries: list[dict], uncompared: Collection[str] = (),
) -> list[dict]:
    """Keep entries that still match or sit on uncompared routes; add each unaccepted break."""
    updated = [entry for entry in entries if entry["route"] in uncompared
               or any(matches(entry, change) for change in changes)]
    for change in changes:
        if (_acceptable(change) and not needs_review(change)
                and not any(matches(entry, change) for entry in updated)):
            updated.append(_identity(change))
    return updated


def write_accepted(path: Path, entries: list[dict]) -> None:
    """Write UTF-8 JSON; a lone surrogate (only possible inside a string) becomes its \\u escape."""
    text = json.dumps(entries, indent=2, ensure_ascii=False) + "\n"
    write_atomic(path, text.encode("utf-8", errors="backslashreplace"))
