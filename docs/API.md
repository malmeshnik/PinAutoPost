# PinAutoPost API reference

PinAutoPost publishes pins to Pinterest on behalf of Pinterest accounts that it
stores (browser cookies + a proxy per account). This document describes every
HTTP endpoint, the exact request and response shapes, and how a client should
react to each outcome. It is written to be given to an automated client (for
example an LLM agent) as its operating manual for this service.

All paths below are relative to the service root, e.g.
`https://pinautopost.example.com`. All request and response bodies are JSON
(`Content-Type: application/json`) unless stated otherwise.

## Contents

1. [Concepts](#1-concepts)
2. [Authentication](#2-authentication)
3. [Endpoints](#3-endpoints)
   - [POST /api/v1/auth/token/](#post-apiv1authtoken) — get an API token
   - [GET /api/v1/accounts/](#get-apiv1accounts) — list accounts
   - [POST /api/v1/accounts/](#post-apiv1accounts) — add an account
   - [GET /api/v1/accounts/{id}/](#get-apiv1accountsid) — one account
   - [PATCH /api/v1/accounts/{id}/](#patch-apiv1accountsid) — change an account
   - [DELETE /api/v1/accounts/{id}/](#delete-apiv1accountsid) — delete an account
   - [GET /api/v1/accounts/{id}/boards/](#get-apiv1accountsidboards) — list boards
   - [POST /api/v1/publish/{webhook_token}/](#post-apiv1publishwebhook_token) — publish a pin
   - [GET /api/v1/publish/status/{status_token}/](#get-apiv1publishstatusstatus_token) — publish status
4. [Error codes](#4-error-codes)
5. [Recipes](#5-recipes)
6. [Limits and rules](#6-limits-and-rules)

---

## 1. Concepts

| Term | Meaning |
|---|---|
| **Account** | A Pinterest account stored in the service: its Pinterest username (`name`), its browser cookies and the proxy all its requests go through. Has an integer `id`. |
| **Board** | A Pinterest board of an account. Identified by Pinterest's `board_id` (a string) and its `name`. The service caches the board list of every account. |
| **webhook_token** | A UUID unique to each account. Knowing it is enough to publish pins to that account, so treat it as a secret. Returned as part of `webhook_url`. |
| **Task** | One publish request. Has a `task_id` and a private `status_token` (UUID) used to read its status. |
| **Idempotency-Key** | A client-chosen string that makes publishing safe to retry: the same key + same body never publishes twice. |

The usual flow is: get a token → add an account (once) → remember its
`webhook_url` and board IDs → publish pins to the `webhook_url`.

## 2. Authentication

There are two kinds of endpoints:

| Endpoints | Authentication |
|---|---|
| `/api/v1/auth/token/` | Username and password in the body. |
| `/api/v1/accounts/...` | Required. Send `Authorization: Token <token>`. |
| `/api/v1/publish/...` | None. The `webhook_token` / `status_token` in the URL is the credential. |

A token belongs to a user of the service. With a token you see and manage only
that user's accounts (a superuser's token sees all accounts). Accounts you
create with a token belong to the token's user.

Missing or invalid token on an `/accounts/` endpoint:

```http
HTTP/1.1 401 Unauthorized
{"detail": "Authentication credentials were not provided."}
```

(or `"Invalid token."` when a wrong token is sent)

(`detail` text is localized, e.g. Russian, depending on the server language.)

---

## 3. Endpoints

### POST /api/v1/auth/token/

Exchanges a username and password for an API token. Calling it again returns
the same token. Limited to **10 requests per minute** per client.

Request:

```json
{"username": "admin", "password": "secret"}
```

Responses:

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"token": "9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b"}` | Use as `Authorization: Token 9944b0...`. |
| `400` | `{"non_field_errors": ["Unable to log in with provided credentials."]}` | Wrong username or password. |
| `429` | `{"detail": "Request was throttled. Expected available in 42 seconds."}` | Too many attempts; wait. |

Tokens can also be issued and revoked by an administrator in the admin panel
(section "Tokens").

---

### GET /api/v1/accounts/

Lists the accounts of the token's user. Not paginated: returns a JSON array.

```json
[
  {
    "id": 3,
    "name": "kitchen_pins",
    "is_active": true,
    "webhook_token": "0b5c3c9e-3f2a-4b8e-9a51-6a1d2a7f4c10",
    "webhook_url": "https://pinautopost.example.com/api/v1/publish/0b5c3c9e-3f2a-4b8e-9a51-6a1d2a7f4c10/",
    "boards": [
      {"id": 17, "board_id": "1046172331540170834", "name": "Recipes", "url": "/kitchen_pins/recipes/"}
    ],
    "created_at": "2026-10-15T14:02:11.523Z"
  }
]
```

| Field | Notes |
|---|---|
| `id` | Integer account ID used in `/accounts/{id}/` paths. |
| `name` | The Pinterest username. |
| `is_active` | `false` means Pinterest rejected the cookies; publishing to this account fails with `account_inactive` until fixed (see [Recipes](#5-recipes)). |
| `webhook_url` | Where to POST pins for this account. |
| `boards[].board_id` | Pinterest's board ID; use it as `board_id` when publishing. `boards[].id` is an internal ID — do not use it for publishing. |

`cookies` and `proxy` are write-only and never returned.

---

### POST /api/v1/accounts/

Adds a Pinterest account. Before saving, the service:

1. normalizes the cookies and checks the login cookies are present;
2. normalizes the proxy and checks a connection through it;
3. loads the account's boards from Pinterest.

If any step fails, **nothing is saved** and the request can be retried after
fixing the input. On success the account is saved together with its boards.
This request makes network calls and can take up to ~40 seconds.

Request:

```json
{
  "name": "kitchen_pins",
  "cookies": [ ...cookie export... ],
  "proxy": "203.0.113.10:8000:login:password",
  "proxy_type": "socks5"
}
```

| Field | Required | Description |
|---|---|---|
| `name` | yes | The **Pinterest username**, exactly as in `pinterest.com/<username>/`. Boards are loaded by this name, so a wrong name fails with `no_boards` or `api_error`. Max 255 chars. |
| `cookies` | yes | Cookies of a browser session logged in to Pinterest, in any format below. Must contain `csrftoken` and `_pinterest_sess`. |
| `proxy` | yes | Proxy for all Pinterest requests of this account, in any format below. |
| `proxy_type` | no | One of `http`, `https`, `socks4`, `socks5`, `socks5h` (`socks5h` resolves DNS through the proxy). Overrides the scheme of `proxy`. If omitted: the scheme from a full URL, otherwise `http`. |

**Accepted `cookies` formats** (all are converted to one internal format):

- The JSON array exported by a browser extension (Cookie-Editor, EditThisCookie), as is:
  `[{"name": "csrftoken", "value": "...", "domain": "www.pinterest.com", "path": "/", "expirationDate": 1820654705.1, ...}, ...]`
- An object with the Pinterest URL and that array. The URL selects the Pinterest
  domain used for requests (e.g. `ru.pinterest.com`):
  `{"url": "https://www.pinterest.com", "cookies": [...]}`
- A Cookie header string: `"csrftoken=...; _pinterest_sess=...; _auth=1"`
- The text of a Netscape `cookies.txt` file.
- Any of the above as a JSON-encoded string.

Cookies of other sites, expired cookies and duplicates are dropped
automatically; there is no need to clean the export.

**Accepted `proxy` formats** (stored as `scheme://login:password@host:port`;
special characters in the login and password are encoded automatically):

| Format | Example |
|---|---|
| `host:port:login:password` | `203.0.113.10:8000:login:password` |
| `login:password:host:port` | `login:password:203.0.113.10:8000` |
| `login:password@host:port` | `login:password@203.0.113.10:8000` |
| `host:port` | `203.0.113.10:8000` |
| full URL | `socks5://login:password@203.0.113.10:8000` |

Responses:

| Status | Body | Meaning / what to do |
|---|---|---|
| `201` | The account object (same shape as in the list), with `boards` filled. | Store `id`, `webhook_url` and the boards. |
| `400` | `{"cookies": ["Missing Pinterest cookies: _pinterest_sess. Export the cookies while logged in to pinterest.com."]}` | Cookie input is unusable. Ask for a fresh export from a logged-in browser. |
| `400` | `{"proxy": ["The proxy port must be a number from 1 to 65535."]}` | Proxy string cannot be parsed (wrong format, bad port, unsupported scheme). Fix the format. |
| `400` | `{"proxy": "<diagnostic>"}` | The proxy could not be reached. The message says why: authentication rejected, host not found, connection refused or timeout. Fix the proxy details. |
| `400` | `{"account": {"error": "auth_error", "message": "..."}}` | Pinterest rejected the cookies. Ask for fresh cookies. |
| `400` | `{"account": {"error": "no_boards", "message": "..."}}` | No boards found. Check `name` is the Pinterest username and the account has at least one board. |
| `400` | `{"account": {"error": "api_error" \| "proxy_error" \| "timeout_error", "message": "..."}}` | Pinterest or the proxy failed while loading boards. Retry later; check `name` if `api_error` repeats. |
| `400` | `{"name": ["This field is required."]}` (any field) | Missing or invalid field. |

Note that field errors are usually arrays of strings, but the proxy
reachability diagnostic is a plain string. Handle both.

---

### GET /api/v1/accounts/{id}/

Returns one account (same shape as in the list). `404` if the account does not
exist or belongs to another user.

---

### PATCH /api/v1/accounts/{id}/

Changes `name`, `proxy`, `proxy_type` or `cookies`. Send only the fields to
change. The same formats as in `POST` apply. A changed proxy is checked for
connectivity (`400 {"proxy": "<diagnostic>"}` on failure). Sending only
`proxy_type` changes the type of the stored proxy.

**Limitation:** new `cookies` sent here are normalized and stored, but they are
**not** checked against Pinterest, and the account's `is_active` flag is **not**
changed (`is_active` is read-only in the API). An account deactivated because
its cookies expired stays inactive; an administrator must update its cookies
in the admin panel, which checks them and reactivates the account.

`PUT` is also available and requires all writable fields.

---

### DELETE /api/v1/accounts/{id}/

Deletes the account, its boards and its publish history. Returns `204` with an
empty body. The account's `webhook_url` stops working immediately.

---

### GET /api/v1/accounts/{id}/boards/

Returns the account's cached boards as an array of
`{"id", "board_id", "name", "url"}`.

| Query | Effect |
|---|---|
| (none) or `?refresh=false` | Returns the cached list. Fast, no Pinterest call. |
| `?refresh=true` | Reloads the boards from Pinterest first, then returns them. Use after boards were created or renamed on Pinterest. |

Errors with `?refresh=true`:
`{"error": "<code>", "message": "..."}` with the status from
[Error codes](#4-error-codes), e.g. `401 auth_error` (cookies expired; the
account is deactivated), `502 proxy_error`. Any other `refresh` value:
`400 {"error": "invalid_refresh", ...}`.

Boards are also refreshed automatically every night, and on publish when the
requested board is not in the cache.

---

### POST /api/v1/publish/{webhook_token}/

Publishes one pin to the account that owns `webhook_token`. No `Authorization`
header is needed.

Request body:

```json
{
  "title": "Creamy tomato soup",
  "description": "Ready in 20 minutes.",
  "link": "https://example.com/tomato-soup",
  "image_url": "https://example.com/images/soup.jpg",
  "board_id": "1046172331540170834"
}
```

| Field | Required | Description |
|---|---|---|
| `image_url` | yes | Public URL of the image. Max 512 chars. Must be reachable by Pinterest. |
| `board_id` | one of `board_id` / `board_name` | Pinterest board ID (from `boards[].board_id`). Preferred: unambiguous. |
| `board_name` | one of `board_id` / `board_name` | Board name, case-insensitive. Fails if two boards share the name. |
| `title` | no | Max **100** characters (Pinterest limit). |
| `description` | no | Pin description. |
| `link` | no | Destination URL of the pin. Max 512 chars. |

If both `board_id` and `board_name` are given, `board_id` is used. Omitted
fields, `null` and `""` are treated the same.

Headers:

| Header | Required | Description |
|---|---|---|
| `Idempotency-Key` | strongly recommended | 1–255 chars, unique per logical pin (e.g. a UUID, or `campaign-42-pin-7`). Makes retries safe. Without it every request is a new publication, so a retry after a network error can create a duplicate pin. |

Query parameters:

| Query | Effect |
|---|---|
| (none) or `?wait=true` | **Synchronous** (default). The request waits for Pinterest, up to 60 seconds. |
| `?wait=false` | **Asynchronous**. Returns `202` immediately; a background worker publishes; poll the status URL. |

#### Synchronous response (`wait=true`, default)

The service returns **Pinterest's own response** unchanged (status code,
body, `Content-Type`), plus two headers:

| Header | Value |
|---|---|
| `X-PinAutoPost-Task-ID` | Task ID. |
| `X-PinAutoPost-Status-URL` | Private status URL of this task. |

Success (`200`), Pinterest's body. The new pin's ID is at
`resource_response.data.id`:

```json
{"resource_response": {"data": {"id": "1046172400012345678", ...}, ...}}
```

Failure: either Pinterest's own error body and status (for example `400` with
`{"resource_response": {"error": {...}}}`; the task records it as
`api_error`), or a PinAutoPost error body when the failure happened before or
around the Pinterest call:

```json
{"error": "proxy_error", "message": "...", "retryable": true, "retry_count": 2}
```

| Field | Meaning |
|---|---|
| `error` | Code from [Error codes](#4-error-codes). |
| `message` | Human-readable explanation. |
| `retryable` | `true` → resending the **same request with the same Idempotency-Key** will try publishing again. `false` → resending returns this same response. |
| `retry_count` | Present when the service already retried automatically within the request. |

**How to read the result:** treat `2xx` with `resource_response.data.id` as
success; anything else as failure, and use `error` + `retryable` when present.

#### Asynchronous response (`wait=false`)

```http
HTTP/1.1 202 Accepted
{"status": "accepted", "task_id": "58", "status_url": "https://.../api/v1/publish/status/5f0c.../"}
```

Then poll `status_url` (see the next endpoint), e.g. every 3–5 seconds, until
`status` is `success` or `failed`. If the queue is down the response is
`503` with `"status": "failed", "error": "queue_error"`; it is retryable.

#### Repeating a request with the same Idempotency-Key

| State of the earlier request | Result of the repeat |
|---|---|
| Succeeded | Same stored response; nothing is published again. |
| Failed with `retryable: true` | The pin is published again (new attempt). |
| Failed with `retryable: false` | Same stored error response; nothing is published again. |
| Still running | Waits for it (up to 60 s), then returns its result; if still running: `409 request_in_progress`. |
| Its process died mid-publish | `504 publish_unknown`. |
| Same key, **different body** | `409 idempotency_key_conflict`. |

#### Request-level errors (no task is created)

| Status | Body | Meaning |
|---|---|---|
| `404` | `{"detail": "Not found."}` | Unknown `webhook_token`. |
| `400` | `{"error": "account_inactive", "message": "Account is inactive."}` | Account's cookies were rejected earlier. Do not retry until the account is fixed. |
| `400` | `{"image_url": ["Enter a valid URL."]}`, `{"title": ["Ensure this field has no more than 100 characters."]}` etc. | Invalid field. |
| `400` | `{"non_field_errors": ["Either board_id or board_name must be provided."]}` | No board given. |
| `400` | `{"error": "invalid_wait", ...}` | `wait` is not `true`/`false`. |
| `400` | `{"error": "invalid_idempotency_key", ...}` | Key longer than 255 chars. |
| `409` | `{"error": "idempotency_key_conflict", ...}` | Key reused with a different body. Use a new key for a different pin. |
| `409` | `{"error": "request_in_progress", ...}` | A request with this key is still running. Retry the same request later. |

---

### GET /api/v1/publish/status/{status_token}/

Returns the state of a publish task. No authentication; the URL comes from
`status_url` or the `X-PinAutoPost-Status-URL` header.

| `status` | HTTP | Body |
|---|---|---|
| `pending` | `200` | `{"task_id": "58", "status": "pending"}` |
| `processing` | `200` | `{"task_id": "58", "status": "processing"}` |
| `success` | `200` | `{"task_id": "58", "status": "success", "message": "Pin published successfully"}` |
| `failed` | per error code | `{"task_id": "58", "status": "failed", "error": "auth_error", "message": "...", "retry_count": 1}` |

For `failed`, the HTTP status follows [Error codes](#4-error-codes). This
endpoint does not include the Pinterest pin ID; use the synchronous mode if
you need it.

Legacy routes `/api/v1/task/{task_id}/` and `/api/v1/publish/status/{task_id}/`
(integer ID) return only `{"task_id", "status"}` and, for failures, `"error"`.
Prefer the token URL.

---

## 4. Error codes

| `error` | HTTP | Retryable | Meaning | What the client should do |
|---|---|---|---|---|
| `validation_error` | 400 | no | Request is invalid for this account (e.g. `board_id` not of this account, board name not found or ambiguous, title too long). | Fix the request. Use `GET /accounts/{id}/boards/?refresh=true` to get valid boards. |
| `no_boards` | 400 | no | Pinterest returned no boards. | Check the account name / create a board. |
| `account_inactive` | 400 | no | Account disabled after Pinterest rejected its cookies. | Stop publishing to it; an administrator must update the cookies. |
| `auth_error` | 401 | no | Pinterest rejected the cookies now; the account was deactivated. | Same as `account_inactive`. |
| `proxy_error` | 502 | **yes** | Proxy unreachable or Pinterest answered 5xx. The pin was not created. | Retry the same request with the same key after a pause (e.g. 1–5 min). If it persists, the proxy needs fixing. |
| `timeout_error` | 504 | **yes** | The 60-second deadline ran out before the pin request was sent. | Retry with the same key. |
| `queue_error` | 503 | **yes** | Background queue unavailable (`wait=false` only). | Retry with the same key, or use `wait=true`. |
| `api_error` | 502, or Pinterest's own status | no | Pinterest rejected the pin (bad image URL, policy, etc.). | Inspect the message/body; change the input and send it with a **new** key. |
| `publish_unknown` | 504 | **no** | The connection broke after the pin request was sent: Pinterest **may have created** the pin. | Do **not** blindly publish again. Check the board on Pinterest; only if the pin is absent, publish again with a **new** key. |
| `internal_error` | 500 | no | Unexpected server error. | Report it; retry later with a new key. |

Automatic retries: for `proxy_error` and `timeout_error` the service already
retries up to 3 times within one request when time allows (`retry_count`
shows how many).

## 5. Recipes

**Add an account and publish a first pin**

1. `POST /api/v1/auth/token/` → `token`.
2. `POST /api/v1/accounts/` with `Authorization: Token <token>`, the Pinterest
   username, the cookie export and the proxy → `201`. Save `webhook_url` and
   pick a `boards[].board_id`.
3. `POST <webhook_url>` with `Idempotency-Key: <new uuid>` and
   `{"image_url": ..., "board_id": ..., "title": ...}`.
4. `200` + `resource_response.data.id` → done. Otherwise follow
   [Error codes](#4-error-codes).

**Retry safely after a network failure on the client side**

Resend the exact same body with the exact same `Idempotency-Key`. The service
either returns the stored result or, if the earlier attempt failed in a
retryable way, publishes again. It never publishes twice for one key.

**Board not found**

Call `GET /api/v1/accounts/{id}/boards/?refresh=true`, pick the board from the
fresh list and publish with its `board_id`.

**Account became inactive (`account_inactive` / `auth_error`)**

The Pinterest session expired. Stop sending pins to this account. New cookies
must be exported from a browser logged in to that Pinterest account and entered
by an administrator in the admin panel (account page → "Cookies"), which checks
them and reactivates the account. The API alone cannot reactivate it.

**Publish many pins**

Use `?wait=false`, keep each `status_url`, and poll them. Give every pin its
own `Idempotency-Key`.

## 6. Limits and rules

- Publishing has a hard 60-second deadline per request (`wait=true`).
- `title` ≤ 100 characters; `image_url`, `link` ≤ 512 characters.
- `POST /api/v1/auth/token/`: 10 requests per minute.
- `webhook_url`, `status_url` and API tokens are secrets: anyone with them can
  publish, read task status, or manage accounts.
- Timestamps are ISO 8601 in UTC.
