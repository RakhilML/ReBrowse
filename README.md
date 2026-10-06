# rebrowse

Learn a website's internal APIs by watching it load, then call them directly.

`build` opens a page in headless Chromium, records its traffic, and turns the API calls
it finds into a saved **skill**. `run` takes a plain-English request, picks the matching
endpoint with an LLM, and calls it. `rebrowse mcp` serves the same skills to agents.

Private project — no license; all rights reserved.

## Setup

```bash
python -m venv .venv && .venv\Scripts\activate      # or: source .venv/bin/activate
pip install -e ".[dev,mcp]"
playwright install chromium
cp .env.example .env                                 # then pick an LLM provider
```

Requires Python 3.11+. Data lives in `~/.rebrowse/` (override with `REBROWSE_DATA_DIR`).

### LLM provider (`.env`)

| `LLM_PROVIDER` | Notes |
|---|---|
| `local` (default) | LM Studio / vLLM at `LLM_BASE_URL` (default `http://localhost:1234/v1`) |
| `ollama` | `http://localhost:11434/v1` |
| `openai` / `gemini` / `claude` | set `LLM_API_KEY` |

`LLM_MODEL_INTENT` parses requests and picks endpoints; `LLM_MODEL_CODE` describes
endpoints at build time. rebrowse refuses to send an API key over plain HTTP to a
non-local host, and warns once when prompts would leave the machine unencrypted.

## Usage

```bash
rebrowse build https://news.ycombinator.com
rebrowse build https://example.com --steps "type #q=shoes; click #search; wait 800"
rebrowse import-har session.har [-d app.local:8080]  # learn from a HAR export instead

rebrowse run "top stories on hacker news"
rebrowse run "add a note saying hi" --dry-run      # show the resolved call, send nothing
rebrowse run "add a note saying hi" --yes          # confirm a call that changes state

rebrowse verify news.ycombinator.com               # health-check a skill's read endpoints
rebrowse openapi news.ycombinator.com -o api.json  # OpenAPI 3.1 export (stdout without -o)
rebrowse mock e2e.har [-p 8787]                    # serve the recorded API on 127.0.0.1
rebrowse diff baseline.har e2e.har                 # API changes that break the client
rebrowse skills [-q "search"]  |  rebrowse show <id>  |  rebrowse delete <id>
rebrowse auth set api.github.com ghp_xxx [--type bearer|header|query]
rebrowse mcp                                       # MCP server over stdio
```

Commands print JSON to stdout (progress goes to stderr) and exit non-zero on errors.

## How it works

**build** — capture → extract → describe → save.
- Records every response (with full headers and cookies) for the initial load, three
  scrolls, and any `--steps` interactions. The raw capture is saved to
  `captures/` so it can be re-extracted without browsing again.
- Drops static files, telemetry (segment-aware, so `/api/login` survives while `/log`
  doesn't), ads, and other sites (by registrable domain).
- Scores, templatizes IDs (`/users/123` → `/users/{users_id}`), and dedupes. GraphQL is
  split into one endpoint per operation.
- Scans first-party JS bundles for routes, taking the method from the call site
  (`axios.post(...)` → POST) and turning template literals into placeholders.
- Infers response schemas across every sample (fields seen in all samples are required).
- Rebuilding a site replaces its skill rather than adding a duplicate.

**run** — the LLM extracts the domain and intent, the skill is found by domain alias or
semantic search (matches below 0.25 similarity are refused, not guessed), and the LLM
picks one endpoint from the healthy ones. Captured values never reach the picker, only
parameter names, and the endpoint list is marked as untrusted data.

**verify** — re-executes read endpoints only and records `verified`/`failed` and a
reliability score; `run` skips failed endpoints and ranks the rest.

## OpenAPI export

`rebrowse openapi <target>` turns a skill into an OpenAPI 3.1 JSON document that Swagger UI
or Redoc can render, Prism can mock (`prism mock api.json`) and Schemathesis can
contract-test. Each operation carries `x-rebrowse-effect` (`read`/`write`/`destructive`),
`x-rebrowse-verification` and `x-rebrowse-observed` (`false` for routes found only in a JS
bundle), so a test run can be limited to reads. Response schemas mark fields seen in every
sample as required; GraphQL operations sharing one URL are merged into one documented
operation. The export runs offline, never includes cookies or auth headers, and redacts
values whose names look secret (`password`, `passwd`, `token`, `csrf`, `session`, `api_key`,
...), also inside JSON-encoded query values such as GraphQL `variables`. Request bodies get a
schema and example only when they are JSON or form-encoded; multipart and XML bodies are
listed by media type alone, and GET operations never document a body. Two exports of an
unchanged API are not identical (descriptions come from the LLM and `info.version` is the
save time), so to see what changed, compare the recordings:
`rebrowse diff baseline.har e2e.har` (see [Drift report](#drift-report)).

## Import from HAR

`build` only sees what a fresh, logged-out browser sees in a few scrolls. If you already
have the traffic, import it instead: `rebrowse import-har session.har` reads a HAR 1.2 file
and builds the same kind of skill, without launching a browser or replaying any request.

- **Browser DevTools.** Sign in yourself, use the app, then in the Network panel choose
  "Save all as HAR" (Chrome) or "Save All As HAR" (Firefox).
- **An e2e suite.** Playwright records one with
  `browser.new_context(record_har_path="e2e.har")`, so every flow the tests exercise ends up
  in the skill.
- **A proxy.** mitmproxy, Charles and Proxyman all export HAR.

The skill is learned for one site: `--domain host[:port]`, defaulting to the host of the
first HTML page in the file. Credentials are stripped as the file is read, before anything
is stored or sent to the LLM: `Cookie`, `Set-Cookie`, `Authorization`, API-key, CSRF and
session headers are dropped, the HAR's cookie lists are ignored, and query, JSON and form
values with secret-looking names (passwords, tokens, keys, SAML assertions...) are replaced
by `<redacted>`. A body that parses as JSON is treated as JSON whatever its declared type;
other bodies that are not form-encoded (multipart, XML, binary) are dropped, because they
cannot be redacted.
Response bodies are only used to infer schemas. The HAR itself is the source, so nothing is
copied to `captures/`; import it again to re-extract.

rebrowse never imports cookies from a HAR, and `auth set` only holds a bearer token, an
`X-API-Key` or an `api_key` query value, so an app that signs in with a session cookie can be
documented but not replayed. `verify` reports such reads as failed: a 401, or `auth_required`
when the request is redirected to an HTML sign-in page. Importing the same site again, or
after a `build`, replaces its skill and keeps its id. In CI, compare the e2e run's HAR with
the one recorded on the main branch, which fails the job on a change that breaks the client
(see [Drift report](#drift-report)):

```bash
rebrowse diff baseline.har e2e.har
```

## Mock server

`rebrowse mock SOURCE` answers a frontend's API calls from recorded traffic, so the UI,
Storybook or an e2e suite can run without the real backend. SOURCE is a HAR file, a capture
saved by `build`, or a `host[:port]` whose newest saved capture is used (`--domain` picks the
site in a HAR, as for `import-har`):

```bash
rebrowse mock e2e.har                                     # http://127.0.0.1:8787
rebrowse build http://localhost:8080 && rebrowse mock localhost:8080 -p 0
```

Point the app's API base URL, or its dev-server proxy, at the printed `url`. The route table
is printed to stdout as JSON at startup, and each request is logged to stderr with how it was
answered.

- **Matching.** Routes are the endpoints the skill and the OpenAPI export see: same-site API
  calls with ids templated and one route per GraphQL operation, including persisted queries
  and GET `?query=` (a batch only matches the same operations in the same order). An unseen
  id is answered from its template (`/api/users/999` gets the recorded `/api/users/1001`),
  and a literal route beats a template (`/users/me` over `/users/{id}`). Routes on sibling
  hosts stay apart; a path recorded on several hosts is answered from the site's own host and
  marked `nearest`. Within a route the closest recording wins: one with a body over one whose
  body was not recorded (DevTools drops them after a navigation; an empty body counts as
  recorded), same path, most shared query pairs, fewest recorded pairs missing from the
  request, then the same for top-level body fields (so `action=save_post` picks the
  `save_post` recording of an RPC endpoint), same body, then a 2xx over an error. Responses carry `x-rebrowse-mock: exact|nearest` and `x-rebrowse-route`, and the
  stderr log adds `no body recorded` when the only recording has none. Anything else is a 404
  with `x-rebrowse-mock: miss` and the list of recorded routes; an unrecorded GraphQL operation
  is a miss, never another operation's data.
- **What is served.** The recorded status and body with its `content-type` (dropped unless it
  is printable ASCII), and no other recorded header, so no `Set-Cookie`. Bodies are sent as
  UTF-8 and a recorded `charset` is rewritten to match. JSON values under secret-looking keys
  (`access_token`, `password`, ...) are replaced by `<redacted>`, also behind a BOM, an XSSI
  guard such as `)]}'` or a JSONP callback, and form-encoded bodies are redacted pair by pair;
  other text bodies, such as XML, are served as recorded. Redirects and
  304s are skipped, as are static files, telemetry and other sites. `HEAD` is not supported.
- **Never the real site.** The mock has no HTTP client: writes get their recorded response
  and nothing is forwarded. It keeps no state either, so a POST does not change what a later
  GET returns.
- **Local only.** It binds 127.0.0.1, refuses requests whose `Host` is not a loopback name
  (DNS rebinding), answers CORS, with credentials, only for `localhost`, `127.0.0.1` and
  `[::1]` origins, and refuses cross-site requests from pages that are not on localhost, so
  another website cannot read it through a `<script>` tag.

## Drift report

`rebrowse diff BASE HEAD` compares the API traffic in two recordings of the same app and
reports what changed from the client's point of view, without a spec, an LLM or the network.
BASE and HEAD are read as `mock` reads SOURCE: a HAR file, a capture saved by `build`, or a
`host[:port]` whose newest saved capture is used. By default each side uses the host of its
own first HTML page, so `prod.har` and `staging.har` line up; `--domain` picks the site in
both files, or in BASE then HEAD when given twice. A side with no API traffic, such as a run
that never got past sign-in, is an input error rather than a pass.

```bash
# main: run the e2e suite with browser.new_context(record_har_path="baseline.har"), keep it
# PR:   run it again with record_har_path="e2e.har", then
rebrowse diff baseline.har e2e.har       # exit 0: nothing breaks, 1: breaking, 2: bad input
rebrowse diff prod.har staging.har       # or two DevTools exports, or two build captures
```

- **Routes.** The ones `mock` serves: same-site API calls with ids templated
  (`/api/users/1001` and `/api/users/42` are one route), one route per GraphQL operation, and
  sibling hosts kept apart. Unlike `mock`, redirects are kept, and a 304 counts as an
  answer.
- **Breaking.** A route that answered 2xx and no longer does, such as one that now redirects
  to a sign-in page, or that starts returning a 5xx it never did (`status`); a 2xx response
  with a media type BASE never returned, such as JSON turning into an HTML page
  (`content_type`); JSON turning into an empty body or a 204 (`body_empty`); a field present
  in every BASE response that is gone (`field_removed`) or missing from some HEAD responses
  (`field_optional`); a field with a new JSON type, including a new `null` (`field_type`).
  `integer` where BASE had `number` is fine, and `number` where BASE had `integer` is info,
  since a JavaScript client cannot tell them apart (`1.0` counts as an integer).
- **Info.** Other status changes, added fields, removed fields that BASE did not always send
  (that may be sampling), and routes recorded on one side only (`route_added`,
  `route_missing`). The client drives the calls, so an endpoint the backend removed while the
  client still calls it shows up as a status change.
- **Fields.** Read from 2xx JSON bodies (also behind an XSSI guard), down to 12 levels; a route
  recorded without bodies, as DevTools does after a navigation, gets no field changes. A field
  that is no longer an object is reported once, not field by field. Paths read
  `$.items[].sku` and `$["content-type"]`. A key that looks like a value rather than a name
  is a map key: one that does not start with a letter (an id or a date), holds a character
  other than letters, digits, `_`, `.`, `:` and `-` (a name or an email), is a UUID or 16 or
  more hex digits, or is 16 or more name characters with digits in three or more places (a
  generated id or token, such as `ghp_...`, a nanoid or a JWT). The values of all such keys
  are compared together under `.*`, as in `$.members.*.role`, and the key is not reported.
  Map keys that read like names (`alice`, `SKU123ABC`, a slug) cannot be told from fields
  and are reported as fields. Names with digits, such as `address_line_2` or
  `oauth2RedirectUri`, stay fields, and fields with secret names such as `accessToken` are
  compared by type like any other.

The report is JSON on stdout: each side's source, domain, route count and how many of
those routes had JSON bodies to compare (a HAR recorded without content compares statuses
and media types only), the number of breaking changes, and the changes ordered by route,
then status, media type, body and fields depth-first. It holds route names, statuses, media
types, field paths and JSON type names, never a response value:

```json
{"severity": "breaking", "kind": "field_type", "route": "GET /api/orders/{orders_id}",
 "field": "$.total", "base": ["string"], "head": ["null", "string"]}
```

## Safety model

- **Effects.** Every endpoint is labelled `read`, `write` or `destructive` from its
  method, path verbs (`/comment/destroy` over GET is destructive) and GraphQL operation
  kind (a POST GraphQL *query* is a read).
- **Confirmation.** Nothing but a read is ever sent without `--yes` (CLI) or
  `confirm=true` (MCP `act`). Writes are never retried, since a timed-out write may
  already have applied.
- **Honest identity.** The capture browser is not disguised, and replays identify as
  `rebrowse/<version>` instead of resending a browser fingerprint.
- **Blocks are failures.** Challenge pages (Cloudflare "Just a moment…", robot-policy
  403s, CAPTCHAs) are reported as `blocked`, never returned as data; there is no
  bypass logic.
- **Credentials stay scoped.** Stored cookies and API keys are attached only to their
  own site and are dropped from any redirect that leaves it; nothing reads your browser's
  cookie store. Requests are paced per host
  (`REBROWSE_HOST_INTERVAL`, default 1s).

## MCP

```json
{ "mcpServers": { "rebrowse": { "command": "rebrowse", "args": ["mcp"] } } }
```

Tools: `search_skills`, `list_operations` (read-only), `read` (executes reads only), and
`act` (write/destructive; returns `confirmation_required` until called with
`confirm=true`). Effects map onto MCP's `readOnlyHint` / `destructiveHint`.

## Development

```bash
pytest            # local fixture site, real headless Chromium, no internet
ruff check rebrowse tests
```

Tests run against a local fixture site with a stubbed LLM and a deterministic stand-in
for the embedding model, in an isolated data directory.

The `skills/` folder holds documentation of skills captured in March 2026 with an
earlier version; it is not loaded by the tool.
