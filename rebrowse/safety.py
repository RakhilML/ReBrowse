"""Classify whether replaying an endpoint reads or changes something."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any
from urllib.parse import urlsplit


class Effect(str, Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


_DESTRUCTIVE_VERBS = frozenset({
    "delete", "destroy", "remove", "purge", "wipe", "drop", "revoke",
    "cancel", "deactivate", "unsubscribe", "terminate", "ban", "unfollow",
    "unlike", "unblock", "clear", "reset",
})

_WRITE_VERBS = frozenset({
    "create", "update", "edit", "add", "new", "set", "save", "post", "put",
    "patch", "upload", "send", "submit", "register", "signup", "login",
    "signin", "logout", "signout", "follow", "mute", "unmute", "block",
    "restrict", "unrestrict", "vote", "upvote", "downvote", "like", "favorite",
    "subscribe", "mark", "resend", "verify", "confirm", "change", "enable",
    "disable", "activate", "apply", "join", "leave", "invite", "share",
    "report", "flag", "hide", "pin", "unpin", "rename", "move", "toggle",
    "consent", "checkout",
})

_SESSION_END = re.compile(r"(sign|log)[_\-]?out")

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]+")


def _words(text: str) -> set[str]:
    return {t for t in _NON_ALNUM.split(_CAMEL_BOUNDARY.sub(" ", text).lower()) if t}


def graphql_kind_of_query(query: str) -> str | None:
    stripped = query.lstrip()
    low = stripped.lower()
    if low.startswith("mutation"):
        return "mutation"
    if low.startswith(("query", "subscription")) or stripped.startswith("{"):
        return "query"
    return None


def _graphql_kind(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    query = body.get("query")
    if isinstance(query, str):
        return graphql_kind_of_query(query)
    op = body.get("operationName")
    if isinstance(op, str) and op and _words(op) & (_DESTRUCTIVE_VERBS | _WRITE_VERBS):
        return "mutation"
    return None


def _has_destructive_mutation(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    text = " ".join(v for k, v in body.items()
                    if k in ("query", "operationName") and isinstance(v, str))
    return bool(_words(text) & _DESTRUCTIVE_VERBS)


def classify_effect(method: str, url: str, body: Any = None) -> Effect:
    method = (method or "GET").upper()
    path = urlsplit(url).path if "//" in url else url
    tokens = _words(path)
    gql = _graphql_kind(body)

    if method == "DELETE" or tokens & _DESTRUCTIVE_VERBS:
        return Effect.DESTRUCTIVE
    if _SESSION_END.search(path.lower()):
        return Effect.WRITE
    if gql == "mutation" and _has_destructive_mutation(body):
        return Effect.DESTRUCTIVE
    if gql == "query" and not tokens & _WRITE_VERBS:
        return Effect.READ
    if method in ("POST", "PUT", "PATCH") or tokens & _WRITE_VERBS or gql == "mutation":
        return Effect.WRITE
    return Effect.READ
