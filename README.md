# rebrowse

Learn a web app's undocumented API from its own traffic, then use it.

rebrowse watches a site load in a browser, or reads the traffic you already have (HAR files
from DevTools, a proxy or a Playwright e2e suite), and works out the API behind it. From that
it gives you:

- **Skills** that an LLM can call from a plain-English request, on the CLI or over MCP.
- **OpenAPI 3.1 docs**, byte-stable, so a committed `api.json` diff reads as an API changelog.
- **A local mock** of the backend for the UI, Storybook or an e2e suite.
- **A committable baseline**: the recording with every credential and response value removed.
- **Breaking-change checks** for CI: `diff` between two recordings, and `contract`, which
  replays the frontend's reads against a changed backend.
- **Coverage**: the API calls the frontend's JS can make that no recording exercised.

Everything that works from a recording runs offline, with no LLM, and never prints or stores
a credential or a response value.

Private project — no license; all rights reserved.

## Commands

| Command | What it does |
|---|---|
| `build <url>` | Capture a site in headless Chromium and save its API as a skill |
| `import-har <file\|dir>` | Build a skill from HAR files, without a browser |
| `run "<request>"` | Pick the saved endpoint that matches a request and call it |
| `verify <skill>` | Re-check a skill's read endpoints and record which still work |
| `skills`, `show`, `delete` | List, search, print and delete saved skills |
| `auth set\|list\|remove` | Manage API keys, each sent only to its own site |
| `mcp` | Serve skills to agents over MCP (stdio) |
| `openapi <source\|skill>` | Export a recording or a skill as an OpenAPI 3.1 document |
| `mock <source>` | Serve recorded responses on 127.0.0.1 |
| `baseline <source>` | Write a recording that is safe to commit |
| `diff <base> <head>` | Report API changes between two recordings that break the client |
| `contract <source> --against <origin>` | Replay recorded reads against a server, report breaking changes |
| `coverage <source>` | List API calls in the frontend's JS that a recording never made |

Commands print JSON to stdout, progress and notes to stderr, and exit non-zero on errors.

## Setup

```bash
python -m venv .venv && .venv\Scripts\activate      # or: source .venv/bin/activate
pip install -e ".[dev,mcp]"
playwright install chromium
cp .env.example .env                                 # then pick an LLM provider
```

Requires Python 3.11+. Data lives in `~/.rebrowse/` (override with `REBROWSE_DATA_DIR`):
`skills.db` (skills, no secrets), `vault/` (encrypted API keys and cookies) and `captures/`
(raw traffic from `build`).

Only `build`, `import-har` (endpoint descriptions) and `run` use an LLM. Set it in `.env`:

| `LLM_PROVIDER` | Notes |
|---|---|
| `local` (default) | LM Studio / vLLM at `LLM_BASE_URL` (default `http://localhost:1234/v1`) |
| `ollama` | `http://localhost:11434/v1` |
| `openai` / `gemini` / `claude` | set `LLM_API_KEY` |

`LLM_MODEL_INTENT` parses requests and picks endpoints; `LLM_MODEL_CODE` describes endpoints.
rebrowse refuses to send an API key over plain HTTP to a non-local host, and warns once when
prompts would leave the machine unencrypted. An LLM outage never fails a build: endpoints are
saved without descriptions and get them on a later run.

## Quick start

### Call a site's API

```bash
rebrowse build https://news.ycombinator.com
rebrowse build https://example.com --steps "type #q=shoes; click #search; wait 800"
rebrowse run "top stories on hacker news"
rebrowse run "add a note saying hi" --dry-run   # show the resolved call, send nothing
rebrowse run "add a note saying hi" --yes       # confirm a call that changes state
rebrowse verify news.ycombinator.com            # health-check the read endpoints
```

### Turn an e2e suite into docs, a mock and CI checks

```bash
# record one HAR per test (see Recordings), then on main:
rebrowse baseline test-results/ -o api-baseline.json        # commit this file, never the HARs
rebrowse openapi api-baseline.json -o docs/openapi.json     # docs, same bytes every run
rebrowse mock api-baseline.json                             # backend for the UI or Storybook

# frontend pull request: did the API the frontend sees change?
rebrowse diff api-baseline.json test-results/ --accepted diff-accepted.json
rebrowse coverage test-results/ --fail-under 80

# backend pull request: does the changed backend still answer the frontend's calls?
rebrowse contract api-baseline.json --against http://api:8000 --follow-ids \
  --header-env Cookie=CI_SESSION --accepted contract-accepted.json
```

## Recordings

Every command that reads traffic (`openapi`, `mock`, `baseline`, `diff`, `contract`,
`coverage`, `import-har`) takes the same kinds of SOURCE:

- **A HAR file.** DevTools "Save all as HAR" (Chrome, Firefox), mitmproxy, Charles, Proxyman
  or Playwright's `recordHar`.
- **A Playwright HAR archive.** `recordHar: { path: 'network.zip' }` writes a `.zip` holding
  `har.har` and one file per body; it is accepted wherever a HAR file is.
- **A HAR with attached bodies.** `page.routeFromHAR('hars/app.har', { update: true })` and
  `recordHar: { content: 'attach' }` write each body to its own file next to the HAR
  (`<sha1>.json`), named in `_file`. rebrowse reads them, preferring the file to embedded text
  as Playwright's replay does. Keep the files with the HAR: a JSON, text, request or script
  body whose file is missing is an error naming both, and a `_file` that is not a plain file
  in the HAR's own folder (a path, a symlink) is refused.
- **A directory** of any of these, read as one recording (below).
- **A capture saved by `build`**, or a baseline written by `baseline` (`.json`).
- **A `host[:port]`**, meaning that host's newest capture saved by `build`.

`--domain host[:port]` picks the site to read, as a bare host, `host:port` or a URL, in any
case. The default is the host of the first HTML page. Calls to sibling hosts of the same site,
such as `api.app.com` next to `www.app.com`, are kept and named with their host; `--domain`
can also move a saved capture to a sibling host. Other sites, telemetry, ads and static files
are dropped.

**What reading strips.** Credentials go as a file is read, before anything is stored, served
or sent to an LLM: `Cookie`, `Set-Cookie`, `Authorization`, API-key, CSRF and session headers,
headers whose value looks like a credential (`Bearer ...`, a JWT), the HAR's cookie lists, user
info in URLs, secret-named path parameters such as `;jsessionid=`, and OAuth `code` / CAS
`ticket` values. Query, JSON and form values under secret-looking names (`password`, `token`,
`api_key`, `session`, `csrf`, SAML assertions, ...) become `<redacted>`, also inside
JSON-encoded values such as GraphQL `variables` and in secret-named GraphQL arguments. A body
that parses as JSON is treated as JSON whatever its declared type; multipart, XML and binary
request bodies are dropped, since they cannot be redacted.

### Many recordings

An e2e suite rarely writes one HAR: @playwright/test gives each test its own context, parallel
workers write their own files, Cypress plugins write one per spec. Point any command at the
folder instead:

```bash
rebrowse baseline test-results/ -o api-baseline.json
rebrowse diff main-hars/ pr-hars/
```

- **What is read.** Every `.har` file (any case, any depth) and every `.zip` holding a
  `har.har`. Playwright's `trace.zip` and other zips are skipped, as are captures and
  baselines, dot folders, `node_modules`, `__MACOSX`, AppleDouble `._*.har` files, directory
  symlinks and junctions, so pointing rebrowse at a repo root reads only your recordings.
- **One recording, same bytes.** Files are read in the order of their `/`-separated path, the
  same on every OS, one at a time (memory holds one raw HAR). Requests keep file order, a
  script in several files is read once, and the site is chosen once over all files. `baseline`
  and `openapi` sort and deduplicate, so a folder gives the same bytes as one HAR holding the
  same calls, whatever the files are called.
- **Errors, never a smaller recording.** A folder with no HAR, a `.har` or archive that is not
  valid JSON or HAR (cut short by a crashed test), an empty or damaged `.zip`, missing body
  files and Git LFS pointers (`run git lfs pull`) are input errors naming the file, and nothing
  is written. An empty SOURCE is an error too, never the current directory.
- stderr gets one line, such as `[baseline] read 14 HAR files under test-results`.

A fixture that records each test into its own folder (`network.zip` writes an archive):

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

## Skills

A skill is a site's endpoints, saved in `skills.db`: method, URL template, parameters, request
template, inferred response schema, effect (`read`, `write` or `destructive`), an LLM
description and its `verify` health.

**`build <url>`** opens the page in headless Chromium and records every response for the load,
three scrolls and any `--steps` (`type`, `click`, `wait`). It then:

- keeps same-site API calls, dropping static files, telemetry (segment-aware, so `/api/login`
  survives while `/log` doesn't), ads and other sites;
- templatizes ids (`/users/123` → `/users/{users_id}`), deduplicates, and splits GraphQL into
  one endpoint per operation, persisted queries and GET `?query=` included;
- scans first-party JS bundles for routes the page never called, taking the method from the
  call site (`axios.post(...)` → POST, `` api.delete(`/items/${id}`) `` → DELETE);
- infers response schemas over every sample (fields seen in all samples are required);
- saves the raw capture to `captures/`, so it can be re-read without browsing again.

**`import-har <file|dir>`** builds the same kind of skill from [recordings](#recordings),
without a browser or a single request, so it can learn the parts of an app behind a sign-in:
sign in yourself, use the app, export the HAR. Response bodies only shape schemas, and nothing
is copied to `captures/`; import again to re-extract.

**Re-learning keeps what didn't change.** Building or importing a site again updates its skill.
An endpoint seen again (same method, URL template and GraphQL operation) keeps its id, its
description and its `verify` result, so ids handed to agents stay valid; only endpoints without
a description go to the LLM, at most 25 per run. The result is reset when an endpoint's effect
changed (a read that is now a write) or it moved between observed traffic and a bundle-only
route. Endpoints the new recording did not see are dropped. The output lists `changes`
(`added`, `kept`, `dropped`, `described`), each endpoint's `change`, the `dropped` ones and any
`effect_changes`. To get fresh descriptions, `delete` the skill and build again.

**`run "<request>"`.** The LLM extracts the site and intent, the skill is found by domain alias
or semantic search (below 0.25 similarity it refuses rather than guesses), and the LLM picks one
endpoint among the healthy ones. Captured values never reach the picker, only parameter names,
and the endpoint list is marked as untrusted data. Anything but a read needs `--yes`.

**`verify <skill>`** re-sends read endpoints only and records `verified` / `failed` with a
reliability score; `run` skips failed endpoints and ranks the rest. A read redirected to an HTML
sign-in page is `auth_required`, not verified.

**Skills and keys.** `skills [-q "search"]`, `show <id>` and `delete <id>` manage skills.
`auth set <domain> <key> [--type bearer|header|query]` stores an API key in the encrypted vault,
sent as `Authorization: Bearer`, `X-API-Key` or `?api_key=` only to that domain and its
same-site hosts; `auth list` and `auth remove` manage them. rebrowse never imports cookies from
a HAR, so `run` and `verify` cannot replay an app that signs in with a session cookie;
`contract` can, with `--header-env`.

**MCP.** `rebrowse mcp` serves skills over stdio:

```json
{ "mcpServers": { "rebrowse": { "command": "rebrowse", "args": ["mcp"] } } }
```

Tools: `search_skills` and `list_operations` (read-only), `read` (executes reads only) and `act`
(write or destructive; returns `confirmation_required` until called with `confirm=true`).
Effects map onto MCP's `readOnlyHint` and `destructiveHint`.

## OpenAPI export

`rebrowse openapi TARGET [-o FILE]` writes an OpenAPI 3.1 JSON document that Swagger UI or
Redoc can render, Prism can mock and Schemathesis can test. Every operation carries
`x-rebrowse-effect` (`read` / `write` / `destructive`), so a test run can be limited to reads.
`info.version` is a hash of the rest of the document, not a time, so it changes exactly when
the document does.

### From a recording

When TARGET is a [recording](#recordings) (a file or a folder), the document is built offline
from its traffic, with no LLM, skill or `skills.db`:

```bash
rebrowse openapi session.har -o api.json
rebrowse openapi api-baseline.json -o docs/openapi.json && git diff --exit-code docs/openapi.json
```

- **Same bytes.** The recording is first reduced to what `baseline` keeps, so a HAR, a folder
  and the baseline written from them give byte-identical documents, as does a new recording of
  the same calls against an unchanged API.
- **Operations** are the routes `diff` compares, redirects and 304s included. A path recorded on
  a sibling host lists its origins under `servers`. GraphQL operations sharing a URL are one
  operation, named in `x-rebrowse-graphql`, with one request example per operation. Query and
  header parameters are required when every recorded call sent them.
- **Responses.** Each recorded status is its own response, so a route that answered 200, 404 and
  a 302 to sign-in documents all three; a status outside 100–599 is `default`. 204, 205, 304
  and responses without a content type have no content; a non-JSON body is listed by media type
  alone.
- **Schemas as `diff` reads them**, down to 12 levels: a field is `required` when every object
  recorded at its place had it, the rule by which `diff` and `contract` call its removal
  breaking. `integer` folds into `number` when both were seen, a field seen as a string and as
  null is `["null", "string"]`, and keys that hold values (ids, emails, dates, tokens) are
  `additionalProperties`, never property names.
- **No values.** Responses carry schemas only. Request examples are the baseline's: read bodies
  keep their values (redacted), write bodies hold placeholders. A JSON body sent as `text/plain`
  gets a schema like any JSON body.

### From a skill

Any other TARGET is a skill id or domain (a recording wins over a skill of the same name). The
export adds the LLM descriptions, routes found only in JS bundles (`x-rebrowse-observed:
false`) and `verify` health (`x-rebrowse-verification`). It is byte-stable within one data
directory, since re-learning keeps descriptions; a fresh `~/.rebrowse` gets a new skill id and
new descriptions, so in CI document the committed baseline instead, or cache only
`$REBROWSE_DATA_DIR/skills.db`, never `vault/` or `captures/`.

## Mock server

`rebrowse mock SOURCE [-p PORT]` answers a frontend's API calls from a recording, so the UI,
Storybook or an e2e suite runs without the real backend. Point the app's API base URL, or its
dev-server proxy, at the printed `url` (default `http://127.0.0.1:8787`; `-p 0` picks a free
port). The route table is printed at startup and each request is logged to stderr.

```bash
rebrowse mock api-baseline.json
rebrowse mock hars/       # the HARs your Playwright tests already replay with routeFromHAR
```

- **Matching.** An unseen id is answered from its template (`/api/users/999` gets the recorded
  `/api/users/1001`), and a literal route beats a template (`/users/me` over `/users/{id}`).
  Each GraphQL operation is its own route, persisted queries and GET `?query=` included (a batch
  matches only the same operations in the same order). Within a route the closest recording
  wins: one with a body, same path, most shared query pairs and top-level body fields (so an RPC
  endpoint's `action=save_post` gets the `save_post` recording), same body, then a 2xx over an
  error. Responses carry `x-rebrowse-mock: exact|nearest` and `x-rebrowse-route`; anything else
  is a 404 marked `miss` listing the recorded routes, and an unrecorded GraphQL operation is a
  miss, never another operation's data.
- **What is served.** The recorded status, body (as UTF-8) and `content-type`, and no other
  header, so no `Set-Cookie`. JSON values under secret-looking keys are `<redacted>`, also behind
  a BOM, an XSSI guard or a JSONP callback, and form-encoded bodies pair by pair.
- **Never the real site.** The mock has no HTTP client and no state: writes get their recorded
  response and nothing is forwarded.
- **Local only.** It binds 127.0.0.1, refuses non-loopback `Host` headers (DNS rebinding),
  answers CORS only for `localhost`, `127.0.0.1` and `[::1]` origins, and refuses cross-site
  requests from other pages.

## Baselines

A raw recording is not safe to keep: a HAR holds the session's cookies and tokens, every
response value (names, emails, totals) and megabytes of bundles. `rebrowse baseline SOURCE -o
api-baseline.json` writes only what `diff`, `contract` and `mock` judge, as a few-KB capture
file that each of them reads as SOURCE. Commit it, never the HARs.

- **What is kept.** The calls `diff` compares (same-site API calls, sibling hosts, redirects and
  304s) with their method, URL, status and `content-type`. Request paths, and the query values
  and bodies of reads, stay as recorded, because `contract` replays them, so review the file
  before the first commit. Response bodies keep their structure and field names.
- **What is removed.** Everything `contract` never sends: credential, tracing and browser
  headers, cache-busters, secret-named values and token-valued query values (`<redacted>`),
  calls with a credential in the path. Every response value becomes a placeholder of its JSON
  type (`""`, `0`, `0.5`, `false`, `null`), except an id that a kept read sends, under an
  id-named key, which `contract --follow-ids` needs; that value is already in the file. Map keys
  that hold values become `"0"`, `"1"`, ...; HTML, XML, text and JSONP bodies are dropped. Write
  bodies keep only their GraphQL `operationName`, `query` and `extensions`, and write query
  values are emptied except those that name or classify the call. The page URL is the site root.
- **Same verdicts.** `diff` reports exactly the same changes against a baseline as against its
  recording, and `contract` sends the same requests whenever no route has more than three
  distinct recorded reads.
- **Stable.** Requests, keys and array items are sorted and deduplicated, and no time, trace id
  or cache-buster is written, so the same calls give the same bytes, the pull-request diff of
  the file reads as the API change, and a baseline of a baseline is the same file.

## Drift report

`rebrowse diff BASE HEAD` compares two recordings of the same app, such as the committed
baseline and this pull request's e2e run, and reports what changed from the client's point of
view, with no spec, LLM or network. Exit 0: nothing breaks; 1: a breaking change; 2: bad input,
including a side with no API traffic (a run that never got past sign-in).

```bash
rebrowse diff api-baseline.json test-results/
rebrowse diff prod.har staging.har -d prod.app.com -d staging.app.com   # --domain per side
```

- **Breaking.** A route that answered 2xx and no longer does (now a redirect to sign-in, a new
  5xx); a 2xx with a media type BASE never returned, such as JSON turned HTML (`content_type`);
  JSON turned empty or 204 (`body_empty`); a field present in every BASE response that is gone
  (`field_removed`) or missing from some HEAD responses (`field_optional`); a new JSON type,
  including a new `null` (`field_type`). `number` where BASE had `integer` is info, since
  JavaScript cannot tell them apart.
- **Info.** Other status changes, added fields, removed fields BASE did not always send, and
  routes on one side only (`route_added`, `route_missing`).
- **Routes and fields.** Ids are templated (`/api/users/1001` and `/42` are one route), each
  GraphQL operation and sibling host is its own route, and a 304 counts as an answer. Fields are
  read from 2xx JSON bodies, behind an XSSI guard too, to 12 levels; map keys that hold values
  (ids, emails, dates, `ghp_...` tokens) are compared together under `.*`.

The report holds each side's source, domain and route counts, the breaking count and the
changes, as route names, statuses, media types, field paths and JSON types, never a value:

```json
{"severity": "breaking", "kind": "field_type", "route": "GET /api/orders/{orders_id}",
 "field": "$.total", "base": ["string"], "head": ["null", "string"]}
```

## Contract tests

`rebrowse contract SOURCE --against ORIGIN` answers a backend pull request's question: does the
changed backend still answer the calls the frontend makes? It replays the reads recorded in
SOURCE against ORIGIN (`scheme://host[:port]`) and judges the answers with `diff`'s rules, so
the frontend's own traffic is the contract, with no hand-written tests. Exit 0: compatible; 1:
a breaking change or a failed replay; 2: bad input, no answer at all, or every read refused.

```bash
docker compose up -d --wait api
REBROWSE_HOST_INTERVAL=0 rebrowse contract api-baseline.json --against http://api:8000 --follow-ids
```

- **Only reads are sent.** GETs, and GraphQL queries POSTed with their query text. A GraphQL
  document that defines a mutation anywhere, or a query parameter such as `action=delete` or
  `_method=PUT`, makes a call a write. Writes, 304-only routes, event streams and calls with a
  credential in the path are listed under `skipped`, never sent. Each route gets at most three
  distinct requests, one at a time, without retries or following redirects (a route that now
  redirects to `/login` is compared as a 302), each within one deadline and size limit.
  Requests to hosts other than localhost are paced (`REBROWSE_HOST_INTERVAL`).
- **What is not sent.** Recorded cookies, credential, CSRF, tracing, conditional and
  method-override headers, `Origin`, `Referer`, cache-busters and secret-named values; the
  User-Agent is rebrowse's own. Keys stored for the recorded site never go to ORIGIN.
- **Credentials.** A backend behind a sign-in answers every replay with a 302 or 401.
  `--header-env NAME=ENVVAR` sends header NAME with the value of environment variable ENVVAR
  on every replay; repeat it for a tenant or CSRF header. Values come only from the environment,
  never the command line, and are never printed or stored; the report lists only the header
  names under `credentials`. They replace a recorded header of the same name and an `auth set`
  key for ORIGIN's host. rebrowse never signs in itself: the session is your own script's.

  ```bash
  STAGING_COOKIE="$(./scripts/test-user-session.sh)" rebrowse contract api-baseline.json \
    --against https://staging.app.com --header-env Cookie=STAGING_COOKIE
  ```
- **Ids from ORIGIN's own answers.** A recording made against staging asks for staging's rows
  (`/api/orders/81723`, `?userId=4410`, a GraphQL `$id`), which a CI backend seeded with its own
  fixtures answers with 404. `--follow-ids` works out which earlier read's answer held each id
  (the `GET /api/orders` whose `$.orders[1].id` was 81723), sends that read first and uses the id
  ORIGIN's answer holds at the same place. Ids are taken from path segments, id-named query
  parameters and id-named JSON body fields. An id that two reads hold equally is not followed,
  and a read whose id ORIGIN does not answer is skipped as `id not found on target`, never sent
  with a guessed value. The report's `followed` rows name each traced id's route, place and
  source, never a value. Without the flag, a stderr note says when such reads answered 404.
- **Judging.** Each request's live answer is compared with every recorded answer to it, so
  recording order never matters. A request recorded with a 2xx or 304 that now redirects, or
  answers 4xx or 5xx, is a breaking `status` change; a status the recording already holds never
  breaks (`/api/me` recorded as 401 before sign-in), except a 5xx. No answer (timeout, refused
  connection) is a breaking `no_response`, a non-API answer too, and a challenge page is
  `blocked`, never data.
- **Refused runs.** When ORIGIN refuses (401, 403) or redirects every read the recording
  answered, the run exits 2 with one error naming the credentials sent, such as expired
  `--header-env` values or an `auth set` key, instead of dozens of breaking changes.

## Accepted changes

Some breaking changes are intended, such as a rename shipped with its frontend change.
`--accepted FILE` on `diff` and `contract` reads a committed JSON list of reviewed breaks; a
matching change is reported as `"severity": "accepted"`, with the entry's `reason`, and no
longer fails the run.

```bash
rebrowse contract api-baseline.json --against http://localhost:8000 \
  --accepted contract-accepted.json --update-accepted   # on the PR: accept, add reasons, commit
rebrowse contract api-baseline.json --against http://api:8000 --accepted contract-accepted.json
```

```json
[
  {"kind": "field_removed", "route": "GET /api/orders/{orders_id}", "field": "$.total",
   "base": ["integer"], "head": null, "reason": "renamed to totalCents in #412"},
  {"kind": "field_type", "route": "GET /api/users/{users_id}"}
]
```

- **Entries** need `kind` and `route`; `field`, `base`, `head` and `reason` are optional and
  must match when given, so a change copied from a report works as is. Unknown keys are errors.
- **Never accepted:** replay failures, a status change to a 5xx (an outage), and, by
  `--update-accepted`, a working route turned into a sign-in redirect, 401 or 403; write those
  by hand if intended.
- **Stale and unchecked.** An entry that matched nothing is listed as `stale` with a stderr
  warning, so the file cannot rot into an ignore list; one on a route the run could not compare
  is `unchecked` and kept.
- **`--update-accepted`** rewrites FILE from the run (kept entries as written, stale ones
  dropped, new breaks appended), never when a run exits 2, a replay failed or a route answered
  5xx. Give each check its own file.

## Coverage

`rebrowse coverage SOURCE [--fail-under PERCENT]` checks a recording against the frontend's own
JS and lists the API calls the JS can make that the recording never exercised, with each one's
effect, so you know which flows to record before trusting the docs, mock or contract runs. Run it
on the whole suite's folder; one test's HAR covers a sliver of the routes a shared bundle names.

```bash
rebrowse coverage test-results/ --fail-under 80   # CI: exit 1 below 80%
```

- **What is scanned.** Every same-site script in the HAR files (a capture from `build` keeps at
  most 20), without ad, embed and analytics scripts: `fetch`, `axios` and `.get/.post/...` calls
  with string or template-literal paths, `/api/...` and `/vN/...` strings, legacy paths under
  prefixes the recording used (`/rest/...`, `/ajax/get_cart.php`), and GraphQL operations from
  documents and precompiled ASTs. Static files, telemetry, `baseURL` strings, all-placeholder
  paths such as `` `/${resource}/${id}` `` and translation strings such as `"query returned
  {count}"` are not references.
- **Matching.** A reference matches the end of a recorded route (so an axios `baseURL` lines up),
  placeholders match any segment, and `/api/teams` matches a recorded `/api/teams/`. A GET or
  `fetch()` matches any method; an explicit `.post`/`.put`/`.patch`/`.delete` needs that method.
  Covered means a matching call answered 2xx or 304.
- **Report.** `referenced`, `coverage` (%), `unrecorded` references with `effect`, `found_by`,
  bundle URL and any error `statuses`, `covered` ones with `recorded_as`, and `unreferenced`:
  recorded routes no reference matched, such as runtime-built URLs. It never prints bundle source.
- **Blind spots.** URLs built from runtime config, relative paths, persisted-only GraphQL, lazy
  chunks the recording never loaded, and methods set in `fetch(url, {method})` options.
- A baseline keeps no JS: run coverage on the HARs it was written from.

## CI recipe

```yaml
# frontend: on main, after the e2e suite
- run: rebrowse baseline test-results/ -o api-baseline.json   # commit if it changed
- run: rebrowse openapi api-baseline.json -o docs/openapi.json
# frontend: on pull requests
- run: rebrowse diff api-baseline.json test-results/ --accepted diff-accepted.json
- run: rebrowse coverage test-results/ --fail-under 80
# backend: on pull requests, with api-baseline.json copied or vendored from the frontend
- run: docker compose up -d --wait api
- run: rebrowse contract api-baseline.json --against http://api:8000 --follow-ids
       --header-env Cookie=CI_SESSION --accepted contract-accepted.json
  env: { REBROWSE_HOST_INTERVAL: "0", CI_SESSION: "${{ secrets.CI_SESSION }}" }
```

Never cache or upload raw HARs, `vault/` or `captures/` where pull-request jobs can read them;
the baseline is the file meant to be shared.

## Safety model

- **Effects.** Every call is labelled `read`, `write` or `destructive` from its method, path
  verbs (`/comment/destroy` over GET is destructive), action-style query parameters
  (`?action=delete`) and GraphQL operation kind (a document defining any mutation is a write).
- **Confirmation.** Nothing but a read is sent without `--yes` (CLI) or `confirm=true` (MCP
  `act`), writes are never retried, and `contract` and `verify` never send writes at all.
- **Honest identity.** The capture browser is not disguised, and replays identify as
  `rebrowse/<version>` instead of resending a browser fingerprint.
- **Blocks are failures.** Challenge pages (Cloudflare "Just a moment…", robot-policy 403s,
  CAPTCHAs) are reported as `blocked`, never returned as data; there is no bypass logic.
- **Credentials stay scoped.** Stored keys and cookies go only to their own site and are dropped
  from any redirect that leaves it; nothing reads your browser's cookie store, and rebrowse
  never signs in. Requests are paced per host (`REBROWSE_HOST_INTERVAL`, default 1s).
- **No values out.** Recording-based commands run offline and redact as they read; reports,
  accepted files and docs hold route names, field paths, statuses and types, never a recorded
  or live value.

## Development

```bash
pytest            # local fixture site, real headless Chromium, no internet
ruff check rebrowse tests
```

Tests run against a local fixture site with a stubbed LLM and a deterministic stand-in for the
embedding model, in an isolated data directory.

| Module | Role |
|---|---|
| `capture/` | browser capture (`browser.py`), HAR reading (`har.py`), saved captures and recording folders (`store.py`) |
| `reverse/` | endpoint extraction, GraphQL operations, JS bundle scanning |
| `orchestrator/pipeline.py` | `build`, `import-har`, `run`, `verify` |
| `openapi.py`, `mock.py`, `baseline.py` | docs, mock server, committable baselines |
| `drift.py`, `contract.py`, `follow.py`, `accepted.py` | breaking-change rules, replay, id following, accepted changes |
| `coverage.py` | JS references against a recording |
| `safety.py`, `execution/`, `auth/` | effects and redaction, the executor, the encrypted vault |
| `mcp_server.py`, `cli.py` | MCP tools and the command line |

The `skills/` folder holds documentation of skills captured in March 2026 with an earlier
version; it is not loaded by the tool.
