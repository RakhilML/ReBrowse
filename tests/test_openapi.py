from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlencode

from jsonschema import Draft202012Validator

from rebrowse.models import (
    EndpointDescriptor,
    HttpMethod,
    RawRequest,
    ResponseSchema,
    SkillManifest,
    VerificationStatus,
)
from rebrowse.openapi import REDACTED, skill_to_openapi
from rebrowse.reverse.extractor import extract_endpoints

METHODS = {"get", "post", "put", "patch", "delete"}


def _schemas(op: dict) -> list[dict]:
    found = [p["schema"] for p in op.get("parameters", [])]
    for section in (op.get("requestBody", {}), *op["responses"].values()):
        found += [m["schema"] for m in section.get("content", {}).values() if "schema" in m]
    return found


def assert_valid(spec: dict) -> None:
    assert spec["openapi"] == "3.1.0" and spec["servers"]
    ids = []
    for path, methods in spec["paths"].items():
        assert set(methods) <= METHODS
        placeholders = set(re.findall(r"\{([^}]+)\}", path))
        for op in methods.values():
            assert "responses" in op
            ids.append(op["operationId"])
            path_params = [p for p in op.get("parameters", []) if p["in"] == "path"]
            assert {p["name"] for p in path_params} == placeholders
            assert all(p["required"] is True for p in path_params)
            for schema in _schemas(op):
                Draft202012Validator.check_schema(schema)
    assert len(ids) == len(set(ids))
    shapes = [re.sub(r"\{[^}]+\}", "{}", path) for path in spec["paths"]]
    assert len(shapes) == len(set(shapes)), "paths differ only in placeholder names"


def _get(url: str, body: dict) -> RawRequest:
    return RawRequest(
        url=url, method="GET", response_status=200,
        response_headers={"content-type": "application/json"}, response_body=json.dumps(body),
    )


def _post(url: str, payload: dict | str, content_type: str = "application/json") -> RawRequest:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return RawRequest(
        url=url, method="POST", request_headers={"content-type": content_type},
        request_body=body, response_status=200,
        response_headers={"content-type": "application/json"}, response_body='{"ok": true}',
    )


def _skill(endpoints: list[EndpointDescriptor]) -> SkillManifest:
    return SkillManifest(skill_id="skill1", name="ex API", domain="ex.com", endpoints=endpoints)


def _export(endpoints: list[EndpointDescriptor]) -> dict:
    spec = skill_to_openapi(_skill(endpoints))
    assert_valid(spec)
    return spec


def _export_traffic(requests: list[RawRequest]) -> dict:
    return _export(extract_endpoints(requests, page_domain="ex.com"))


def test_merged_samples_give_path_params_and_optional_fields():
    spec = _export_traffic([
        _get("https://ex.com/api/users/101", {"id": 101, "name": "a", "bio": "hi"}),
        _get("https://ex.com/api/users/102", {"id": 102, "name": "b"}),
    ])

    assert spec["servers"] == [{"url": "https://ex.com"}]
    op = spec["paths"]["/api/users/{users_id}"]["get"]
    assert op["operationId"] == "getApiUsersUsersId"
    [param] = op["parameters"]
    assert {k: param[k] for k in ("name", "in", "required")} == {
        "name": "users_id", "in": "path", "required": True}
    assert param["example"] in {"101", "102"}
    schema = op["responses"]["default"]["content"]["application/json"]["schema"]
    assert "bio" in schema["properties"] and schema["required"] == ["id", "name"]
    assert (op["x-rebrowse-effect"], op["x-rebrowse-observed"]) == ("read", True)
    assert "inferred_from_samples" not in json.dumps(spec)


def test_bundle_route_declares_placeholders_without_claiming_a_response():
    spec = _export([EndpointDescriptor(url_template="http://h/api/users/{id}/posts")])

    op = spec["paths"]["/api/users/{id}/posts"]["get"]
    assert op["parameters"] == [
        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]
    assert op["x-rebrowse-observed"] is False
    assert op["responses"]["default"]["description"].startswith("Not observed")
    assert "content" not in op["responses"]["default"]


def test_paths_differing_only_in_placeholder_names_merge_under_the_observed_name():
    observed = extract_endpoints(
        [_get("https://ex.com/api/users/101/posts", {"posts": []})], page_domain="ex.com")
    spec = _export(observed + [
        EndpointDescriptor(url_template="https://ex.com/api/users/{id}/posts"),
        EndpointDescriptor(method=HttpMethod.POST, url_template="https://ex.com/api/users/{uid}/posts",
                           path_params={"uid": "7"}, body={"text": "hi"}),
    ])

    assert list(spec["paths"]) == ["/api/users/{users_id}/posts"]
    methods = spec["paths"]["/api/users/{users_id}/posts"]
    get, post = methods["get"], methods["post"]
    assert (get["summary"], get["x-rebrowse-observed"]) == ("2 operations", True)
    assert [(p["name"], p["example"]) for p in get["parameters"]] == [("users_id", "101")]
    assert [(p["name"], p["example"]) for p in post["parameters"]] == [("users_id", "7")]

    bundle_only = _export([EndpointDescriptor(url_template="https://a.com/u/{userId}/p"),
                           EndpointDescriptor(url_template="https://a.com/u/{id}/p")])
    assert list(bundle_only["paths"]) == ["/u/{id}/p"]


def test_graphql_operations_merge_into_one_post():
    spec = _export_traffic([
        _post("https://ex.com/api/graphql",
              {"operationName": "GetUser", "query": "query GetUser { me { id } }"}),
        _post("https://ex.com/api/graphql",
              {"operationName": "AddNote", "query": 'mutation AddNote { add(text: "x") { id } }'}),
    ])

    assert list(spec["paths"]["/api/graphql"]) == ["post"]
    op = spec["paths"]["/api/graphql"]["post"]
    assert op["summary"] == "2 operations"
    assert op["x-rebrowse-effect"] == "write"
    assert [(o["summary"], o["effect"]) for o in op["x-rebrowse-operations"]] == [
        ("GraphQL mutation: AddNote", "write"), ("GraphQL query: GetUser", "read")]
    media = op["requestBody"]["content"]["application/json"]
    assert set(media["examples"]) == {"GraphQL mutation: AddNote", "GraphQL query: GetUser"}
    assert media["schema"]["required"] == ["operationName", "query"]


def test_secrets_are_redacted_and_trigger_urls_dropped():
    search = _get("https://ex.com/api/search?session_token=sess-xyz&q=cats", {"results": []})
    spec = _export_traffic([
        _post("https://ex.com/api/login",
              {"username": "a", "password": "hunter2", "meta": {"api_token": "tok-xyz"}}),
        search,
    ])
    text = json.dumps(spec)

    for secret in ("hunter2", "tok-xyz", "sess-xyz", search.url):
        assert secret not in text
    login = spec["paths"]["/api/login"]["post"]["requestBody"]["content"]["application/json"]
    assert login["schema"]["properties"]["password"] == {"type": "string"}
    assert login["example"] == {
        "username": "a", "password": REDACTED, "meta": {"api_token": REDACTED}}
    params = spec["paths"]["/api/search"]["get"]["parameters"]
    assert {p["name"]: p["example"] for p in params} == {"q": "cats", "session_token": REDACTED}


def test_header_params_skip_browser_headers_and_strip_media_params():
    ep = EndpointDescriptor(
        method=HttpMethod.POST, url_template="https://ex.com/api/notes", body={"text": "hi"},
        headers_template={"content-type": "application/json", "accept": "*/*",
                          "sec-fetch-mode": "cors", "X-Requested-With": "XMLHttpRequest"},
    )
    op = _export([ep])["paths"]["/api/notes"]["post"]

    assert op["parameters"] == [{"name": "x-requested-with", "in": "header", "required": False,
                                 "schema": {"type": "string"}, "example": "XMLHttpRequest"}]
    assert list(op["requestBody"]["content"]) == ["application/json"]

    form = ep.model_copy(update={"headers_template": {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}})
    content = _export([form])["paths"]["/api/notes"]["post"]["requestBody"]["content"]
    assert list(content) == ["application/x-www-form-urlencoded"]


def test_credential_headers_are_never_emitted():
    ep = EndpointDescriptor(url_template="https://ex.com/api/me", headers_template={
        "Authorization": "Bearer sekrit-1", "Cookie": "sid=sekrit-2", "X-Session-Id": "sekrit-3",
    })
    spec = _export([ep])

    assert spec["paths"]["/api/me"]["get"]["parameters"] == [
        {"name": "x-session-id", "in": "header", "required": False,
         "schema": {"type": "string"}, "example": REDACTED}]
    assert "sekrit" not in json.dumps(spec)


def test_secondary_origins_get_operation_servers():
    spec = _export([
        EndpointDescriptor(url_template="https://app.ex.com/api/a"),
        EndpointDescriptor(url_template="https://app.ex.com/api/b"),
        EndpointDescriptor(url_template="https://api.ex.com/api/c"),
    ])

    assert spec["servers"] == [{"url": "https://app.ex.com"}]
    assert "servers" not in spec["paths"]["/api/a"]["get"]
    assert spec["paths"]["/api/c"]["get"]["servers"] == [{"url": "https://api.ex.com"}]


def test_export_is_deterministic_and_hides_endpoint_ids():
    endpoints = extract_endpoints([
        _post("https://ex.com/api/graphql", {"operationName": "A", "query": "query A { a }"}),
        _post("https://ex.com/api/graphql", {"operationName": "B", "query": "mutation B { b }"}),
    ], page_domain="ex.com") + [
        EndpointDescriptor(url_template="https://ex.com/a-b", description="dash"),
        EndpointDescriptor(url_template="https://ex.com/a_b", description="underscore"),
        EndpointDescriptor(url_template="https://api.ex.com/a_b", description="elsewhere"),
    ]
    skill = _skill(endpoints)
    reversed_skill = skill.model_copy(update={"endpoints": endpoints[::-1]})

    outputs = {json.dumps(skill_to_openapi(s)) for s in (skill, skill, reversed_skill)}

    assert len(outputs) == 1
    spec = skill_to_openapi(skill)
    assert_valid(spec)
    assert spec["paths"]["/a-b"]["get"]["operationId"] == "getAB"
    assert spec["paths"]["/a_b"]["get"]["operationId"] == "getAB2"
    assert spec["paths"]["/a_b"]["get"]["servers"] == [
        {"url": "https://api.ex.com"}, {"url": "https://ex.com"}]
    text = outputs.pop()
    assert "endpoint_id" not in text
    assert not any(ep.endpoint_id in text for ep in endpoints)


def test_root_path_and_origin_tie_break():
    spec = _export([
        EndpointDescriptor(url_template="https://b.ex.com"),
        EndpointDescriptor(url_template="https://a.ex.com/x"),
    ])

    assert spec["servers"] == [{"url": "https://a.ex.com"}]
    root = spec["paths"]["/"]["get"]
    assert root["operationId"] == "getRoot"
    assert root["servers"] == [{"url": "https://b.ex.com"}]


def test_paths_sorted_and_methods_in_canonical_order():
    methods = (HttpMethod.DELETE, HttpMethod.PATCH, HttpMethod.GET, HttpMethod.PUT, HttpMethod.POST)
    spec = _export([EndpointDescriptor(url_template="https://ex.com/z")] + [
        EndpointDescriptor(method=m, url_template="https://ex.com/a") for m in methods])

    assert list(spec["paths"]) == ["/a", "/z"]
    assert list(spec["paths"]["/a"]) == ["get", "post", "put", "patch", "delete"]


def test_sensitive_path_params_and_nested_list_values_are_redacted():
    reset = EndpointDescriptor(url_template="https://ex.com/reset/{token}",
                               path_params={"token": "path-secret"})
    body = {"items": [{"secret": "list-secret", "n": 1}], "Password": "upper-secret"}
    upload = EndpointDescriptor(method=HttpMethod.POST, url_template="https://ex.com/up", body=body)
    before = upload.model_dump()

    spec = _export([reset, upload])

    text = json.dumps(spec)
    assert not any(s in text for s in ("path-secret", "list-secret", "upper-secret"))
    [param] = spec["paths"]["/reset/{token}"]["get"]["parameters"]
    assert param["example"] == REDACTED
    example = spec["paths"]["/up"]["post"]["requestBody"]["content"]["application/json"]["example"]
    assert example == {"items": [{"secret": REDACTED, "n": 1}], "Password": REDACTED}
    assert upload.model_dump() == before


def test_collision_response_schemas_collapse_when_equal_else_any_of():
    a = ResponseSchema(properties={"a": {"type": "integer"}}, required=["a"])
    b = ResponseSchema(properties={"b": {"type": "string"}}, required=["b"])

    def response(*schemas: ResponseSchema) -> dict:
        spec = _export([
            EndpointDescriptor(method=HttpMethod.POST, url_template="https://ex.com/gql",
                               description=f"op{i}", body={"op": i}, response_schema=s)
            for i, s in enumerate(schemas)])
        return spec["paths"]["/gql"]["post"]["responses"]["default"]["content"]["application/json"]

    assert response(a, a)["schema"] == {
        "type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    assert [s["required"] for s in response(a, b, a)["schema"]["anyOf"]] == [["a"], ["b"]]


def test_empty_skill_exports_an_empty_document():
    spec = skill_to_openapi(_skill([]))
    assert (spec["openapi"], spec["servers"], spec["paths"]) == ("3.1.0", [], {})


def test_collisions_union_parameters_and_report_the_worst_health():
    ok = EndpointDescriptor(method=HttpMethod.POST, url_template="https://ex.com/rpc", description="a",
                            query={"v": "1"}, verification_status=VerificationStatus.VERIFIED,
                            trigger_url="https://ex.com/rpc?v=1")
    broken = EndpointDescriptor(method=HttpMethod.POST, url_template="https://ex.com/rpc", description="b",
                                query={"v": "2", "w": "3"}, headers_template={"X-Client": "web"},
                                verification_status=VerificationStatus.FAILED)

    op = _export([broken, ok])["paths"]["/rpc"]["post"]

    assert [(p["in"], p["name"], p["example"]) for p in op["parameters"]] == [
        ("query", "v", "1"), ("query", "w", "3"), ("header", "x-client", "web")]
    assert (op["x-rebrowse-verification"], op["x-rebrowse-observed"]) == ("failed", True)
    assert [(o["summary"], o["verification"], o["observed"]) for o in op["x-rebrowse-operations"]] == [
        ("a", "verified", True), ("b", "failed", False)]
    assert "requestBody" not in op


def test_graphql_variables_are_redacted_in_examples():
    spec = _export_traffic([
        _post("https://ex.com/graphql", {
            "operationName": "Login", "query": "mutation Login($u: String!, $p: String!) { login }",
            "variables": {"u": "ann", "password": "gql-secret"}}),
        _post("https://ex.com/graphql", {"operationName": "Me", "query": "query Me { me { id } }"}),
    ])

    media = spec["paths"]["/graphql"]["post"]["requestBody"]["content"]["application/json"]
    assert "gql-secret" not in json.dumps(spec)
    assert media["examples"]["GraphQL mutation: Login"]["value"]["variables"] == {
        "u": "ann", "password": REDACTED}
    assert media["schema"]["properties"]["variables"]["properties"]["password"] == {"type": "string"}


def test_unknown_and_mixed_types_become_unconstrained_schemas():
    spec = _export_traffic([
        _get("https://ex.com/api/feed", {"a": None, "b": [1, "x"], "c": 1}),
        _get("https://ex.com/api/feed", {"a": None, "b": [], "c": "one"}),
    ])

    schema = spec["paths"]["/api/feed"]["get"]["responses"]["default"]["content"]["application/json"]["schema"]
    assert schema["properties"] == {"a": {}, "b": {"type": "array", "items": {}}, "c": {}}


def test_browser_transport_and_credential_headers_are_not_parameters():
    browser = {"User-Agent": "ua", "Host": "h", "Content-Length": "3", "Origin": "o", "Referer": "r",
               "Accept-Language": "en", "Cache-Control": "no-cache", "Pragma": "no-cache",
               "Priority": "u=1", "DNT": "1", "sec-ch-ua": '"x"', "Sec-Fetch-Site": "same-origin"}
    credentials = {"X-CSRF-Token": "cred-1", "X-XSRF-TOKEN": "cred-2", "X-API-Key": "cred-3",
                   "Proxy-Authorization": "cred-4", "Set-Cookie": "cred-5"}
    ep = EndpointDescriptor(url_template="https://ex.com/api/v", headers_template={
        **browser, **credentials, "X-Client-Version": "2.1"})

    spec = _export([ep])

    assert [p["name"] for p in spec["paths"]["/api/v"]["get"]["parameters"]] == ["x-client-version"]
    assert not any(value in json.dumps(spec) for value in credentials.values())


def test_regenerated_endpoint_ids_do_not_change_the_export():
    endpoints = extract_endpoints([
        _get("https://ex.com/api/users/101", {"id": 101}),
        _post("https://ex.com/graphql", {"operationName": "A", "query": "query A { a }"}),
        _post("https://ex.com/graphql", {"operationName": "B", "query": "mutation B { b }"}),
    ], page_domain="ex.com")
    rebuilt = [ep.model_copy(update={"endpoint_id": f"new{i}"}) for i, ep in enumerate(endpoints)]
    skill = _skill(endpoints)

    first = skill_to_openapi(skill)
    again = skill_to_openapi(skill.model_copy(update={"endpoints": rebuilt[::-1]}))

    assert_valid(first)
    assert json.dumps(first) == json.dumps(again)


def test_version_is_a_hash_of_the_document_not_of_save_times():
    endpoints = extract_endpoints([
        _get("https://ex.com/api/users/101", {"id": 101}),
        _post("https://ex.com/graphql", {"operationName": "A", "query": "query A { a }"}),
    ], page_domain="ex.com")
    skill = _skill(endpoints)
    resaved = skill.model_copy(update={
        "created_at": "2031-01-01T00:00:00+00:00", "updated_at": "2031-01-02T00:00:00+00:00",
        "endpoints": [ep.model_copy(update={"endpoint_id": f"new{i}"})
                      for i, ep in enumerate(endpoints)]})

    spec = skill_to_openapi(skill)

    assert json.dumps(spec) == json.dumps(skill_to_openapi(resaved))
    info = spec["info"]
    assert list(info) == ["title", "version", "description", "x-rebrowse-skill-id"]
    assert re.fullmatch(r"[0-9a-f]{12}", info["version"])
    unversioned = {**spec, "info": {k: v for k, v in info.items() if k != "version"}}
    canonical = json.dumps(unversioned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert info["version"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    renamed = [endpoints[0].model_copy(update={"description": "a user"}), *endpoints[1:]]
    assert skill_to_openapi(_skill(renamed))["info"]["version"] != info["version"]


def test_only_json_and_form_bodies_get_schemas_and_examples():
    boundary = "----WebKitFormBoundaryAbC"
    multipart = "".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        for name, value in (("user", "ann"), ("password", "MP-SECRET"))) + f"--{boundary}--\r\n"
    soap = ('<?xml version="1.0"?><soap:Envelope><soap:Body><Login><user>ann</user>'
            "<password>XML-SECRET</password></Login></soap:Body></soap:Envelope>")
    spec = _export_traffic([
        _post("https://ex.com/api/upload", {"name": "a.txt"}),
        _post("https://api.ex.com/api/upload", multipart, f"multipart/form-data; boundary={boundary}"),
        _post("https://ex.com/soap", soap, "text/xml; charset=utf-8"),
    ])

    text = json.dumps(spec)
    assert not any(s in text for s in ("MP-SECRET", "XML-SECRET", "WebKitFormBoundary"))
    upload = spec["paths"]["/api/upload"]["post"]["requestBody"]["content"]
    assert list(upload) == ["application/json", "multipart/form-data"]
    assert upload["application/json"]["schema"]["required"] == ["name"]
    assert upload["multipart/form-data"] == {}
    assert spec["paths"]["/soap"]["post"]["requestBody"]["content"] == {"text/xml": {}}


def test_legacy_secret_names_are_redacted_without_hiding_ordinary_keys():
    login = "user=ann&passwd=S1&pwd=S2&csrfmiddlewaretoken=S3&otp=S4&PHPSESSID=S5"
    spec = _export_traffic([
        _post("https://ex.com/login", login, "application/x-www-form-urlencoded"),
        _get("https://ex.com/api/search?keyword=shoes&sort_key=price&author=ann&key=S6&appKey=S7",
             {"results": []}),
    ])

    form = spec["paths"]["/login"]["post"]["requestBody"]["content"][
        "application/x-www-form-urlencoded"]
    assert form["example"] == {"user": "ann", **dict.fromkeys(
        ("passwd", "pwd", "csrfmiddlewaretoken", "otp", "PHPSESSID"), REDACTED)}
    params = spec["paths"]["/api/search"]["get"]["parameters"]
    assert {p["name"]: p["example"] for p in params} == {
        "keyword": "shoes", "sort_key": "price", "author": "ann", "key": REDACTED,
        "appKey": REDACTED}


def test_get_graphql_documents_query_params_without_a_body():
    variables = json.dumps({"user": "ann", "password": "VAR-SECRET"})
    query = urlencode({"operationName": "Login", "variables": variables})
    spec = _export_traffic([_get(f"https://ex.com/graphql?{query}", {"data": {}})])

    op = spec["paths"]["/graphql"]["get"]
    assert "requestBody" not in op
    assert {p["name"]: p["example"] for p in op["parameters"]} == {
        "operationName": "Login", "variables": '{"user":"ann","password":"<redacted>"}'}
    assert "VAR-SECRET" not in json.dumps(spec)


def test_secret_names_ending_in_key_are_redacted():
    query = {"accesskey": "LEAK-1", "privatekey": "LEAK-2", "appkey": "LEAK-3", "authkey": "LEAK-4",
             "serverKey": "LEAK-5", "merchantKey": "LEAK-6", "apiKeys": "LEAK-9", "amount": "5"}
    spec = _export_traffic([
        _get(f"https://ex.com/api/pay?{urlencode(query)}", {"ok": True}),
        _post("https://ex.com/api/sign", {"hmacKey": "LEAK-7", "nested": {"sharedkey": "LEAK-8"}}),
    ])

    assert "LEAK-" not in json.dumps(spec)
    params = {p["name"]: p["example"] for p in spec["paths"]["/api/pay"]["get"]["parameters"]}
    assert params == {**dict.fromkeys(set(query) - {"amount"}, REDACTED), "amount": "5"}


def test_sensitive_keys_hide_whole_values_and_json_encoded_strings():
    body = {"credentials": {"user": "ann", "pass": "SUB-SECRET"},
            "payload": json.dumps({"refresh_token": "STR-SECRET", "n": 1}), "tags": ["a"]}
    spec = _export_traffic([_post("https://ex.com/api/connect", body)])

    media = spec["paths"]["/api/connect"]["post"]["requestBody"]["content"]["application/json"]
    assert media["example"] == {
        "credentials": REDACTED, "payload": '{"refresh_token":"<redacted>","n":1}', "tags": ["a"]}
    assert media["schema"]["properties"]["credentials"]["properties"] == {
        "user": {"type": "string"}, "pass": {"type": "string"}}
    assert "SECRET" not in json.dumps(spec)


def test_operation_id_suffixes_never_collide_with_real_paths():
    spec = _export([EndpointDescriptor(url_template=f"https://ex.com{p}") for p in ("/a_b", "/a-b2", "/a-b")])

    ids = {path: methods["get"]["operationId"] for path, methods in spec["paths"].items()}
    assert ids == {"/a-b": "getAB", "/a-b2": "getAB2", "/a_b": "getAB3"}
