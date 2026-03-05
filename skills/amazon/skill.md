# www.amazon.com API

- **Domain**: `www.amazon.com`
- **Skill ID**: `db790e6cf0bd`
- **Endpoints**: 5
- **Description**: Auto-discovered APIs from www.amazon.com

## Endpoints

### `GET https://www.amazon.com/hz/rhf`

Retrieves the 'Recommended for You' homepage content

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 1.0 |
| Idempotency | safe |
| Query Params | currentPageType, currentSubPageType, cardJSPresent |

### `GET https://www.amazon.com/cart/add-to-cart/patc-config`

Fetches configuration data for adding items to the cart

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 1.0 |
| Idempotency | safe |
| Query Params | clientName, ref_ |

### `GET https://www.amazon.com/Best-Sellers/zgbs`

Returns the best sellers list for a given category

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.5 |
| Idempotency | safe |

### `GET https://www.amazon.com/portal-migration/hz/glow/get-rendered-toaster`

Gets rendered toast notification content for user alerts

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.5 |
| Idempotency | safe |
| Query Params | pageType, aisTransitionState, rancorLocationSource, isB2B, _ |

### `POST https://www.amazon.com/vap/ew/componentbuilder`

Builds and renders dynamic UI components via server-side request

| Property | Value |
|----------|-------|
| Method | POST |
| Reliability | 0.4 |
| Idempotency | unsafe |
