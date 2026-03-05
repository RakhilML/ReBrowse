# en.wikipedia.org API

- **Domain**: `en.wikipedia.org`
- **Skill ID**: `4b2e58febada`
- **Endpoints**: 3
- **Description**: Auto-discovered APIs from en.wikipedia.org

## Endpoints

### `GET https://en.wikipedia.org/w/api.php`

Main MediaWiki API endpoint for fetching and manipulating wiki data

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |

### `GET https://en.wikipedia.org/w/load.php`

Serves client-side resources like JavaScript, CSS, and images for the wiki interface

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |
| Query Params | lang, modules, only, skin |

### `GET https://en.wikipedia.org/wiki/Special:CentralAutoLogin/start`

Initiates single sign-on authentication across Wikimedia projects

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |
| Query Params | useformat, type, usesul3 |
