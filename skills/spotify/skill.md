# open.spotify.com API

- **Domain**: `open.spotify.com`
- **Skill ID**: `fff922863690`
- **Endpoints**: 10
- **Description**: Auto-discovered APIs from open.spotify.com

## Endpoints

### `POST https://api-partner.spotify.com/pathfinder/v2/query`

Executes GraphQL-style queries to fetch Spotify data like playlists, tracks, or user info.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 1.0 |
| Idempotency | unsafe |

### `GET https://open.spotify.com/api/token`

Retrieves an OAuth access token for authenticated API requests.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 1.0 |
| Idempotency | safe |
| Query Params | reason, productType, totp, totpServer, totpVer |

### `POST https://clienttoken.spotify.com/v1/clienttoken`

Obtains a client token for unauthenticated or device-specific access to Spotify services.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 1.0 |
| Idempotency | unsafe |

### `POST https://gae2-spclient.spotify.com/remote-config-resolver/v3/unauth/configuration`

Fetches remote feature flag and configuration settings for unauthenticated clients.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 1.0 |
| Idempotency | unsafe |

### `GET https://apresolve.spotify.com/`

Resolves the appropriate backend service endpoints for client connections.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 1.0 |
| Idempotency | safe |
| Query Params | type |

### `GET https://www.spotify.com/api/masthead/v1/masthead`

Returns masthead (header) content for the Spotify web player UI.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.8 |
| Idempotency | safe |
| Query Params | market, language, countryHub |

### `GET https://open.spotify.com/api/masthead/v1/masthead`

Returns masthead content for the Spotify web API context (e.g., embedded player).

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://open.spotify.com/v1/msg/batch`

Retrieves a batch of server-sent messages or updates for real-time client synchronization.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://open.spotify.com/v1/metric`

Collects and reports usage metrics or telemetry data to Spotify.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://open.spotify.com/v1/msg/jssdk_content_request`

Requests dynamic content (e.g., ads, messages) for the JavaScript SDK integration.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
