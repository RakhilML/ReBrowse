# www.ebay.com API

- **Domain**: `www.ebay.com`
- **Skill ID**: `77155b6fb9db`
- **Endpoints**: 8
- **Description**: Auto-discovered APIs from www.ebay.com

## Endpoints

### `GET https://www.ebay.com/sch/ajax/autocomplete`

Returns search term autocomplete suggestions

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 1.0 |
| Idempotency | safe |

### `GET https://www.ebay.com/gh/useracquisition`

Handles user acquisition tracking or onboarding flow

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 1.0 |
| Idempotency | safe |
| Query Params | correlation, v |

### `GET https://backstory.ebay.com/customer/v1/bs_img_service`

Serves customer backstory images for analytics or personalization

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.8 |
| Idempotency | safe |
| Query Params | pld, ct |

### `POST https://www.ebay.com/gh/gadget_csm`

Submits gadget or widget usage telemetry data

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.7 |
| Idempotency | unsafe |
| Query Params | v |

### `GET https://www.ebay.com/ifh/inflowcomponent`

Returns UI component data for page inflow tracking

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.5 |
| Idempotency | safe |
| Query Params | callback, fromGH, input |

### `GET https://devicebind.ebay.com/signin/sub/tt.html`

Initiates device binding for sign-in session tracking

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.5 |
| Idempotency | safe |
| Query Params | st, f, e, pageid, rec, sc, sm, sig |

### `GET https://rover.ebay.com/roverimp/0/0/9`

Tracks impression events for advertising or recommendation systems

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
| Query Params | imp, trknvp |

### `GET https://www.ebay.com/sch/ajax/post/image`

Retrieves uploaded post image for listing or user content

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.3 |
| Idempotency | safe |
