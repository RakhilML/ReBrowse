# medium.com API

- **Domain**: `medium.com`
- **Skill ID**: `ec63fe0a6e7a`
- **Endpoints**: 4
- **Description**: Auto-discovered APIs from medium.com

## Endpoints

### `POST https://medium.com/_/graphql`

Executes GraphQL queries/mutations for dynamic content fetching.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 1.0 |
| Idempotency | unsafe |

### `POST https://medium.com/_/clientele/reports/performance`

Submits performance telemetry data for analytics.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.2 |
| Idempotency | unsafe |

### `GET https://medium.com/api/v2/`

Fetches API version and service status information.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://medium.com/v1/input/`

Retrieves input configuration or metadata for client-side forms.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
