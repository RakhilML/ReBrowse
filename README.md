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
rebrowse openapi api-baseline.json -o api.json     # ... from a recording, no LLM or skill
rebrowse mock e2e.har [-p 8787]                    # serve the recorded API on 127.0.0.1
rebrowse baseline e2e.har -o api-baseline.json     # a recording that is safe to commit
rebrowse diff api-baseline.json e2e.har            # API changes that break the client
rebrowse contract api-baseline.json --against http://localhost:8000 --follow-ids  # replay reads
rebrowse diff api-baseline.json e2e.har --accepted diff-accepted.json  # reviewed breaks pass
rebrowse coverage e2e.har --fail-under 80          # API calls in the JS the recording missed
rebrowse coverage test-results/                    # any SOURCE may be a folder of HAR files
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
  (`axios.post(...)` → POST, `` api.delete(`/items/${id}`) `` → DELETE) and turning
  template literals into placeholders.
- Infers response schemas across every sample (fields seen in all samples are required).
- Rebuilding a site updates its skill rather than adding a duplicate. An endpoint seen again
  (same method, URL template and GraphQL operation) keeps its id, its description and its
  `verify` result. The result is reset when the endpoint's effect changed, for example a read
  that now classifies as a write, or when it moved between observed traffic and a route found
  only in a JS bundle. The LLM only describes endpoints that have no description
  yet, at most 25 per run, so a large app gets the rest on later runs, and an LLM outage
  leaves existing descriptions alone. Request templates, examples and response schemas always
  come from the new recording. Endpoints the new recording did not see are dropped. The JSON
  output reports `changes` (`added`, `kept`, `dropped`, `described`),
  marks each endpoint `added` or `kept`, lists the `dropped` ones with their old ids, and
  lists kept endpoints whose effect changed (`effect_changes`, such as a read that became a
  write). To get fresh descriptions, `rebrowse delete` the skill and build again; that also
  resets ids and `verify` results.

**run** — the LLM extracts the domain and intent, the skill is found by domain alias or
semantic search (matches below 0.25 similarity are refused, not guessed), and the LLM
picks one endpoint from the healthy ones. Captured values never reach the picker, only
parameter names, and the endpoint list is marked as untrusted data.

**verify** — re-executes read endpoints only and records `verified`/`failed` and a
reliability score; `run` skips failed endpoints and ranks the rest.

## OpenAPI export

`rebrowse openapi <target>` turns a skill, or a recording (see
[From a recording](#from-a-recording)), into an OpenAPI 3.1 JSON document that Swagger UI
or Redoc can render, Prism can mock (`prism mock api.json`) and Schemathesis can
contract-test. Each operation carries `x-rebrowse-effect` (`read`/`write`/`destructive`), so
a test run can be limited to reads; a skill's export also carries `x-rebrowse-verification`
and `x-rebrowse-observed` (`false` for routes found only in a JS bundle). Response schemas mark fields seen in every
sample as required; GraphQL operations sharing one URL are merged into one documented
operation. The export runs offline, never includes cookies or auth headers, and redacts
values whose names look secret (`password`, `passwd`, `token`, `csrf`, `session`, `api_key`,
...), also inside JSON-encoded query values such as GraphQL `variables`. Request bodies get a
schema and example only when they are JSON or form-encoded; multipart and XML bodies are
listed by media type alone, and GET operations never document a body. `info.version` is the
first 12 hex digits of a SHA-256 of the rest of the document, not a save time, so it
changes exactly when the document does. Re-learning a recording that carries the same
requests and values gives a byte-identical file, because kept endpoints keep their
descriptions, which makes a committed `api.json` diff read as an API change log. This holds
within one data directory: a fresh `~/.rebrowse` creates a new skill, with a new
`x-rebrowse-skill-id` and new LLM descriptions, so in CI document the committed baseline
instead (see [From a recording](#from-a-recording)), or cache and restore
`$REBROWSE_DATA_DIR/skills.db` between runs, and only that file. It holds no secrets, while
`vault/` keeps stored API keys and cookies next to the key that decrypts them and `captures/`
keeps raw traffic; never cache either, since pull-request workflows can restore a cache.
Values that differ per session, such as a per-request id header or feed ids, still show up
as example changes. To check whether a change breaks the client, compare the recordings:
`rebrowse diff api-baseline.json e2e.har` (see [Drift report](#drift-report)).

### From a recording

When TARGET is a file, a HAR, a capture saved by `build` or a
[baseline](#committable-baselines), or a [directory of HAR files](#many-recordings),
`rebrowse openapi` documents its traffic directly
(`--domain` picks the site in a HAR, as for `mock`). No LLM, embedding model or `skills.db`
is involved, nothing is stored and nothing is sent over the network, so a DevTools export
becomes docs in one offline command:

```bash
rebrowse openapi session.har -o api.json
# CI: regenerate the docs from the baseline committed for diff and contract
rebrowse openapi api-baseline.json -o docs/openapi.json && git diff --exit-code docs/openapi.json
```

- **Same bytes.** The recording is first reduced to what `baseline` keeps, and the document
  is built from that alone, so a HAR and the baseline written from it give byte-identical
  documents, as does a new recording of the same calls against an unchanged API. `info`
  holds the title and a `version` hashed from the rest of the document; no skill id, time or
  source path.
- **Operations.** The routes `diff` compares: same-site API calls with ids templated,
  redirects and 304s included. A path recorded on a sibling host lists its origins under the
  operation's `servers`. GraphQL operations sharing a URL are one operation that names them
  in `x-rebrowse-graphql`, with one request example per operation. `x-rebrowse-effect` is the
  most severe effect among the operation's calls. Query and header parameters are required
  when every recorded call sent them.
- **Responses.** Each recorded status is its own response, so a route that answered 200, 404
  and a 302 to the sign-in page documents all three; a status outside 100-599, such as a
  proxy's 999, is documented as `default`. 204, 205 and 304 responses have no
  content, nor does a response recorded without a content type, such as a bare 302 (browsers
  write `x-unknown` for these in a HAR). A body that is not JSON, such as an HTML page, is
  listed by media type alone. Responses carry schemas, never values.
- **Schemas as `diff` reads them.** Every recorded body counts, down to 12 levels, and a
  field is `required` when every object recorded at its place had it, the rule by which
  `diff` and `contract` call its removal breaking. They agree on a route on one host with one
  2xx status and one media type; where this document merges GraphQL operations or sibling
  hosts, a field only one of them always returns is optional here, and where a route
  answered with two 2xx statuses or media types, `required` is per status and media type
  while `diff` pools them. `integer` folds into `number` when both were seen, and a field seen
  as a string and as null is `["null", "string"]`. Keys that hold values, such as ids, emails, dates and
  tokens, are documented as `additionalProperties`,
  never as property names, so `{"members": {"ann@corp.test": {"role": "admin"}}}` documents
  `members.additionalProperties.properties.role`.
- **What is left out.** Everything `baseline` drops: credentials, cookies, response values,
  calls with a credential in the path and cache-busters; secret-named query, body and GraphQL
  argument values read `<redacted>`. Request examples are the baseline's, so read bodies keep
  their values and write bodies hold placeholders. A JSON body sent as `text/plain` (fetch's
  default) gets a schema like any JSON body. A form-encoded GraphQL write keeps its operation
  fields; other form, multipart and XML write bodies are not documented, since `baseline`
  drops them.
- **Compared with the skill export.** There are no LLM descriptions (summaries read
  `GET /api/orders/{orders_id}`), no routes found only in JS bundles and no `verify` health;
  those stay with the skill export. A TARGET that is not a file or directory is still a skill
  id or domain, and a file or directory wins over a skill of the same name.

## Import from HAR

`build` only sees what a fresh, logged-out browser sees in a few scrolls. If you already
have the traffic, import it instead: `rebrowse import-har session.har` reads a HAR 1.2 file
and builds the same kind of skill, without launching a browser or replaying any request.

- **Browser DevTools.** Sign in yourself, use the app, then in the Network panel choose
  "Save all as HAR" (Chrome) or "Save All As HAR" (Firefox).
- **An e2e suite.** Record one HAR per test and import the folder,
  `rebrowse import-har test-results/` (see [Many recordings](#many-recordings)), so every
  flow the tests exercise ends up in the skill. `browser.new_context(record_har_path="e2e.har")`
  gives a single file only when every test shares that one context, and @playwright/test
  gives each test its own.
- **Playwright recordings.** `page.routeFromHAR('hars/app.har', { update: true })`, the HAR
  mocking workflow, and `recordHar: { path: 'app.har', content: 'attach' }` write each body to
  its own file next to the HAR (`<sha1>.json`), which the HAR names in `_file`.
  `recordHar: { path: 'network.zip' }` writes a zip archive holding `har.har` and those files.
  rebrowse reads those bodies, and a `.zip` archive is accepted wherever a HAR file is, so
  `rebrowse import-har hars/` or `rebrowse openapi network.zip` work on the recordings the
  tests already replay. Keep the body files with the HAR: a JSON, text or request body whose
  file is missing is an input error naming the HAR and the file, never a recording without
  its bodies, and a `_file` that is not a plain file in the HAR's own folder (a path, a
  symlink) is refused. Only those files, or the archive's entries, are opened.
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
`X-API-Key` or an `api_key` query value, so `run` and `verify` cannot replay an app that signs
in with a session cookie; `contract` can, with a session you pass through `--header-env`. `verify` reports such reads as failed: a 401, or `auth_required`
when the request is redirected to an HTML sign-in page. Importing the same site again, or
after a `build`, updates its skill and keeps its id. Endpoints seen before keep their ids,
descriptions and `verify` results, and only endpoints without a description are sent to
the LLM; the output reports what was `added`, `kept` and `dropped`. In CI, compare the e2e run's HAR with
a baseline committed from the main branch, which fails the job on a change that breaks the client
(see [Committable baselines](#committable-baselines) and [Drift report](#drift-report)):

```bash
rebrowse diff api-baseline.json e2e.har
```

## Many recordings

An e2e suite rarely writes one HAR. @playwright/test gives every test a fresh browser context,
so recording per context leaves one HAR per test, and parallel workers write their own. Cypress
HAR plugins write one per spec, and DevTools exports made by hand cover one area of an app at a
time. Every command that reads a recording (`openapi`, `mock`, `baseline`, `diff`, `contract`,
`coverage` and `import-har`) also takes a directory, and reads every file under it whose name
ends in `.har`, and every Playwright HAR archive (a `.zip` holding `har.har`), in any case and
at any depth, as one recording:

```bash
rebrowse baseline test-results/ -o api-baseline.json
rebrowse coverage test-results/ --fail-under 80
rebrowse diff api-baseline.json test-results/
rebrowse diff main-hars/ pr-hars/
```

- **Order.** Files are read in the order of their path below the directory, written with `/`
  and compared as plain strings (`B/x.har` before `a/x.har`), so a tree reads the same way on
  Windows, macOS and Linux. Directory symlinks and junctions are not followed, and dot
  folders, `node_modules`, `__MACOSX` and AppleDouble `._*.har` files are skipped, so pointing
  rebrowse at a repo root reads only your own recordings.
- **One recording.** Requests are kept in file order, and a script recorded in several files
  is read once, from the first. The site is chosen once, over every file, as for one HAR:
  `--domain`, else the host of the first HTML page. A file with no call to the site is fine,
  since it may hold only sibling-host or third-party calls. `coverage` reads every same-site
  script in every file, and `import-har` scans at most 20, counted over all files.
- **One file at a time.** Each file is parsed and has its credentials dropped before the next
  is opened, so memory holds one raw HAR plus what rebrowse keeps.
- **Same bytes.** `baseline` sorts and deduplicates, so `baseline test-results/` writes the
  same file as a baseline of one HAR holding the same calls, and renaming or reordering the
  files changes nothing unless it changes which site is chosen. The same holds for `openapi`.
- **Errors.** A directory with no `.har` file is an input error, and so is a broken
  recording, which the error names: a `.har` or archive that is not valid JSON or HAR, such
  as one cut short by a crashed test, or whose body files are missing, and any `.zip` that is
  empty or cut short, whatever its name, since it may have been a HAR archive. Nothing is
  written, so a broken file never silently shrinks a baseline. The exit code is the command's
  own for bad input.
- **Output.** stderr gets one line, such as `[baseline] read 14 HAR files under test-results`,
  and the JSON on stdout keeps its shape, with `source` set to the directory. A directory wins
  over a saved capture of a host with the same name, as a file does.

Captures and baselines (`.json`) in the directory are not read, and neither are the body
files Playwright attaches to a HAR, except through the HAR that names them. Playwright's
`trace.zip` and any other zip without a `har.har` entry are skipped, as is a file named `.zip`
that is not a zip, while an empty or cut-short zip is an error. A fixture that records each test into its own folder under `test-results/`
(with `testInfo.outputPath('network.zip')`, each test writes an archive instead):

```ts
// fixtures.ts; specs import { test, expect } from './fixtures'
import { test as base } from '@playwright/test';

export const test = base.extend({
  contextOptions: async ({ contextOptions }, use, testInfo) => {
    await use({ ...contextOptions, recordHar: { path: testInfo.outputPath('network.har') } });
  },
});
export { expect } from '@playwright/test';
```

```python
# conftest.py, with pytest-playwright
import re

import pytest


@pytest.fixture
def context(browser, browser_context_args, request):
    name = re.sub(r"[^\w.-]+", "-", request.node.nodeid)
    context = browser.new_context(**browser_context_args,
                                  record_har_path=f"test-results/{name}/network.har")
    yield context
    context.close()  # writes the HAR
```

## Mock server

`rebrowse mock SOURCE` answers a frontend's API calls from recorded traffic, so the UI,
Storybook or an e2e suite can run without the real backend. SOURCE is a HAR file or a
[directory of them](#many-recordings), a capture saved by `build`, or a `host[:port]` whose
newest saved capture is used (`--domain` picks the site in a HAR, as for `import-har`):

```bash
rebrowse mock e2e.har                                     # http://127.0.0.1:8787
rebrowse mock hars/                                       # the HARs Playwright tests replay
rebrowse build http://localhost:8080 && rebrowse mock localhost:8080 -p 0
```

`rebrowse mock hars/` serves the same HARs the Playwright tests replay with `routeFromHAR`,
bodies included, to Storybook or the dev server.

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
BASE and HEAD are read as `mock` reads SOURCE: a HAR file or a directory of them, a capture
saved by `build` or written by [`baseline`](#committable-baselines), or a `host[:port]` whose
newest saved capture is used. By default each side uses the host of its own first HTML page, so `prod.har` and
`staging.har` line up; `--domain` picks the site in both files, or in BASE then HEAD when
given twice. A side with no API traffic, such as a run that never got past sign-in, is an
input error rather than a pass.

```bash
# main: run the e2e suite with a HAR per test (see Many recordings), then commit
rebrowse baseline test-results/ -o api-baseline.json   # no credentials or response values
# PR:   run it again, then
rebrowse diff api-baseline.json test-results/  # exit 0: nothing breaks, 1: breaking, 2: bad input
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

## Contract tests

`rebrowse contract SOURCE --against ORIGIN` answers the question a backend pull request asks:
does the changed backend still answer the calls the frontend makes? It replays the reads
recorded in SOURCE against ORIGIN and judges the live answers with the rules of `diff`, so the
frontend's own traffic is the contract: no hand-written consumer contracts, browser, LLM or
skill, and nothing is stored. SOURCE is read as `mock` reads it, and ORIGIN is
`scheme://host[:port]` with no path.

```bash
# frontend, once: record the e2e run with a HAR per test (see Many recordings), write
# `rebrowse baseline test-results/ -o api-baseline.json` and commit that file next to the
# backend, never the HARs. Backend CI, on every pull request:
docker compose up -d --wait api    # seeded with a few fixtures of its own
REBROWSE_HOST_INTERVAL=0 rebrowse contract api-baseline.json --against http://api:8000 --follow-ids
# exit 0: compatible, 1: breaking change or failed replay,
# 2: bad input, no answer at all, or every read refused or redirected
```

- **What is sent.** Only calls classified as reads: GETs, and GraphQL queries POSTed with
  their query text. A GraphQL document that defines a mutation anywhere counts as a mutation,
  and a query parameter such as `action`, `cmd`, `op` or `_method` with a write or delete verb
  (`/ajax.php?action=delete`) makes the call a write. Writes, destructive calls, routes
  recorded only as 304, event streams, and calls with a credential-like path segment (a JWT)
  are listed under `skipped` with the reason, and never sent; there is no `--yes`. Each route gets at most three
  distinct recorded requests, one at a time, in recorded order (sources first with
  `--follow-ids`), with no retries and no redirects followed, so a route that now redirects
  to `/login` is compared as a 302.
  Recorded calls to sibling hosts, such as `api.app.com` next to `app.com`, are skipped;
  `--domain api.app.com` picks the host under test, in a HAR or in a saved capture of
  `www.app.com`. Requests to a host other than localhost are paced like `verify`
  (`REBROWSE_HOST_INTERVAL=0` turns that off for a CI service name such as `api`). Every
  replay has one overall deadline and reads at most the size limit, so a stream or a huge
  export cannot hang the run.
- **What is not sent.** The URL is ORIGIN plus the recorded path and query, with secret-named
  path parameters such as `;jsessionid=` removed, secret-named query values redacted, jQuery's
  `_=<timestamp>` cache-buster and the fragment dropped. Cookies, `Authorization`, API-key,
  CSRF and session headers, headers whose value looks like a credential, `Origin`, `Referer`,
  the conditional headers (`If-None-Match`, `If-Modified-Since`, ...), method overrides
  (`X-HTTP-Method-Override`) and per-request tracing headers (`traceparent`, `tracestate`,
  `baggage`, `sentry-trace`, `X-Request-Id`, `X-Correlation-Id`, `X-Amzn-Trace-Id`, B3,
  Datadog and New Relic headers, ...) are dropped, as are headers that are not valid
  HTTP as recorded (a non-token name, a value with control or non-ASCII characters), and the
  User-Agent is rebrowse's own. Bodies of non-GET reads are redacted like the query, and so
  are secret-named arguments written into a GraphQL document (`user(token: "...")`). The only
  credentials ORIGIN gets are the `--header-env` headers below and an API key stored with
  `rebrowse auth set` for ORIGIN's own host; recorded cookies are never sent, and keys stored
  for the recorded site never go to ORIGIN. A recording that cannot be sent at all, such as a
  URL over 64 KB, gives way to the next one, and a route left with none is skipped as
  `unreplayable`.
- **Credentials.** A backend behind a sign-in answers every replay with a 302 to `/login` or
  a 401. `--header-env NAME=ENVVAR` sends header NAME, with the value of environment variable
  ENVVAR, on every replay to ORIGIN; repeat it for a tenant or CSRF-style header. Values are
  read from the environment only, never from the command line, so they stay out of shell
  history, process lists and CI logs. Surrounding whitespace, such as the trailing newline
  of a secret saved from a file, is stripped, and the value is never printed, stored or sent
  to an LLM: the report lists only the lowercase header names, under `credentials`. A
  `--header-env` header replaces a recorded header of the same name and the header an
  `auth set` key for ORIGIN adds, so `--header-env Authorization=CI_TOKEN` wins over a stored
  bearer key, while a query key stays in the query. Host, User-Agent and the framing headers
  cannot be set, and an unset variable or a value with control or non-ASCII characters exits
  2 before anything is sent. That error names at most the header, never the variable or
  anything after `=`, so a value typed in place of the variable's name, as in
  `--header-env "Cookie=$SESSION"`, is not printed either. The value goes to ORIGIN exactly
  as given and nowhere else, since no redirect is followed and no write is sent, so use https
  for a remote host; an `HTTP(S)_PROXY` set in the environment is honoured, as for every
  request rebrowse sends. rebrowse never signs in itself: getting the session is your own script
  or CI secret.

  ```bash
  # a script of yours signs a seeded test user in and prints its session cookie
  STAGING_COOKIE="$(./scripts/test-user-session.sh)" rebrowse contract api-baseline.json \
    --against https://staging.app.com --header-env Cookie=STAGING_COOKIE
  # CI_API_TOKEN holds "Bearer ...", CI_TENANT the tenant id
  rebrowse contract api-baseline.json --against http://api:8000 \
    --header-env Authorization=CI_API_TOKEN --header-env X-Tenant=CI_TENANT
  ```
- **Ids from ORIGIN's own answers.** A recording made against staging asks for staging's
  rows: `/api/orders/81723`, `?userId=4410`, a GraphQL `Order($id)` variable. A CI backend
  seeded with its own fixtures answers 404 or `null` to each, and every detail route reads as
  a breaking `status` change. `--follow-ids` works out, from the recording alone, which
  earlier read's answer held each id: the `GET /api/orders` whose `$.orders[1].id` was 81723.
  It sends that read to ORIGIN first and sends the id ORIGIN's answer holds at the same
  place, or at the same place in the first item when ORIGIN's list is shorter. Ids are taken
  from templated path segments, id-named query parameters (`userId`, `order_id`) and id-named
  JSON body fields such as GraphQL variables (`$.variables.id`); an answer field counts only
  under an id-named key (`id`, `ids`, `uuid`, `...Id`, `..._id`), never a secret-named one.
  When the same id is in several answers, as small seeded ids often are, it is taken from the
  bare id of what it names (`$.products[1].id`, or the `$.id` of `/api/products/42`, for
  `/api/products/{products_id}`), then from a read that sends fewer ids of its own, then from
  a key named for it (`user_id` for `?userId=`) before a key named for something else
  (`productId`), whatever the order of the recording. Only reads are followed, since writes are never sent, and a read whose id ORIGIN does not
  answer (the source failed, holds no value there, or holds one that is not a plain id) is
  never sent with a guessed or recorded id: its route is listed under `skipped` as `id not
  found on target`. A rewritten request must still be a read of the same route, so a value
  such as `../admin` or `delete-all` is never sent, and values from ORIGIN go only back to
  ORIGIN. The report gains `followed`, one row per traced id with the route, where the id is
  sent (`path`, `query` or `body`), its name, the route it came from and the field, never a
  value. Without the flag, a run where such a read answers 404 or 410 says so on stderr; with
  it, a baseline written by an older rebrowse, which keeps no ids in answers, gets a note to
  write it again.
- **Judging.** Each request is sent once, and its live answer is compared with every recorded
  answer to that same request (method, URL and body), so the order of the recording never
  changes the verdict: a list loaded before and after a create is judged against the fields of
  both answers. Answers are compared route by route with the breaking rules of
  [`diff`](#drift-report) (`content_type`, `body_empty`, `field_removed`, `field_optional`,
  `field_type`). Statuses are judged per request: one recorded with a 2xx or 304 that now
  answers with a redirect, a client error or a server error, or one recorded only below 500
  that now answers with a 5xx, is a breaking `status` change for its route, even when other
  requests to that route still answer. A status the recording already holds for the same
  request never breaks, so `/api/me` recorded as 401 before sign-in and 200 after passes when
  ORIGIN answers 401, except a 5xx: a request recorded as a 500 and then, retried, as a 200
  fails when ORIGIN answers 500. Other differences in a route's statuses are reported as info. A replay
  that gets no answer, such as a timeout or a refused connection, is a
  breaking `no_response`, and so is any replayed route whose answer is no longer an API
  response, such as a JSON route at `/` answered with an HTML page. A
  challenge page is a breaking `blocked`, never data. A failed replay is left out of the
  comparison, so its route is reported once, by the failure. When ORIGIN answers none of the
  requests, the run exits 2, which tells "the server did not start" apart from "the API
  broke".
- **Refused runs.** The run also exits 2, with no report, when ORIGIN refuses (401, 403) or
  redirects (a 3xx other than 304) every request the recording answered with a 2xx or 304. A
  refusal the recording already holds for a request, such as `/api/me` recorded as 401 before
  sign-in, neither counts nor hides the others. Missing or expired credentials, a secret from
  another environment, or an http ORIGIN that redirects to https then give one error, such as
  `http://api:8000 refused or redirected every read the recording answered (302 x5)`, naming
  the `--header-env` headers and their variables, or the `auth set` key that was sent, instead
  of dozens of breaking changes, and `--update-accepted` never writes. When only some requests
  are refused, each one is still a breaking `status` change, and `--update-accepted` leaves
  such changes for you to add by hand, so a half-expired session cannot be accepted either.
- **Caveats.** Without `--follow-ids`, the recorded ids must exist on ORIGIN, otherwise
  `/api/orders/1001` answers 404 and is reported as a breaking status change. With it, an id
  that no replayed read's answer held, such as one in the page URL, a header, a write's answer
  or a GraphQL literal (`user(id: 1001)`), is still sent as recorded. A field counts as
  required when every replayed recording carries it, so an optional field that ORIGIN leaves
  out for differently seeded data is reported as removed.
  Only reads are ever replayed, so changes to writes go untested. Secret-named values are
  sent redacted, so a call that needs one (`pageToken`, a `sessionId` variable) usually
  answers 400 and is reported as broken.

The report is JSON on stdout: the source, domain and target; the `--header-env` header
names, as `credentials`, when any were given; how many routes were replayed,
how many requests were sent and answered; with `--follow-ids`, the ids followed; the skipped
routes; the number of breaking changes; and the changes ordered by route. Like `diff`, it
holds route names, statuses, media types, field paths and JSON type names, plus transport
error text, never a recorded or live value:

```json
{"severity": "breaking", "kind": "no_response", "route": "GET /api/summary",
 "error": "ReadTimeout: timed out", "failed": 1}
```

## Accepted changes

Some breaking changes are intended, such as a backend pull request that renames `$.total`
while the frontend change ships alongside it. `--accepted FILE` on `diff` and `contract` reads
a committed JSON list of the breaking changes a team has reviewed. A change that matches an
entry keeps its place in `changes` with `"severity": "accepted"` (and the entry's `reason`)
and no longer counts toward `breaking` or the exit code; any other break still fails the run.

```bash
# on the pull request: accept this run's breaks, add a reason to each entry, commit the file
rebrowse contract api-baseline.json --against http://localhost:8000 \
  --accepted contract-accepted.json --update-accepted
# CI reads it and never rewrites it
rebrowse contract api-baseline.json --against http://api:8000 --accepted contract-accepted.json
```

```json
[
  {"kind": "field_removed", "route": "GET /api/orders/{orders_id}", "field": "$.total",
   "base": ["integer"], "head": null, "reason": "renamed to totalCents in #412"},
  {"kind": "field_type", "route": "GET /api/users/{users_id}"}
]
```

- **Entries.** `kind` (`status`, `content_type`, `body_empty`, `field_removed`,
  `field_optional` or `field_type`) and `route` are required; `field`, `base`, `head` and
  `reason` are optional. `severity` is ignored, so a change copied from a report works as it
  is. Any other key is an error, which catches typos such as `feild`.
- **Matching.** `kind` and `route` must be equal, and so must each of `field`, `base` and
  `head` that the entry gives. Leaving out `field` accepts every field change of that kind on
  the route; leaving out `base` and `head` accepts the change whatever the new statuses or
  types are. Only breaking changes are matched.
- **What cannot be accepted.** Replay failures (`no_response`, `blocked`) are not API changes:
  a timeout or a challenge page must never turn into a pass, and neither can a status change
  to a server error (`"head": [503]`), which is an outage answered as JSON. A broad `status`
  entry accepts 3xx and 4xx changes (a removed endpoint, a 410, a new sign-in redirect) but
  never a 5xx. A `field` on `status`, `content_type` or `body_empty` is an error too, since
  those change the whole route. Info changes, such as
  `route_added`, `route_missing` or an added field, never fail a run and need no entry. An
  entry of any of these kinds makes the file invalid.
- **Stale entries.** An entry that matched no breaking change on a route the run compared,
  or that names a route the run never saw, is listed under `stale` and counted in a warning
  on stderr, so the file cannot rot into a blanket ignore list. It usually means the
  recording caught up, for example after main's baseline was re-recorded: delete the entry,
  or rerun with `--update-accepted`.
- **Unchecked entries.** An entry on a route the run saw but could not compare is listed
  under `unchecked` instead, never as stale: a route only one recording has in `diff`, or one
  that `contract` skipped (a write, a sibling host, ...) or failed to replay.
- **`--update-accepted`.** Rewrites FILE from the run, creating it if needed: entries that
  still match are kept as written, reasons included, and so are unchecked ones; stale ones
  are dropped; each breaking change that no entry matches is appended as its `kind`,
  `route`, `field`, `base` and `head`. The file holds route names, field paths, statuses,
  media types and JSON type names, never a recorded value, and a rerun writes the same
  bytes. A run that exits 2 never writes it, and a run in which a route failed to replay or
  answered with a 5xx leaves it untouched, so a server that is still starting or partly
  down cannot rewrite the
  reviewed file.
- **One file per check.** Give `diff` and `contract`, and each baseline, a file of its own.
  A run judges only the routes it compares, so an entry another check needs can be stale
  here, and `--update-accepted` would drop it.

With `--accepted`, the report also holds `accepted` (how many changes were accepted),
`stale` and `unchecked` (the entries), and `breaking` counts the unaccepted ones only. Exit
codes keep their meaning. An invalid file, or a missing one without `--update-accepted`,
exits 2 before `contract` sends a request.

## Committable baselines

`diff` and `contract` need a recording of how the API answered before a change, and a raw
recording is not safe to keep. A HAR from Playwright, DevTools or a proxy holds the session's
`Cookie`, `Authorization`, `Set-Cookie` and CSRF headers, tokens in URLs, every response value
(names, emails, addresses, order totals) and megabytes of bundles and HTML; a capture saved by
`build` keeps request credentials and `Set-Cookie` too. Committed, or kept as a CI artifact
that pull-request jobs can read, it leaks a live session and customer data.
`rebrowse baseline SOURCE` writes only what `diff`, `contract` and `mock` judge, as a capture
file that each of them reads as SOURCE:

```bash
rebrowse baseline e2e.har -o api-baseline.json   # commit this file
rebrowse diff api-baseline.json e2e.har [--accepted diff-accepted.json]
rebrowse contract api-baseline.json --against http://api:8000
rebrowse mock api-baseline.json
```

SOURCE is read as `mock` reads it, and `--domain` picks the site in a HAR. Without `-o` the
baseline is written to stdout; with it, FILE is replaced in one step and a summary is printed:
the source, domain, path, the number of routes, and how many requests were read and written.
Nothing is sent over the network.

- **What is kept.** The calls `diff` compares: same-site API calls, sibling hosts included,
  with redirects and 304s, and their method, URL, status and `content-type`. Request paths,
  and the query values and bodies of reads, stay as recorded, because `contract` replays them,
  so ids, search terms and GraphQL variables are in the file: review it before the first
  commit. An HTML page other than the site root is a route for `diff` (a deep link such as
  `/app/dashboard?tab=billing`), so it is kept like any read, with its query values. The query
  values of writes are emptied, except those that name or classify the call: the GraphQL
  `operationName`, document and `extensions`, and `action`, `cmd`, `op` or `_method`.
  The GraphQL document of a write without an `operationName` or persisted hash keeps its
  literals too, apart from secret-named arguments, because its text names the route, so an
  inline `createUser(email: "...")` stays: name the operation to have its literals emptied.
  Request headers are the ones `contract` would send, such as
  `accept` and `content-type`. Response bodies keep their structure and field names, the same
  exposure as a `diff` report.
- **What is removed.** Everything `contract` never sends: cookies, `Authorization`, API-key,
  CSRF and session headers, `Origin`, `Referer`, tracing headers (`traceparent`, `baggage`,
  `sentry-trace`, `X-Request-Id`, ...) and headers whose value looks like a credential, user
  info, fragments, jQuery's `_=<timestamp>` cache-buster and secret-named path parameters such
  as `;jsessionid=`. Secret-named query values and query values that hold a token (`Bearer
  ...`, a JWT) become `<redacted>`, as `contract` sends them, and calls with a credential in
  the path, matrix parameters included, are dropped. The recorded `final_url` becomes the site
  root, such as `https://app.example.com/`, since the page a run opens first is not part of
  the API and would change the file whenever tests are renamed or reordered. Secret-named values in read bodies, including
  secret-named arguments in a GraphQL document (`login(password: "...")`), are redacted as
  `contract` redacts them. Every response header but `content-type`, so `Set-Cookie` and
  `Location`, is dropped. Every response value becomes a placeholder of its JSON type (`""`,
  `0`, `0.5`, `false`, `null`), except, in the answer to a read, a value that a kept read
  sends as an id (a templated path segment, an id-named query value or JSON body field) and
  that sits under an id-named key that is not secret-named, where `contract --follow-ids`
  looks: `$.items[1].id` keeps `1002` when `/api/items/1002` is in the file, while a `count`
  or `total` of 1002, or an id under a key that `diff` reads as a value, stays a placeholder.
  That value is already in the file, in the request that sends it; only its place is added,
  and answers that differ only in other values give the same file. Answers to writes keep
  no value. Keys that `diff` reads as values (ids, emails,
  dates, tokens) become `"0"`, `"1"`, ...; HTML, XML, text and JSONP bodies are dropped.
  Write bodies keep only the `operationName` and `query` strings and the `extensions` object
  of each GraphQL operation, with secret-named values redacted, and turn every other JSON
  value into a placeholder; a form-encoded GraphQL write keeps the same three fields, and
  other form, multipart and XML write bodies are dropped. In the document of a named or
  persisted write, every number becomes `0` and every string and comment keeps only the words
  that classify the call, a destructive verb or `mutation` (`bulk(action: "delete", note: "")`),
  so its effect is the same as recorded. Calls that `diff` does not compare, such as the site
  root page, static files, JS bundles, telemetry and other sites, are left out, as are the
  HAR's cookie lists and timestamps, so the file is a few KB.
- **Same verdicts.** `diff` reports exactly the same changes against a baseline as against its
  recording, on either side: a placeholder has the JSON type `diff` reads (`4.0` stays an
  integer, `1.5` a number), and identical array items and map entries are merged, which never
  changes whether a field is in every response. `contract` judges each request against all of
  its recorded answers, whatever their order, so it sends the same requests and reaches the
  same judgement whenever no route has more than three distinct recorded reads; past that it
  replays the first three in the baseline's sorted order. `mock` serves the placeholders with
  the recorded status and `content-type`, and a list's kept ids match the detail routes it
  serves.
- **Stable.** Requests are deduplicated and sorted, as are keys and array items, and no time,
  trace id or cache-buster is written, so recording the same calls against an unchanged API
  gives the same bytes and the pull-request diff of `api-baseline.json` reads as the API
  change, such as `"total":0` becoming `"total":""`. A baseline of a baseline is the same file.

## Coverage

Docs, mocks, baselines and contract runs cover only the calls a recording happened to make.
`rebrowse coverage SOURCE` checks the recording against the frontend's own JS and lists the API
calls the JS can make that the recording never exercised, writes included. SOURCE is read as
`mock` reads it: a HAR file or a directory of them, a capture saved by `build`, or a
`host[:port]` whose newest saved capture is used (`--domain` picks the site in a HAR).

```bash
rebrowse coverage test-results/                   # what the whole e2e suite never called
rebrowse coverage test-results/ --fail-under 80   # CI: exit 1 when coverage is below 80%
```

The JSON report gives the number of `bundles` scanned, the API routes and GraphQL operations
they reference (`referenced`), and the percentage of those that were recorded (`coverage`).
Each `unrecorded` reference carries its `effect` (`read`, `write` or `destructive`), how it was
found (`found_by`) and its bundle URL without the query. When the matching calls got only
errors or redirects, it also lists their `statuses`. Each `covered` reference names the
recorded routes that answered it (`recorded_as`). `unreferenced` counts recorded API routes
that no reference matched, such as URLs built at runtime, which shows how much of the recording
the scan could not see. Routes are sorted by path then method and operations by name, so the
same input gives the same bytes.

- **What is scanned.** The same-site JS bundles in SOURCE, without ad, embed and analytics
  scripts, as `build` scans them: `fetch`, `axios` and `.get/.post/.put/.patch/.delete` calls,
  `/api/...` and `/vN/...` strings, and template literals (`` `/api/users/${id}` `` becomes
  `/api/users/{id}`). A call keeps its method whether its path is a string or a template
  literal, so `` api.delete(`/api/items/${id}`) `` is `DELETE /api/items/{id}`. The recording
  adds prefixes. The first segment of every recorded route answered with JSON is searched
  too, so a recorded `GET /rest/orders` lets the scan find `"/rest/invoices/" + id`. An HTML
  page such as `/en/home` adds no prefix. Call and prefix paths may contain dots, so legacy
  endpoints such as `/ajax/get_cart.php` and `/services/OrderService.asmx/GetOrders` are
  found, while static files (`.js`, `.json`, `.css`, images) are not. GraphQL operations come
  from source documents (`` gql`query GetCart($id: ID!) {` ``, a minified
  `"\nmutation RemoveItem{"`) and from precompiled ASTs
  (`operation:"mutation",name:{kind:"Name",value:"Checkout"}`). Fragments and text such as
  `"query failed ("` or `` `query failed (${status})` `` are not operations. Telemetry paths
  are dropped.
- **Every script in a HAR.** coverage reads every same-site script in a HAR, or in every HAR
  of a directory, whatever their number or size. Run it on the whole suite's folder, since
  one test's HAR exercises only a sliver of the routes a shared bundle references. A capture saved by `build` keeps at most 20 scripts under 2 MB each, so for
  a large app, or as a CI gate, run coverage on a HAR.
- **Matching.** Hosts are ignored, because bundle paths are relative to the origin. A reference
  matches the end of a recorded route, so an axios `baseURL` still lines up:
  `api.get("/orders/summary")` matches `GET /api/v2/orders/summary`, and `"/v1/items"`
  matches a call to `api.app.test/v1/items`. A placeholder matches any segment, but a literal
  matches only itself, so `/api/users/me` is not covered by `/api/users/7`. A literal that
  looks like an id (two or more digits, a long hex string or a UUID) matches a recorded id, as
  rebrowse templates both alike: `fetch("/api/reports/2024")` is covered by a recording of
  `/api/reports/2023`. A path that ends in `/` (`"/api/users/" + id`) matches any recorded
  route that continues past it. A plain string or `fetch()` is taken as a GET and matches any
  method. An explicit `.post`, `.put`, `.patch` or `.delete` needs a recording with that
  method. A GraphQL operation matches recorded calls with the same operation name.
- **Covered** means a matching call was answered with a 2xx or 304. A route recorded only
  with a 401, a redirect to sign-in or a 500 stays unrecorded and shows those statuses.
- **CI gate.** `--fail-under PERCENT` exits 1 when coverage is below PERCENT. The full report
  is still printed, and stderr gets a note. Bad input exits 2: a SOURCE that cannot be read or
  is unknown, no first-party JS, or JS that references nothing rebrowse can find.
- **Baselines keep no JS.** A file written by `baseline`, or a HAR saved without content, has
  no bundles and exits 2. Run coverage on the HAR or capture the baseline was written from, or
  on a DevTools "Save all as HAR with content" export.
- **Offline.** coverage reads only SOURCE. It sends nothing over the network, calls no LLM and
  never prints bundle source.
- **Blind spots.** It cannot see URLs assembled from runtime config, relative paths without a
  leading slash, Relay or persisted-only GraphQL documents, or lazy-loaded chunks the recording
  never loaded. A path outside `/api/`, `/vN/` and the recorded prefixes is found only as the
  argument of a call such as `fetch("/x/y")` or `` axios.post(`/x/${id}`) ``. A method set in
  the options of `fetch(url, {method: "DELETE"})` is not read, so that call is taken as a GET.
  Query parameters, body fields, statuses and schema fields are not compared.

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
