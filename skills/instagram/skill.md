# www.instagram.com API

- **Domain**: `www.instagram.com`
- **Skill ID**: `1d9b8fa95fbc`
- **Endpoints**: 12
- **Description**: Auto-discovered APIs from www.instagram.com

## Endpoints

### `POST https://www.instagram.com/ajax/bulk-route-definitions`

Fetches bulk route definitions for client-side routing optimization.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.4 |
| Idempotency | unsafe |

### `POST https://www.instagram.com/ajax/qm`

Submits quality metrics or telemetry data for performance monitoring.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.2 |
| Idempotency | unsafe |
| Query Params | __a, __user, __comet_req, jazoest |

### `POST https://www.instagram.com/ajax/bz`

Sends beacon or analytics data for user behavior tracking.

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.2 |
| Idempotency | unsafe |
| Query Params | __a, __ccg, __comet_req, __crn, __d, __hs, __hsi, __req, __rev, __s, __spin_b, __spin_r, __spin_t, __user, dpr, jazoest, lsd, ph |

### `GET https://www.instagram.com/api/v1/friendships/mute_posts_or_story_from_follow/`

Mutes posts and stories from a followed user.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.instagram.com/api/v1/friendships/unmute_posts_or_story_from_follow/`

Unmutes posts and stories from a previously muted followed user.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.instagram.com/api/v1/web/restrict_action/restrict/`

Restricts a user's ability to interact with the current account.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.instagram.com/api/v1/web/restrict_action/unrestrict/`

Removes restrictions from a previously restricted user.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.instagram.com/api/v1/web/search/topsearch/`

Returns top search results (users, hashtags, locations) based on query.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.instagram.com/api/v1/web/location_search/`

Searches for locations by name or coordinates.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.instagram.com/api/v1/web/search/hide_search_entities/`

Hides specific search entities (e.g., users, hashtags) from appearing in search suggestions.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.instagram.com/api/v1/fbsearch/non_profiled_serp/`

Returns non-profiled search results (e.g., posts, reels) for a given query.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.instagram.com/api/v1/web/search/recent_searches/`

Retrieves the user's recent search history.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
