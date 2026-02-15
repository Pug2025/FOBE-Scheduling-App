# FOBE Scheduling App (v2 Auth)

FastAPI scheduler with PostgreSQL-backed authentication, organization roster persistence, and server-side DB sessions.

## Stack
- FastAPI
- SQLAlchemy
- Alembic
- PostgreSQL (Render)
- Jinja templates
- bcrypt password hashing

## Environment Variables
- `DATABASE_URL` (Render Internal Database URL)
- `SESSION_SECRET` (long random value)
- `BOOTSTRAP_TOKEN` (temporary first-admin bootstrap token)

## Local Run
1. Install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Run migrations:
   ```bash
   alembic upgrade head
   ```
3. Start server:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
   ```

## Auth + Session Model
- Passwords are stored with `bcrypt`.
- Session cookie stores only an opaque `session_id`.
- Session rows are persisted in the `sessions` table.
- Session expiration is 14 days.
- Cookie policy:
  - Local HTTP: `Secure=False`, `HttpOnly=True`, `SameSite=Lax`
  - Render HTTPS: `Secure=True`, `HttpOnly=True`, `SameSite=Lax`

## API Summary
- `POST /auth/bootstrap` (requires `X-Bootstrap-Token`, first user only)
- `POST /auth/login`
- `POST /auth/logout`
- `GET /auth/me`
- `GET /api/employees` (authenticated users)
- `PUT /api/employees` (admin only)
- `GET /api/admin/users` (admin only)
- `POST /api/admin/users` (admin only)
- `PATCH /api/admin/users/{id}` (admin only)
- `POST /generate` (authenticated; payload-driven scheduler)

## Render Deployment

### Automatic migration on deploy (recommended)
- Build Command:
  ```bash
  pip install -r requirements.txt
  ```
- Start Command:
  ```bash
  alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'
  ```

### Manual migration fallback
- Start Command:
  ```bash
  uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'
  ```
- Run migrations via Render Shell / one-off command:
  ```bash
  alembic upgrade head
  ```

## Post-Bootstrap Hardening
After you create the first admin with `POST /auth/bootstrap`:
- Remove or rotate `BOOTSTRAP_TOKEN` in Render environment variables.
- Redeploy so the old token can no longer be used.
- Keep the Render start command as:
  ```bash
  alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'
  ```

## Post-Deploy Smoke Check
Run this from your local machine after each deploy:
```bash
BASE_URL="https://your-service.onrender.com" \
ADMIN_EMAIL="admin@example.com" \
ADMIN_PASSWORD="your-admin-password" \
./scripts/post_deploy_smoke.sh
```

Checks performed:
- `GET /health` returns `200` and `{"ok": true}`
- `GET /auth/me` returns `401` while logged out
- `POST /auth/login` returns `200`
- `GET /auth/me` returns `200` after login

## Tests
```bash
pytest
```
