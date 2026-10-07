from __future__ import annotations

import json

from rebrowse.models import RawRequest
from rebrowse.reverse.extractor import extract_endpoints
from rebrowse.reverse.graphql import graphql_ops
from rebrowse.safety import Effect


def test_graphql_ops_single_and_batch():
    single = graphql_ops({"operationName": "X", "query": "query X { a }"})
    assert len(single) == 1 and single[0].name == "X" and single[0].kind == "query"

    batch = graphql_ops([
        {"operationName": "A", "query": "query A { a }"},
        {"operationName": "B", "query": "mutation B { b }"},
    ])
    assert [o.name for o in batch] == ["A", "B"]
    assert [o.kind for o in batch] == ["query", "mutation"]


def test_non_graphql_json_is_not_graphql():
    assert graphql_ops({"query": "cats", "page": 1}) == []
    assert graphql_ops({"foo": "bar"}) == []


def test_an_anonymous_operation_is_identified_without_its_secrets():
    def token(query: str) -> str:
        return graphql_ops({"query": query})[0].dedup_token()

    assert token('{ a(token: "x") { b } }') == token('{ a(token: "<redacted>") { b } }')
    assert token('{ a(id: 1) { b } }') != token('{ a(id: 2) { b } }')


def test_get_persisted_query_from_params():
    ops = graphql_ops(
        {},
        {"operationName": "Feed",
         "extensions": '{"persistedQuery":{"sha256Hash":"deadbeef"}}'},
    )
    assert len(ops) == 1
    assert ops[0].name == "Feed"
    assert ops[0].hash == "deadbeef"


def _gql_req(op: str, query: str, url: str = "https://r.com/graphql") -> RawRequest:
    return RawRequest(
        url=url, method="POST",
        request_body=json.dumps({"operationName": op, "query": query, "variables": {}}),
        response_status=200,
        response_headers={"content-type": "application/json"},
        response_body='{"data": {"ok": true}}' + " " * 60,
    )


def test_extract_splits_operations_on_one_url():
    reqs = [
        _gql_req("PopularPosts", "query PopularPosts { posts { id } }"),
        _gql_req("CurrentUser", "query CurrentUser { me { id } }"),
        _gql_req("FollowUser", "mutation FollowUser { follow(id: 1) }"),
        _gql_req("PopularPosts", "query PopularPosts { posts { id } }"),
    ]
    eps = [e for e in extract_endpoints(reqs, page_domain="r.com")
           if e.url_template.endswith("/graphql")]

    assert len(eps) == 3

    by_name = {}
    for e in eps:
        for name in ("PopularPosts", "CurrentUser", "FollowUser"):
            if name in (e.description or ""):
                by_name[name] = e
    assert set(by_name) == {"PopularPosts", "CurrentUser", "FollowUser"}

    assert by_name["PopularPosts"].get_effect() == Effect.READ
    assert by_name["CurrentUser"].get_effect() == Effect.READ
    assert by_name["FollowUser"].get_effect() == Effect.WRITE

    assert by_name["PopularPosts"].body["operationName"] == "PopularPosts"
    assert by_name["FollowUser"].body["operationName"] == "FollowUser"
