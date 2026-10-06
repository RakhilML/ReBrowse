from __future__ import annotations

from rebrowse.models import RawRequest
from rebrowse.reverse.extractor import _merge_schemas, extract_endpoints, schema_from_values


def test_merge_objects_required_vs_optional():
    schema = schema_from_values([{"a": 1, "b": 2}, {"a": 9}])
    assert schema.type == "object"
    assert schema.inferred_from_samples == 2
    assert set(schema.properties) == {"a", "b"}
    assert schema.required == ["a"]
    assert "b" not in schema.required


def test_merge_list_of_objects():
    schema = schema_from_values([[{"x": 1}, {"x": 2}]])
    assert schema.type == "array"
    items = schema.items
    assert items["type"] == "object"
    assert items["required"] == ["x"]
    assert items["inferred_from_samples"] == 2


def test_merge_schemas_from_bodies():
    assert _merge_schemas([None, "not json", ""]) is None
    schema = _merge_schemas(['{"a": 1, "b": 2}', '{"a": 3}'])
    assert schema.required == ["a"]
    assert schema.inferred_from_samples == 2


def _req(url, body):
    return RawRequest(
        url=url, method="GET", response_status=200,
        response_headers={"content-type": "application/json"}, response_body=body,
    )


def test_extract_merges_samples_across_requests():
    reqs = [
        _req("https://ex.com/api/x", '{"a": 1, "b": 2}'),
        _req("https://ex.com/api/x", '{"a": 9}'),
    ]
    eps = extract_endpoints(reqs, page_domain="ex.com")
    assert len(eps) == 1
    schema = eps[0].response_schema
    assert schema is not None
    assert schema.inferred_from_samples == 2
    assert schema.required == ["a"]
    assert set(schema.properties) == {"a", "b"}
