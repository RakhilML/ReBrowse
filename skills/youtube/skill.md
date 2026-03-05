# www.youtube.com API

- **Domain**: `www.youtube.com`
- **Skill ID**: `87f52ebb54d7`
- **Endpoints**: 6
- **Description**: Auto-discovered APIs from www.youtube.com

## Endpoints

### `POST https://www.youtube.com/youtubei/v1/feedback`

Submits user feedback or bug reports to YouTube.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 1.0 |
| Idempotency | unsafe |
| Query Params | prettyPrint |

### `POST https://www.youtube.com/youtubei/v1/guide`

Retrieves the user's navigation guide (e.g., sidebar menu items) from YouTube.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 1.0 |
| Idempotency | unsafe |
| Query Params | prettyPrint |

### `POST https://www.youtube.com/api/jnn/v1/GenerateIT`

Generates an identity token (IT) for authentication or session management.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 1.0 |
| Idempotency | unsafe |

### `GET https://youtube.com/`

Serves the main YouTube homepage.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |

### `GET https://www.youtube.com/api/lounge`

Initializes or retrieves lounge-related data, likely for YouTube Premium features.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.youtube.com/api/loungedev`

Retrieves development-specific lounge configuration or data.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
