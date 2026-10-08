from __future__ import annotations

from rebrowse.reverse.scanner import (
    is_third_party_bundle,
    scan_bundles_for_operations,
    scan_bundles_for_routes,
)

BUNDLE = """
fetch("/api/users");
fetch(`/api/users/${user.id}/posts`);
axios.post("/api/comments/delete", body);
client.put('/v2/profile/settings', data);
const LITERAL = "/api/feed/home";
fetch("/api/users");                // duplicate
const ASSET = "/static/logo.png";   // skipped
"""


def _routes():
    return {(r.method, r.path): r for r in scan_bundles_for_routes(
        {"https://site.com/app.js": BUNDLE}, "https://site.com")}


def test_methods_come_from_call_sites():
    routes = _routes()
    assert ("POST", "/api/comments/delete") in routes
    assert ("PUT", "/v2/profile/settings") in routes
    assert ("GET", "/api/comments/delete") not in routes


def test_template_literal_becomes_placeholder():
    routes = _routes()
    assert ("GET", "/api/users/{id}/posts") in routes
    assert routes[("GET", "/api/users/{id}/posts")].url == "https://site.com/api/users/{id}/posts"


def test_literals_and_dedupe():
    routes = _routes()
    assert ("GET", "/api/feed/home") in routes
    assert sum(1 for (_, p) in routes if p == "/api/users") == 1
    assert not any(p.startswith("/static") for (_, p) in routes)


LEGACY = """
const A = "/rest/invoices";
const B = `/rest/invoices/${invoice.id}/lines`;
const C = '/rest/customers/' + id;
const D = "/rest/";
const E = "/en/about";
http.post("/rest/invoices/void", body);
"""


def test_recorded_prefixes_find_routes_outside_api_paths():
    routes = scan_bundles_for_routes({"https://site.com/app.js": LEGACY}, "https://site.com",
                                     prefixes={"/rest/"})

    assert sorted((r.method, r.path, r.match_type) for r in routes) == [
        ("GET", "/rest/customers/", "recorded_prefix"),
        ("GET", "/rest/invoices", "recorded_prefix"),
        ("GET", "/rest/invoices/{id}/lines", "recorded_prefix"),
        ("POST", "/rest/invoices/void", "call"),
    ]


def test_without_prefixes_or_wide_the_scan_is_the_build_scan():
    bundles = {"https://site.com/app.js": BUNDLE + LEGACY}

    routes = scan_bundles_for_routes(bundles, "https://site.com")

    assert routes == scan_bundles_for_routes(bundles, "https://site.com", ())
    assert [r.path for r in routes if r.path.startswith("/rest/")] == ["/rest/invoices/void"]
    assert {r.match_type for r in routes} == {"call", "string_literal", "template"}


GRAPHQL = r"""
const CART = gql`
  query GetCart(
    $id: ID!
  ) { cart(id: $id) { ...CartFields } }
  fragment CartFields on Cart { id }
`;
const MINIFIED = "\nmutation RemoveItem{removeItem{id}}";
const WRAPPED = "query Search(\n  $q: String\n) { search(q: $q) { id } }";
const LIVE = "subscription OnCart @live { cart { id } }";
const A = {kind:"OperationDefinition",operation:"mutation",name:{kind:"Name",value:"Checkout"}};
const B = {"kind":"OperationDefinition","operation":"query","name":{"kind":"Name","value":"Me"}};
const C = {'operation': 'query', 'name': {'kind': 'Name', 'value': 'Me'}};
const TEXT = "query failed (" + reason + ")";
const NOTE = "subquery Nope { x }";
"""


def test_graphql_operations_from_documents_and_compiled_asts():
    ops = scan_bundles_for_operations({"https://site.com/app.js": GRAPHQL})

    assert [(op.kind, op.name, op.match_type) for op in ops] == [
        ("query", "GetCart", "document"),
        ("mutation", "RemoveItem", "document"),
        ("query", "Search", "document"),
        ("subscription", "OnCart", "document"),
        ("mutation", "Checkout", "compiled"),
        ("query", "Me", "compiled"),
    ]
    assert {op.source_bundle for op in ops} == {"https://site.com/app.js"}


def test_graphql_operations_are_deduplicated_across_bundles():
    ops = scan_bundles_for_operations({
        "https://site.com/a.js": "gql`query Me { me { id } }`",
        "https://site.com/b.js": "gql`query Me { me { name } }` gql`mutation Me { me }`",
    })

    assert [(op.kind, op.name, op.source_bundle) for op in ops] == [
        ("query", "Me", "https://site.com/a.js"), ("mutation", "Me", "https://site.com/b.js")]


def test_anonymous_operations_fragments_and_prose_are_not_operations():
    ops = scan_bundles_for_operations({"https://site.com/app.js": (
        'gql`query { me { id } }`; gql`fragment UserFields on User { id }`; '
        'const E = "query Search(" + term + ")"; const F = "$query Hidden { x }"; '
        "throw new Error(`query failed (${res.status})`); "
        "log(`mutation blocked (  ${why})`);")})

    assert ops == []


def test_template_literal_calls_keep_their_method():
    code = ("api.delete(`/api/items/${item.id}`); "
            "axios.post(`/api/orders/${id}/refund`, body); "
            "fetch(`/api/users/${id}?full=${full}`); "
            "const PAGE = `/api/pages/${n}`;")

    routes = scan_bundles_for_routes({"https://site.com/app.js": code}, "https://site.com",
                                     wide=True)

    assert sorted((r.method, r.path, r.match_type) for r in routes) == [
        ("DELETE", "/api/items/{id}", "call"),
        ("GET", "/api/pages/{n}", "template"),
        ("GET", "/api/users/{id}", "call"),
        ("POST", "/api/orders/{id}/refund", "call"),
    ]


def test_calls_and_recorded_prefixes_take_dotted_legacy_paths():
    code = ('fetch("/ajax/get_cart.php"); $.post("/ajax/save_cart.php", form); '
            "svc.delete(`/services/Cart.asmx/Remove/${line}`); "
            'const A = "/ajax/orders.php"; const B = "/services/OrderService.asmx/GetOrders"; '
            'const C = "/ajax/loader.js"; fetch("/ajax/strings.json");')

    routes = scan_bundles_for_routes({"https://site.com/app.js": code}, "https://site.com",
                                     prefixes={"/ajax/", "/services/"}, wide=True)

    assert sorted((r.method, r.path, r.match_type) for r in routes) == [
        ("DELETE", "/services/Cart.asmx/Remove/{line}", "call"),
        ("GET", "/ajax/get_cart.php", "call"),
        ("GET", "/ajax/orders.php", "recorded_prefix"),
        ("GET", "/services/OrderService.asmx/GetOrders", "recorded_prefix"),
        ("POST", "/ajax/save_cart.php", "call"),
    ]


def test_recorded_prefixes_are_literal_and_drop_the_query():
    code = 'const A = "/rest.svc/orders?page=2#top"; const B = "/restXsvc/items";'

    routes = scan_bundles_for_routes({"https://site.com/app.js": code}, "https://site.com",
                                     prefixes=["/rest.svc/"])

    assert [(r.path, r.match_type) for r in routes] == [("/rest.svc/orders", "recorded_prefix")]


def test_third_party_bundles():
    assert is_third_party_bundle("https://www.youtube.com/s/player/base.js", "dev.to")
    assert is_third_party_bundle("https://cdnjs.cloudflare.com/x.js", "dev.to")
    assert not is_third_party_bundle("https://dev.to/assets/app.js", "dev.to")
    assert not is_third_party_bundle("https://github.githubassets.com/app.js", "github.com")
    assert not is_third_party_bundle("https://www.youtube.com/s/player/base.js", "www.youtube.com")


def test_build_scan_ignores_assets_dotted_paths_and_template_calls():
    bundle = ('fetch("/fonts/inter.woff"); $.get("/img/logo.webp"); fetch("/version.txt");'
              'fetch("/ajax/get_cart.php"); api.delete(`/api/items/${id}`);'
              'router.get(`/${lang}/${page}`);')
    bundles = {"https://site.com/app.js": bundle}

    narrow = {(r.method, r.path) for r in scan_bundles_for_routes(bundles, "https://site.com")}
    wide = {(r.method, r.path)
            for r in scan_bundles_for_routes(bundles, "https://site.com", wide=True)}

    assert narrow == {("GET", "/api/items/{id}")}
    assert wide == {("DELETE", "/api/items/{id}"), ("GET", "/ajax/get_cart.php")}


def test_translation_placeholders_are_not_graphql_operations():
    bundles = {"https://site.com/app.js": (
        '"Your subscription renews {date}"; "Your search query returned {count} results";'
        '"This mutation affects {n} rows"; "query Feed { feed { id } }";'
        '"mutation Save { save(id: 1) { ok } }"; "query Me { ...MeFields }";'
        '"mutation Logout { logout }"')}

    found = {(op.kind, op.name) for op in scan_bundles_for_operations(bundles)}

    assert found == {("query", "Feed"), ("mutation", "Save"), ("query", "Me"),
                     ("mutation", "Logout")}
