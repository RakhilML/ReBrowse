# www.goodreads.com API

- **Domain**: `www.goodreads.com`
- **Skill ID**: `a1fb91d7bcd6`
- **Endpoints**: 23
- **Description**: Auto-discovered APIs from www.goodreads.com

## Endpoints

### `GET https://www.goodreads.com/fb_dep_verify_email_address/update_cache`

Updates cache for Facebook-dependent email address verification status.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/fb_dep_resend_verify_email`

Resends verification email for Facebook-dependent accounts.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/ev_resend_verification_email`

Resends email verification for standard accounts.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/track/track`

Tracks user activity or page views.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/track/track_click`

Tracks user click events.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/friend/requests`

Retrieves pending friend requests.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/facebook_users/auth_status`

Checks Facebook authentication status for a user.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/facebook_users/fb_url`

Retrieves Facebook profile URL for a user.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/user/sign_out`

Logs out the current user.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/user/update_preferences`

Updates user preferences (likely via query params).

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/user_challenges/covers/show/`

Displays cover images for reading challenges.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/notifications/track`

Tracks notification-related events.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/year_in_books/covers/show/`

Displays cover images for Year in Books summary.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/videos/update_views/`

Increments view count for a video.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/user_challenges/watched_video`

Records that a user watched a challenge-related video.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/year_in_books/blurb/show/`

Retrieves Year in Books summary text.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/year_in_books/blurb/update/`

Updates Year in Books summary text.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/author_followings`

Lists authors the user is following.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/comment/destroy`

Deletes a comment (likely via query param ID).

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/user/edit_fav_genres`

Updates user's favorite genres.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/genres/featured_books`

Retrieves books featured in specific genres.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/dfp/browse_menu_impression`

Tracks impression of DFP (DoubleClick for Publishers) browse menu.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |

### `GET https://www.goodreads.com/user/`

Retrieves current user's profile information.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
