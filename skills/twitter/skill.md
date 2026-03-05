# x.com API

- **Domain**: `x.com`
- **Skill ID**: `b7f4e066ba5e`
- **Endpoints**: 6
- **Description**: Auto-discovered APIs from x.com

## Endpoints

### `GET https://api.x.com/1.1/hashflags.json`

Retrieves a list of hash flags (trending topics or special campaign tags) available on the platform.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.6 |
| Idempotency | safe |

### `GET https://api.x.com/graphql/zWQLM9HIVahRSUvzUH4lDw/Viewer`

Fetches the current user's profile and account details via GraphQL.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.5 |
| Idempotency | safe |
| Query Params | variables, features, fieldToggles |

### `POST https://api.x.com/1.1/onboarding/sso_init.json`

Initializes single sign-on (SSO) authentication for user onboarding.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.4 |
| Idempotency | unsafe |

### `POST https://api.x.com/1.1/graphql/user_flow.json`

Handles user flow state and progress tracking during onboarding or account actions.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.3 |
| Idempotency | unsafe |

### `POST https://api.x.com/1.1/graphql/ces/p2`

Submits or retrieves content moderation feedback (e.g., report, appeal) via GraphQL.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.3 |
| Idempotency | unsafe |

### `GET https://x.com/`

Serves the main homepage or landing page for unauthenticated users.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |
