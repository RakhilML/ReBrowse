# www.pinterest.com API

- **Domain**: `www.pinterest.com`
- **Skill ID**: `b8ef3faad528`
- **Endpoints**: 12
- **Description**: Auto-discovered APIs from www.pinterest.com

## Endpoints

### `GET https://www.pinterest.com/resource/UnauthUserDataResource/get`

Fetches user data for unauthenticated users.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 1.0 |
| Idempotency | safe |
| Query Params | source_url, data, _ |

### `GET https://www.pinterest.com/resource/UserExperienceResource/get`

Retrieves user experience-related configuration or telemetry data.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 1.0 |
| Idempotency | safe |
| Query Params | source_url, data, _ |

### `GET https://www.pinterest.com/resource/ApiResource/get`

Fetches general API data, likely for initial page load or user context.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 1.0 |
| Idempotency | safe |
| Query Params | source_url, data, _ |

### `POST https://www.pinterest.com/_/_/trace/trace`

Submits tracing or performance telemetry data.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.9 |
| Idempotency | unsafe |

### `POST https://www.pinterest.com/resource/ApiResource/create`

Creates or submits new API data, such as user actions or content.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.9 |
| Idempotency | unsafe |

### `POST https://www.pinterest.com/resource/ActivateExperimentResource/create`

Activates or logs participation in an A/B test experiment.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.9 |
| Idempotency | unsafe |

### `POST https://www.pinterest.com/resource/UserRegisterTrackActionResource/update`

Records or updates user tracking actions for analytics.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.9 |
| Idempotency | unsafe |

### `POST https://www.pinterest.com/resource/ApiSResource/create`

Submits data to a specialized API service, likely for search or suggestions.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.9 |
| Idempotency | unsafe |

### `POST https://www.pinterest.com/resource/ApiCResource/create`

Submits data to a specialized API service, likely for content creation or moderation.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.9 |
| Idempotency | unsafe |

### `GET https://www.pinterest.com/manifest.json`

Serves the web app manifest for PWA installation metadata.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.7 |
| Idempotency | safe |

### `GET https://ct.pinterest.com/ct.html`

Handles click tracking or conversion pixel events.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.5 |
| Idempotency | safe |

### `POST https://www.pinterest.com/_/_/storage_report`

Reports client-side storage usage or quota status.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.2 |
| Idempotency | unsafe |
