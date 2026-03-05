# stackoverflow.com API

- **Domain**: `stackoverflow.com`
- **Skill ID**: `dd5ab41d1a0f`
- **Endpoints**: 2
- **Description**: Auto-discovered APIs from stackoverflow.com

## Endpoints

### `GET https://stackoverflow.com/`

Returns the homepage of Stack Overflow.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |

### `GET https://stackoverflow.com/questions`

Returns a list of questions on Stack Overflow.

| Property | Value |
|----------|-------|
| Method | GET |
| Reliability | 0.2 |
| Idempotency | safe |
