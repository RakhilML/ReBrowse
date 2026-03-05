# dev.to API

- **Domain**: `dev.to`
- **Skill ID**: `b827a7c3394a`
- **Endpoints**: 25
- **Description**: Auto-discovered APIs from dev.to

## Endpoints

### `GET https://dev.to/async_info/base_data`

Fetches base asynchronous data for page initialization

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.6 |
| Idempotency | safe |

### `POST https://dev.to/ahoy/visits`

Records a user visit event

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.4 |
| Idempotency | unsafe |

### `GET https://dev.to/`

Serves the main homepage

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |

### `GET https://dev.to/bmar11/sidebar_left`

Returns left sidebar content for user bmar11

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |

### `GET https://dev.to/bmar11/sidebar_left_2`

Returns secondary left sidebar content for user bmar11

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |

### `GET https://dev.to/api/followers/users`

Lists users following the current user

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/api/videos`

Retrieves a list of videos

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/bb_tabulations`

Returns tabulation data for BB (likely badge or behavior) metrics

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/async_info/base_data`

Fetches base asynchronous data for page initialization

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/v1/events`

Fetches versioned event data

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/v1/notices/js`

Serves JavaScript for client-side notices

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/image_uploads`

Returns metadata or status for image uploads

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/comments/moderator_create`

Prepares or renders moderator comment creation interface

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/response_templates`

Returns reusable response templates

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/async_info/navigation_links`

Fetches asynchronous navigation link data

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/feed_events`

Returns feed events for activity stream

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/fallback_activity_recorder`

Records fallback activity data

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/ahoy/email_clicks`

Tracks email click events

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/page_views`

Records or retrieves page view statistics

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/notifications/reads`

Marks notifications as read

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/sidebars/home`

Returns home sidebar content

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/api/jnn/v1/GenerateIT`

Calls internal AI service to generate IT-related content

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/generate_204`

Returns HTTP 204 No Content for tracking or heartbeat

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/api/lounge`

Fetches data for the lounge community section

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://dev.to/api/loungedev`

Fetches data for the loungedev community section

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
