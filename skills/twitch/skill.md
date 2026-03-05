# www.twitch.tv API

- **Domain**: `www.twitch.tv`
- **Skill ID**: `13bb5f716a54`
- **Endpoints**: 7
- **Description**: Auto-discovered APIs from www.twitch.tv

## Endpoints

### `POST https://gql.twitch.tv/gql`

Main GraphQL endpoint for Twitch API requests

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 1.0 |
| Idempotency | unsafe |

### `POST https://gql.twitch.tv/integrity`

Submits integrity token for client authentication

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.9 |
| Idempotency | unsafe |

### `GET https://assets.twitch.tv/eppo/api/flag-config/v1/config`

Fetches feature flag configuration from Eppo

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.7 |
| Idempotency | safe |
| Query Params | sdkName, sdkVersion, callback |

### `GET https://assets.twitch.tv/config/manifest.json`

Retrieves client-side configuration manifest

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.7 |
| Idempotency | safe |
| Query Params | v |

### `GET https://passport.twitch.tv/{id}/{id_id}/fp`

Fetches fingerprint data for user identification

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.4 |
| Idempotency | safe |
| Query Params | x-kpsdk-v |
| Path Params | id, id_id |

### `GET https://gql.twitch.tv/{id}/{id_id}/fp`

Fetches fingerprint data via GraphQL endpoint

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.4 |
| Idempotency | safe |
| Query Params | x-kpsdk-v |
| Path Params | id, id_id |

### `GET https://gql.twitch.tv/{id}/{id_id}/mfc`

Fetches minimal feature configuration via GraphQL

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
| Path Params | id, id_id |
