"""Classify what replaying an endpoint does, and which names hold secrets."""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit


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

REDACTED = "<redacted>"
_NAME_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
_SECRET_FRAGMENT = re.compile(
    r"token|secret|passw|passcode|passphrase|pwd|credential|session|sessid|csrf|xsrf|cookie"
    r"|jwt|bearer|signature|apikey|saml|assertion")
_SECRET_WORDS = frozenset({
    "auth", "authn", "authentication", "authorization", "pass", "pw", "otp", "sid", "sig",
})
_BENIGN_KEY_QUALIFIERS = frozenset({
    "sort", "primary", "foreign", "public", "partition", "cache", "idempotency", "row",
    "group", "lookup", "hot", "composite", "natural", "unique", "map", "dedupe",
})
_NOT_KEYS = frozenset({"monkey", "turkey", "donkey", "hockey", "jockey", "whiskey"})
_XSSI_GUARDS = (")]}',", ")]}'", ")]}", "while(1);", "for(;;);", "for (;;);")
_JSONP = re.compile(r"(\s*(?:/\*\*/)?\s*[\w$.]+\s*\(\s*)(.*?)(\s*\)\s*;?\s*)", re.DOTALL)
_CAS_TICKETS = ("ST-", "PT-")
FORM = "application/x-www-form-urlencoded"
_MUTATION_DEFINITION = re.compile(r"(?:^|[\s},])mutation\b")
_INNER_MUTATION = re.compile(r"[\s},]mutation\b")
ACTION_KEYS = frozenset({"action", "cmd", "command", "op", "do", "task", "method", "_method"})
_GRAPHQL_TOKEN = re.compile(
    r'"""(?:\\"""|(?!""")[\s\S])*(?:"""|\Z)|"(?:\\[\s\S]|[^"\\\n\r])*"?|#[^\n\r]*'
    r"|-?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|[_A-Za-z][_0-9A-Za-z]*|\S")
_GRAPHQL_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_GRAPHQL_NUMBER = re.compile(r"-?[0-9]")


def _words(text: str) -> set[str]:
    return {t for t in _NON_ALNUM.split(_CAMEL_BOUNDARY.sub(" ", text).lower()) if t}


def graphql_kind_of_query(query: str) -> str | None:
    stripped = query.lstrip()
    low = stripped.lower()
    if low.startswith("mutation") or _MUTATION_DEFINITION.search(stripped):
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


def _action_words(url: str) -> set[str]:
    query = urlsplit(url).query if "//" in url else ""
    return {word for key, value in parse_qsl(query) if key.lower() in ACTION_KEYS
            for word in _words(value)}


def classify_effect(method: str, url: str, body: Any = None) -> Effect:
    method = (method or "GET").upper()
    path = urlsplit(url).path if "//" in url else url
    tokens = _words(path) | _action_words(url)
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


def _secret_word(word: str, previous: str | None) -> bool:
    stem = word.removesuffix("s")
    if stem.endswith("key") and stem not in _NOT_KEYS:
        return (stem[:-3] or previous) not in _BENIGN_KEY_QUALIFIERS
    return word in _SECRET_WORDS or bool(_SECRET_FRAGMENT.search(word))


def is_secret_name(name: str) -> bool:
    if _SECRET_FRAGMENT.search(name.lower()):
        return True
    words = [w.lower() for w in _NAME_WORD.findall(name)]
    return any(_secret_word(w, words[i - 1] if i else None) for i, w in enumerate(words))


def split_xssi(text: str) -> tuple[str, str]:
    """Split a BOM or an anti-hijacking guard such as )]}' off the front of a JSON response."""
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    body = text[len(bom):]
    for guard in _XSSI_GUARDS:
        if body.startswith(guard):
            rest = body[len(guard):].lstrip()
            return text[:len(text) - len(rest)], rest
    return bom, body


def _redact_json_text(text: str) -> str:
    guard, rest = split_xssi(text)
    try:
        parsed = json.loads(rest)
    except ValueError:
        return text
    if not isinstance(parsed, (dict, list)):
        return text
    redacted = redact(parsed)
    return text if redacted == parsed else guard + json.dumps(redacted, separators=(",", ":"))


def _named_secret(pair: dict) -> bool:
    name = pair.get("name")
    return "value" in pair and isinstance(name, str) and is_secret_name(name)


def _is_literal(token: str) -> bool:
    return token.startswith('"') or bool(_GRAPHQL_NUMBER.match(token))


def _value_end(tokens: list[re.Match], start: int) -> int | None:
    first = tokens[start].group()
    if _is_literal(first):
        return start
    if first not in ("[", "{"):
        return None
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index].group()
        depth += (token in ("[", "{")) - (token in ("]", "}"))
        if depth == 0:
            return index
    return len(tokens) - 1


def _replaced(text: str, spans: list[tuple[int, int]], replacement: str) -> str:
    parts, last = [], 0
    for start, end in spans:
        parts += [text[last:start], replacement]
        last = end
    return "".join(parts) + text[last:]


def redact_document(document: str) -> str:
    """DOCUMENT with the value of each secret-named GraphQL argument or input field redacted."""
    tokens = [m for m in _GRAPHQL_TOKEN.finditer(document) if m.group()[0] not in ",#"]
    spans: list[tuple[int, int]] = []
    index = 0
    while index + 2 < len(tokens):
        name, colon = tokens[index].group(), tokens[index + 1].group()
        secret = colon == ":" and _GRAPHQL_NAME.fullmatch(name) and is_secret_name(name)
        end = _value_end(tokens, index + 2) if secret else None
        if end is None:
            index += 1
            continue
        spans.append((tokens[index + 2].start(), tokens[end].end()))
        index = end + 1
    return _replaced(document, spans, f'"{REDACTED}"') if spans else document


def _blank_token(match: re.Match) -> str:
    token = match.group()
    if token[0] in '"#':
        kept = " ".join(sorted(_words(token) & _DESTRUCTIVE_VERBS))
        kept += " mutation" if _INNER_MUTATION.search(token) else ""
        return f"#{kept}" if token[0] == "#" else f'"{kept}"'
    return "0" if _GRAPHQL_NUMBER.match(token) else token


def blank_document(document: str) -> str:
    """DOCUMENT without literal values, keeping the words classify_effect reads in them."""
    return _GRAPHQL_TOKEN.sub(_blank_token, document)


def _redact_value(name: str, value: Any) -> Any:
    redacted = redact(value)
    return redact_document(redacted) if name == "query" and isinstance(redacted, str) else redacted


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {k: REDACTED if is_secret_name(str(k)) else _redact_value(str(k), v)
               for k, v in value.items()}
        return {**out, "value": REDACTED} if _named_secret(value) else out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _redact_json_text(value)
    return value


def _secret_pair(name: str, value: str, names: set[str]) -> bool:
    low = name.lower()
    return (is_secret_name(name) or (low == "code" and "state" in names)
            or (low == "ticket" and value.startswith(_CAS_TICKETS)))


def redact_pairs(text: str) -> str:
    pairs = parse_qsl(text, keep_blank_values=True)
    names = {k.lower() for k, _ in pairs}
    redacted = [(redact(k), REDACTED if _secret_pair(k, v, names) else _redact_value(k, v))
                for k, v in pairs]
    return text if redacted == pairs else urlencode(redacted)


def redact_body(text: str, content_type: str = "") -> str:
    if FORM in content_type.lower():
        return redact_pairs(text)
    if (wrapped := _JSONP.fullmatch(text)) and wrapped[2].startswith(("{", "[")):
        return wrapped[1] + redact(wrapped[2]) + wrapped[3]
    return redact(text)
