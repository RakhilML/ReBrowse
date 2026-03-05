# www.netflix.com API

- **Domain**: `www.netflix.com`
- **Skill ID**: `9ea5d0a1c116`
- **Endpoints**: 5
- **Description**: Auto-discovered APIs from www.netflix.com

## Endpoints

### `POST https://web.prod.cloud.netflix.com/graphql`

Executes GraphQL queries/mutations for client-side data fetching and updates.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 1.0 |
| Idempotency | unsafe |

### `GET https://www.netflix.com/in`

Serves the Netflix India regional landing page.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.netflix.com/`

Serves the global Netflix homepage or redirects based on user location/authentication.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |

### `GET https://www.netflix.com/api/v2/pixel`

Tracks user events and analytics via invisible pixel requests.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.netflix.com/api/v1/`

Returns basic API metadata or health status for version 1 endpoints.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
