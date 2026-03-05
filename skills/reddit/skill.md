# www.reddit.com API

- **Domain**: `www.reddit.com`
- **Skill ID**: `290b4fbe82ec`
- **Endpoints**: 12
- **Description**: Auto-discovered APIs from www.reddit.com

## Endpoints

### `POST https://www.reddit.com/svc/shreddit/graphql`

Executes GraphQL queries/mutations for dynamic content fetching.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 1.0 |
| Idempotency | unsafe |

### `GET https://www.reddit.com/svc/shreddit/styling-overrides`

Fetches custom styling overrides for the UI.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.8 |
| Idempotency | safe |
| Query Params | v |

### `GET https://www.reddit.com/svc/shreddit/feeds/popular-feed`

Retrieves the popular feed content for the homepage.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.8 |
| Idempotency | safe |
| Query Params | after, distance, adDistance, navigationSessionId, ad_posts_served, cursor, sort |

### `GET https://www.reddit.com/svc/shreddit/partial/J7VVLM/common-left-nav`

Loads the common left navigation sidebar component.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.5 |
| Idempotency | safe |
| Query Params | data, sig |

### `GET https://www.reddit.com/r/popular`

Serves the main popular posts page HTML.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.reddit.com/svc/shreddit/update-recaptcha`

Updates or refreshes reCAPTCHA configuration/token.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
| Query Params | k |

### `GET https://www.reddit.com/svc/shreddit/data-protection-consent`

Fetches user data protection consent preferences.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://alb.reddit.com/skatepark`

Handles A/B testing or feature flag configuration retrieval.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
| Query Params | IDLT |

### `POST https://www.reddit.com/svc/shreddit/perfMetrics`

Submits client-side performance metrics for monitoring.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.2 |
| Idempotency | unsafe |

### `GET https://www.reddit.com/svc/shreddit/update-recaptcha`

Updates or refreshes reCAPTCHA configuration/token.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.reddit.com/svc/shreddit/token`

Retrieves authentication or CSRF token for secure requests.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.reddit.com/svc/shreddit/events/validate-schema`

Validates event tracking schema before sending telemetry data.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
