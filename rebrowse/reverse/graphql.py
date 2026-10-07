"""Split captured GraphQL requests into their operations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from rebrowse.safety import Effect, classify_effect, graphql_kind_of_query, redact_document


@dataclass
class GraphQLOp:
    name: str | None
    kind: str | None
    body: dict | None
    query: str | None = None
    hash: str | None = None

    def dedup_token(self) -> str:
        if self.name:
            return f"gql:{self.name}"
        if self.hash:
            return f"gql#{self.hash[:16]}"
        if self.query:
            document = redact_document(self.query).encode("utf-8")
            return "gqlq:" + hashlib.sha1(document).hexdigest()[:12]
        return "gql:anon"

    def label(self) -> str:
        kind = self.kind or "operation"
        if self.name:
            return f"GraphQL {kind}: {self.name}"
        if self.hash:
            return f"GraphQL persisted {kind} ({self.hash[:8]})"
        return f"GraphQL {kind}"


def _op_from_dict(d: dict) -> GraphQLOp | None:
    name = d.get("operationName")
    name = name if isinstance(name, str) and name else None

    query = d.get("query")
    query = query if isinstance(query, str) and query.strip() else None

    pq = None
    ext = d.get("extensions")
    if isinstance(ext, dict):
        persisted = ext.get("persistedQuery")
        if isinstance(persisted, dict) and isinstance(persisted.get("sha256Hash"), str):
            pq = persisted["sha256Hash"]

    kind = graphql_kind_of_query(query) if query else None

    if not (name or pq or kind):
        return None
    return GraphQLOp(name=name, kind=kind, body=d, query=query, hash=pq)


def graphql_ops(body: Any, query_params: dict | None = None) -> list[GraphQLOp]:
    ops: list[GraphQLOp] = []

    if isinstance(body, list):
        for item in body:
            if isinstance(item, dict):
                op = _op_from_dict(item)
                if op:
                    ops.append(op)
    elif isinstance(body, dict):
        op = _op_from_dict(body)
        if op:
            ops.append(op)

    if ops:
        return ops

    if isinstance(query_params, dict):
        name = query_params.get("operationName")
        name = name if isinstance(name, str) and name else None
        query = query_params.get("query")
        query = query if isinstance(query, str) and query.strip() else None
        kind = graphql_kind_of_query(query) if query else None
        pq = None
        ext = query_params.get("extensions")
        if isinstance(ext, str):
            try:
                persisted = (json.loads(ext).get("persistedQuery") or {})
                if isinstance(persisted.get("sha256Hash"), str):
                    pq = persisted["sha256Hash"]
            except (ValueError, AttributeError, TypeError, RecursionError):
                pq = None
        if name or pq or kind:
            ops.append(GraphQLOp(name=name, kind=kind, body=None, query=query, hash=pq))

    return ops


def request_effect(method: str, url: str, body: Any) -> Effect:
    """The most severe effect of a request's parsed body and of each GraphQL operation in it."""
    ops = graphql_ops(body, dict(parse_qsl(urlsplit(url).query)))
    bodies = [op.body if op.body is not None else {"operationName": op.name, "query": op.query}
              for op in ops]
    effects = {classify_effect(method, url, item) for item in [body, *bodies]}
    return max(effects, key=list(Effect).index)
