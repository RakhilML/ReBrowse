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

rebrowse run "top stories on hacker news"
rebrowse run "add a note saying hi" --dry-run      # show the resolved call, send nothing
rebrowse run "add a note saying hi" --yes          # confirm a call that changes state

rebrowse verify news.ycombinator.com               # health-check a skill's read endpoints
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
  own site; nothing reads your browser's cookie store. Requests are paced per host
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
pytest            # 124 tests; local fixture site, real headless Chromium, no internet
ruff check rebrowse tests
```

Tests run against a local fixture site with a stubbed LLM and a deterministic stand-in
for the embedding model, in an isolated data directory.

The `skills/` folder holds documentation of skills captured in March 2026 with an
earlier version; it is not loaded by the tool.
