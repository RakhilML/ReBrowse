# www.imdb.com API

- **Domain**: `www.imdb.com`
- **Skill ID**: `47df312fa281`
- **Endpoints**: 6
- **Description**: Auto-discovered APIs from www.imdb.com

## Endpoints

### `GET https://api.graphql.imdb.com/`

Primary GraphQL endpoint for executing queries and mutations via POST requests.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 1.0 |
| Idempotency | safe |
| Query Params | operationName, variables, extensions |

### `GET https://caching.graphql.imdb.com/`

GraphQL caching endpoint optimized for read-heavy, cached responses.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 1.0 |
| Idempotency | safe |
| Query Params | operationName, variables, extensions |

### `POST https://api.graphql.imdb.com/`

Primary GraphQL endpoint for executing queries and mutations via POST requests.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.9 |
| Idempotency | unsafe |

### `GET https://www.imdb.com/api/_ajax/weblab/`

Fetches A/B testing configuration or WebLab experiment data for client-side personalization.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.imdb.com/api/mobile/notifications/getInboxUnseenCount`

Retrieves the count of unseen notifications in the user's inbox.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.imdb.com/api/instantSearch`

Returns real-time search suggestions/results as the user types in the search bar.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
