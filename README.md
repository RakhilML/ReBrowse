# rebrowse

**Reverse-engineer any website's API. Locally. In one command.**

You know that feeling when you're inspecting network traffic in DevTools, copying URLs, figuring out headers, trying to understand what API a site is actually using under the hood? That's what rebrowse automates. Point it at any website, and it figures out every API endpoint the site talks to — then lets you call those endpoints yourself with plain English.

No cloud. No API keys required. Everything runs on your machine.

## How it works

```
                         YOU
                          |
              "get trending repos from github"
                          |
                    +-----v------+
                    |   rebrowse  |
                    +-----+------+
                          |
            +-------------+-------------+
            |                           |
      +-----v------+            +------v------+
      |    build    |            |     run     |
      +-----+------+            +------+------+
            |                           |
    +-------v--------+          +------v------+
    | Headless Browser|          | Parse Intent |
    | (Playwright)    |          | (Local LLM)  |
    +-------+--------+          +------+------+
            |                           |
    +-------v--------+          +------v------+
    | Capture Traffic  |          | Search Skills|
    | Filter Telemetry |          | (Embeddings) |
    | Score Endpoints  |          +------+------+
    | Scan JS Bundles  |                 |
    +-------+--------+          +------v------+
            |                    | LLM Picks    |
    +-------v--------+          | Best Endpoint |
    | LLM Describes   |          +------+------+
    | Each Endpoint    |                 |
    +-------+--------+          +------v------+
            |                    | Execute HTTP |
    +-------v--------+          | + Return Data|
    | Save as "Skill"  |          +--------------+
    | (SQLite + Embed) |
    +------------------+
```

Two commands. That's the whole interface:

```bash
# Step 1: Teach rebrowse about a website
python -m rebrowse build https://github.com/trending

# Step 2: Ask it things in plain english
python -m rebrowse run "get trending repos from github"
```

**build** opens a real browser (headless Chromium), visits the page, scrolls around like a human would, and records every single HTTP request the site makes. Then it filters out all the tracking/analytics garbage, scores what's left by how "API-like" it looks, digs through JavaScript bundles for hidden routes, asks a local LLM to describe each endpoint, and saves the whole thing as a searchable "skill".

**run** takes your plain English query, figures out which website you're talking about, searches your saved skills, asks the LLM to pick the right endpoint and fill in parameters, fires the request, and hands you back the data.

## Quick start

```bash
git clone https://github.com/RakhilML/ReBrowse
cd rebrowse
pip install -r requirements.txt
playwright install chromium
```

Copy the example config and pick your LLM:

```bash
cp .env.example .env
```

### Pick your LLM provider

rebrowse works with whatever LLM you have. Local, cloud, doesn't matter — as long as it can handle chat completions.

**LM Studio / vLLM (default — fully offline)**
```env
LLM_PROVIDER=local
LLM_BASE_URL=http://localhost:1234/v1
LLM_MODEL_INTENT=qwen/qwen3-8b
LLM_MODEL_CODE=qwen/qwen3-coder-next
```

**Ollama**
```env
LLM_PROVIDER=ollama
LLM_MODEL_INTENT=llama3.1:8b
LLM_MODEL_CODE=qwen2.5-coder:14b
```

**OpenAI**
```env
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_MODEL_INTENT=gpt-4o-mini
LLM_MODEL_CODE=gpt-4o
```

**Google Gemini**
```env
LLM_PROVIDER=gemini
LLM_API_KEY=AIza...
LLM_MODEL_INTENT=gemini-2.0-flash
LLM_MODEL_CODE=gemini-2.5-pro
```

**Anthropic Claude**
```env
LLM_PROVIDER=claude
LLM_API_KEY=sk-ant-...
LLM_MODEL_INTENT=claude-haiku-4-5-20251001
LLM_MODEL_CODE=claude-sonnet-4-6
```

## Usage

### Build skills from websites

```bash
python -m rebrowse build https://www.reddit.com/r/popular
python -m rebrowse build https://dev.to
python -m rebrowse build https://www.imdb.com
python -m rebrowse build https://news.ycombinator.com
```

Each build captures the site's traffic, reverse-engineers every API it can find, and saves it locally. You only need to do this once per site.

### Query with natural language

```bash
python -m rebrowse run "get popular posts from reddit"
python -m rebrowse run "search for movies on imdb"
python -m rebrowse run "get trending hashtags from twitter"
python -m rebrowse run "show top stories on hacker news"
```

The LLM figures out which site you mean, picks the right endpoint, fills in the parameters, and fires the request. You get back raw API data.

### Manage skills

```bash
python -m rebrowse skills                      # list all saved skills
python -m rebrowse skills -q "search api"      # semantic search across skills
python -m rebrowse show <skill_id>             # full endpoint details
python -m rebrowse delete <skill_id>           # remove a skill
```

### Store API keys for authenticated sites

Some APIs need keys. rebrowse stores them encrypted locally and auto-injects them into requests:

```bash
# Bearer token (most common)
python -m rebrowse auth set api.github.com ghp_yourtoken

# As X-API-Key header
python -m rebrowse auth set api.example.com your-key --type header

# As query parameter (?api_key=...)
python -m rebrowse auth set maps.googleapis.com AIza... --type query

# Manage stored keys
python -m rebrowse auth list
python -m rebrowse auth remove api.github.com
```

## What happens under the hood

Here's the full pipeline when you run `build`:

1. **Capture** — Playwright launches headless Chromium, navigates to the URL, scrolls the page (triggers lazy-loaded SPA content), and records every HTTP request/response
2. **Filter** — Strips out 50+ known telemetry/tracking/analytics patterns (Google Analytics, Facebook Pixel, ad networks, health checks, beacons, etc.)
3. **Score** — Rates each remaining request by API likelihood. GraphQL gets +8, `/api/` paths get +5, JSON responses get +5, authentication headers get +4. Telemetry paths get penalized hard
4. **Normalize** — Templatizes dynamic path segments. `/users/12345/repos` becomes `/users/{users_id}/repos`
5. **Deduplicate** — Merges duplicate (method, template) pairs
6. **Scan JS** — Regex-scans all JavaScript bundles for hidden API routes (`/api/*`, `fetch()` calls, `/v1/*` patterns)
7. **Describe** — LLM writes a one-line description for each discovered endpoint
8. **Store** — Saves everything as a SkillManifest with semantic embeddings in SQLite

And when you `run`:

1. **Parse** — LLM extracts domain + action + params from your natural language
2. **Search** — Looks up skills by domain alias ("twitter" -> x.com) + semantic similarity
3. **Pick** — LLM chooses the best endpoint and fills in path/query parameters
4. **Execute** — Fires the HTTP request with stored cookies/API keys, retries on failure
5. **Return** — Hands you the raw response data

## Project structure

```
rebrowse/
  capture/       Playwright headless browser, scrolling, traffic recording
  reverse/       Endpoint extraction, scoring, JS bundle scanning
  llm/           Multi-provider LLM client + DSPy signatures
  store/         SQLite + sentence-transformers semantic search
  execution/     HTTP executor with retry, cookie/API key injection
  auth/          Cookie extraction (Chrome/Firefox DPAPI) + encrypted vault
  orchestrator/  Build and Run pipelines
  cli.py         Click CLI (build, run, skills, show, delete, auth)
  models.py      Pydantic models
  config.py      All config from .env
```

## What makes this different

- **Truly local** — default setup needs zero internet. Local LLM, local embeddings, local storage. Nothing phones home
- **Any website** — not pre-configured for specific sites. Point it at literally any URL and it figures out the APIs
- **Smart filtering** — doesn't just dump all network traffic. Actively filters telemetry, scores by API likelihood, deduplicates
- **JS bundle scanning** — finds API routes that never appeared in network traffic by scanning the site's JavaScript
- **Browser cookie extraction** — pulls auth cookies straight from Chrome/Firefox (DPAPI decryption on Windows) for authenticated captures
- **API key management** — store keys per-domain with encrypted vault, auto-injected as Bearer/header/query param
- **Domain aliases** — say "twitter" and it finds x.com. Say "insta" and it finds instagram.com. 40+ aliases built in
- **Multi-provider LLM** — works with LM Studio, Ollama, OpenAI, Gemini, Claude, vLLM, or any OpenAI-compatible server
- **DSPy signatures** — typed input/output contracts for all LLM calls, with raw-prompt fallback
- **Semantic search** — find skills by meaning, not just keywords (sentence-transformers all-MiniLM-L6-v2)

## Pre-built skills

The [`skills/`](skills/) directory has documented API discoveries from 23 major sites:

| Site | Endpoints | Highlights |
|------|-----------|------------|
| [dev.to](skills/dev/skill.md) | 25 | Videos, Followers, Feed Events |
| [Goodreads](skills/goodreads/skill.md) | 23 | Featured Books, Author Followings |
| [Reddit](skills/reddit/skill.md) | 12 | GraphQL, Popular Feed |
| [Instagram](skills/instagram/skill.md) | 12 | Search, Location Search, Friendships |
| [Pinterest](skills/pinterest/skill.md) | 12 | User Data Resource, Pin Feed |
| [Spotify](skills/spotify/skill.md) | 10 | GraphQL Pathfinder, Token API |
| [eBay](skills/ebay/skill.md) | 8 | Autocomplete, Search |
| [Twitch](skills/twitch/skill.md) | 7 | GraphQL GQL |
| [Twitter/X](skills/twitter/skill.md) | 6 | GraphQL Viewer, Hashflags |
| [YouTube](skills/youtube/skill.md) | 6 | Guide API, Feedback |
| [IMDB](skills/imdb/skill.md) | 6 | GraphQL, Instant Search |
| [LinkedIn](skills/linkedin/skill.md) | 6 | User Metadata, Litms API |
| [Amazon](skills/amazon/skill.md) | 5 | Best Sellers, Recommendations |
| [Netflix](skills/netflix/skill.md) | 5 | GraphQL, Shakti API |
| [Medium](skills/medium/skill.md) | 4 | GraphQL, Content Feed |
| [Flipkart](skills/flipkart/skill.md) | 3 | Homepage, Product API |
| [Wikipedia](skills/wikipedia/skill.md) | 3 | MediaWiki API |
| [Wikipedia Portal](skills/wikipedia-portal/skill.md) | 2 | Portal Homepage |
| [Stack Overflow](skills/stackoverflow/skill.md) | 2 | Homepage, Questions |
| [npm](skills/npmjs/skill.md) | 2 | Settings, Package Search |
| [GitHub](skills/github/skill.md) | 1 | Trending Repos |
| [Hacker News](skills/hackernews/skill.md) | 1 | Top Stories |
| [JSONPlaceholder](skills/jsonplaceholder/skill.md) | 1 | REST API Resources |

Each site links to its full endpoint documentation.

## DSPy signatures

All LLM interactions are defined as typed DSPy signatures — structured input/output contracts that make it clear what goes in and what comes out of each LLM call. This means the LLM isn't just getting a loose prompt; it's working against a defined schema.

rebrowse uses three signatures:

```python
class ParseIntent(dspy.Signature):
    """Extract structured intent from a natural language query."""
    user_query: str = dspy.InputField()
    domain: str = dspy.OutputField()     # e.g. "github.com"
    action: str = dspy.OutputField()     # e.g. "get trending repositories"
    params: dict = dspy.OutputField()    # e.g. {"language": "python"}

class PickEndpoint(dspy.Signature):
    """Pick the best API endpoint for a user's intent."""
    intent: str = dspy.InputField()
    endpoints: list[dict] = dspy.InputField()
    endpoint_id: str = dspy.OutputField()
    params: dict = dspy.OutputField()
    query: dict = dspy.OutputField()
    reason: str = dspy.OutputField()

class DescribeEndpoints(dspy.Signature):
    """Write descriptions for discovered API endpoints."""
    endpoints_json: str = dspy.InputField()
    descriptions_json: str = dspy.OutputField()
```

DSPy is **optional** — if it's not installed, rebrowse falls back to raw prompts that do the same thing. But when it's there, you get:
- **Typed contracts** — the LLM knows exactly what fields to return, reducing malformed output
- **Prompt optimization** — DSPy can auto-tune prompts for your specific model
- **Swappable models** — change your LLM and the signatures still work; DSPy adapts the prompting style

Install it separately if you want it:
```bash
pip install dspy
```

See [`rebrowse/llm/signatures.py`](rebrowse/llm/signatures.py) for the full source.

## Data storage

Everything lives at `~/.rebrowse/`:
- `skills.db` — SQLite database (skill manifests + vector embeddings)
- `vault/` — Fernet/AES encrypted credentials, cookies, and API keys
- `skills/` — Skill data

## Requirements

- Python 3.11+
- Any LLM (local or cloud — see provider setup above)
- Playwright (`playwright install chromium`)
