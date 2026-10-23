# Admin Users & Session Invalidation

> Extends the [auth and secrets scenario](./quality-guidelines.md#scenario-auth-and-secrets-environment-contract).
> Where they differ, this file wins for: JWT payload (`ver`), `get_current_user` checks, login of disabled users.

---

## Scenario: Admin role, user management and token versioning

### 1. Scope / Trigger

Admin bootstrap, `/admin/users` endpoints, self-service password change, and invalidating sessions
via `users.token_version`. Trigger: changing `app/core/{security,deps,config}.py`,
`app/services/{auth_service,admin_service}.py`, `app/api/v1/{auth,admin}.py`, the `users` table,
or anything that changes a user's password / active state.

### 2. Signatures

```python
# backend/app/models/entities.py — User (migration 0003_user_admin_fields, down_revision '0002')
is_admin: bool       # server_default false
is_active: bool      # server_default true
token_version: int   # server_default 0

# backend/app/core/security.py
def create_access_token(user_id: int, token_version: int, secret: str, expire_minutes: int) -> str
def decode_access_token(token: str, secret: str) -> tuple[int, int] | None   # (user_id, ver)

# backend/app/core/deps.py
async def get_current_user(...) -> User          # CurrentUser
async def get_admin_user(user: CurrentUser) -> User   # AdminUser

# backend/app/core/config.py
admin_username: str | None = None   # "" treated as None
admin_password: str | None = None
@property admin_credentials -> Credentials | None

# backend/app/services/auth_service.py
async def create_user(db, creds: Credentials, *, is_admin: bool = False) -> User   # shared by register + admin create
async def change_password(db, user: User, data: ChangePasswordRequest) -> User
async def bootstrap_admin(sessionmaker, settings: Settings) -> None              # called in main.py lifespan

# backend/app/services/admin_service.py
list_users(db, *, q, page, page_size) -> AdminUserList
create_user(db, data) / update_user(db, actor, user_id, data) / reset_password(db, user_id, data) / delete_user(db, actor, user_id)
```

### 3. Contracts

- JWT payload: `{"sub", "ver", "iat", "exp"}`. Missing `ver` (tokens issued before 0003) → `0`,
  matching the backfilled default, so upgrading does not log anyone out. Non-int or bool `ver` → invalid.
- `get_current_user`: user missing, `ver != user.token_version`, or `not user.is_active`
  → 401 `未登录或登录已过期` (frontend already maps 401 → clear auth + redirect to login).
- `token_version` is bumped (in SQL, see §7) by: admin reset-password, admin PATCH `is_active: true→false`,
  self change-password. Not bumped by: role changes, re-enabling.
- Env: `ADMIN_USERNAME` / `ADMIN_PASSWORD` optional, must be set together, validated with the
  `Credentials` rules; passed through `docker-compose.yml`, `.env.example`, `backend/.env.example`.
  Settings uses `hide_input_in_errors=True` so validation errors never echo secrets.
- Bootstrap (lifespan, after `alembic upgrade head`): missing → create with `is_admin=True`;
  existing → set `is_admin=True, is_active=True`, **never touch the password**; concurrent create
  (IntegrityError) → promote the existing row. Logs only the username. Ignores `REGISTRATION_ENABLED`.
- Endpoints (`/api/v1`):

| Method & path | Body / query | Success |
|---|---|---|
| `GET /auth/me` (also login/register) | — | `UserOut {id, username, is_admin, created_at}` |
| `POST /auth/change-password` | `{old_password (1–200 chars), new_password (Password rules)}` | 204 + new `Set-Cookie` for the current session |
| `GET /admin/users` | `q` (≤64, case-insensitive substring, `%`/`_` literal), `page ≥1`, `page_size 1–100` (default 20) | `{items: AdminUserOut[], total}` ordered by id |
| `POST /admin/users` | `{username, password, is_admin=false}` | 201 `AdminUserOut` |
| `PATCH /admin/users/{id}` | `{is_admin?, is_active?}` | `AdminUserOut` |
| `POST /admin/users/{id}/reset-password` | `{new_password}` | 204 |
| `DELETE /admin/users/{id}` | — | 204 (projects/chapters/AI configs removed by FK CASCADE) |

  `AdminUserOut = {id, username, is_admin, is_active, created_at, project_count}`. Admins cannot read
  other users' novel content — only the counts.

### 4. Validation & Error Matrix

| Condition | Result |
|---|---|
| only one of `ADMIN_USERNAME`/`ADMIN_PASSWORD` set, or invalid format | `ValidationError` at startup, password not echoed |
| login with correct password but `is_active=false` | 403 `账号已被禁用，请联系管理员` (wrong password stays 401, status not leaked) |
| stale `ver` / disabled user on any authed request | 401 `未登录或登录已过期` |
| non-admin calls `/admin/*` | 403 `需要管理员权限` |
| target user not found | 404 `用户不存在` |
| duplicate username on admin create | 409 `用户名已存在` |
| actor disables / demotes / deletes self | 400 `不能对自己执行该操作` (no-op PATCH on self is allowed) |
| operation would leave zero active admins | 409 `至少需要保留一名启用中的管理员` |
| change-password with wrong old password | 400 `原密码不正确` |
| out-of-range `page`/`page_size`/`q`, invalid password | 422 |

### 5. Good / Base / Bad Cases

- Good: admin resets Bob's password → Bob's old cookie gets 401 on the next request; new password logs in.
- Base: user changes own password → current tab stays logged in (cookie re-issued with new `ver`), other browsers get 401.
- Bad: restarting with `ADMIN_PASSWORD` changed in `.env` and expecting the password to change — bootstrap never overwrites; use the UI.

### 6. Tests Required

- `backend/tests/test_admin.py` — bootstrap (create, idempotent, no password overwrite, promote existing,
  pair validation, no-op when unset, lifespan calls it); 403/401/404; list paging/search/project_count;
  create with registration disabled + login + is_admin + 409; reset → old session 401; disable → 401 and
  login 403; enable → login ok; `test_legacy_token_without_ver_still_valid`; `test_token_with_bad_ver_rejected`;
  self guardrails; last-admin guardrail (service-level — unreachable sequentially over HTTP because the
  actor is always a remaining active admin); delete cascades; `test_change_password_keeps_current_session`.
- `backend/tests/test_migrations.py::test_0003_backfills_existing_users` — rows from 0002 get
  `is_admin=false, is_active=true, token_version=0`; `downgrade -1` returns to 0002.

### 7. Wrong vs Correct

Wrong — read-modify-write in Python loses an increment when reset and disable race:

```python
user.token_version += 1
```

Correct — increment in SQL (`admin_service.py`, `auth_service.change_password`), then commit + refresh
before re-signing the cookie:

```python
user.token_version = User.token_version + 1
```

Wrong — checking "is there another admin" with a plain SELECT lets two admins demote each other concurrently.

Correct (`admin_service._ensure_other_active_admin`) — lock the active-admin set in the same transaction
as the update (PostgreSQL re-evaluates after the lock wait; SQLite ignores it but serializes anyway):

```python
await db.scalars(select(User.id).where(User.is_admin.is_(True), User.is_active.is_(True)).with_for_update())
```
