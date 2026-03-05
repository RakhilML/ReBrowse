# www.linkedin.com API

- **Domain**: `www.linkedin.com`
- **Skill ID**: `aebcf990ffeb`
- **Endpoints**: 6
- **Description**: Auto-discovered APIs from www.linkedin.com

## Endpoints

### `GET https://www.linkedin.com/litms/api/metadata/user`

Retrieves user metadata from LinkedIn's Litms service.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.9 |
| Idempotency | safe |

### `GET https://www.linkedin.com/homepage-guest/manifest.json`

Fetches the homepage manifest for guest users.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.6 |
| Idempotency | safe |

### `POST https://www.linkedin.com/homepage-guest/api/ingraphs/gauge`

Submits gauge metric data for guest homepage analytics.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.3 |
| Idempotency | unsafe |

### `POST https://www.linkedin.com/homepage-guest/api/ingraphs/counter`

Submits counter metric data for guest homepage analytics.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.3 |
| Idempotency | unsafe |

### `GET https://www.linkedin.com/`

Serves the main LinkedIn homepage.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |

### `GET https://www.linkedin.com/api/v2/msft`

Retrieves Microsoft-related metadata or configuration from LinkedIn's API.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
