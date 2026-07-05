# VastraBook Production Runbook

This deploy bundle runs the real multi-tenant app with Postgres as the only data source.

## 1. Prepare Environment

```powershell
Copy-Item deploy\.env.production.example .env
```

Or on Linux:

```bash
cp deploy/.env.production.example .env
```

Edit `.env` and replace every placeholder secret. Production must use:

```text
DEBUG=False
DEMO_SESSION_ENABLED=False
SEED_DEMO_DATA=False
CORS_ALLOW_ALL_ORIGINS=False
CORS_ALLOWED_ORIGINS=https://app.vastrabook.in
CSRF_TRUSTED_ORIGINS=https://app.vastrabook.in
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_REFERRER_POLICY=strict-origin-when-cross-origin
X_FRAME_OPTIONS=DENY
SMS_PROVIDER=<real provider>
EMAIL_PROVIDER=resend
RESEND_API_KEY=<real Resend key>
RESEND_FROM_EMAIL=<verified sender>
```

Set `SEED_DEMO_DATA=True` only for demo/staging deployments that need the CSM SILKS tenant auto-seeded after migrations.

The frontend image must be built with a browser-reachable API URL, for example:

```text
VITE_API_URL=https://api.vastrabook.in/api/v1
```

Set that GitHub Actions repository variable before publishing the frontend container.

## 2. Start The Stack

### Option A: Separate subdomains (default)

Frontend and backend each exposed on their own port. Use a load balancer or
separate subdomains (e.g. `app.vastrabook.in` -> port 8080, `api.vastrabook.in` -> port 8001).

```powershell
docker compose --env-file .env -f deploy/docker-compose.prod.yml pull
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d
docker compose --env-file .env -f deploy/docker-compose.prod.yml ps
```

### Option B: Single-server reverse proxy (recommended for VPS)

A single nginx container serves the frontend SPA and proxies `/api/*` to the
backend. Set `USE_REVERSE_PROXY=true` in `.env` and run:

```powershell
docker compose --env-file .env -f deploy/docker-compose.prod.yml pull
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d
docker compose --env-file .env -f deploy/docker-compose.prod.yml --profile reverse-proxy up -d nginx-reverse-proxy
```

The reverse proxy listens on ports 80/443. For SSL, pair with a companion like
Caddy, or mount certs and uncomment SSL lines in `deploy/nginx/reverse-proxy.conf`.

### Health check

```powershell
Invoke-RestMethod http://127.0.0.1:8001/healthz
```

Expected response:

```json
{
  "success": true,
  "status": "ok",
  "app": "VastraBook",
  "database": "ok"
}
```

## 3. Run Production Smoke Check

After the stack is healthy, run the deployment smoke checker from the backend repo root:

```powershell
python .\deploy\smoke_check.py `
  --backend-url http://127.0.0.1:8001 `
  --frontend-url http://127.0.0.1:8080 `
  --expected-api-origin https://api.vastrabook.in
```

For a seeded demo deployment, also verify that the demo tenant secure cookie auth flow is alive:

```powershell
python .\deploy\smoke_check.py `
  --backend-url http://127.0.0.1:8001 `
  --frontend-url http://127.0.0.1:8080 `
  --expected-api-origin https://api.vastrabook.in `
  --demo-mobile 8608633066
```

The smoke check verifies backend `/healthz`, database reachability, backend security headers, frontend app shell, frontend CSP/cache/security headers, scoped `connect-src` for the expected API origin, immutable asset caching, and, when `--demo-mobile` is supplied, that demo login returns only an access token in JSON while the refresh token is set as an HttpOnly cookie. Use `--skip-frontend-security` only against a local Vite dev server, not a production Nginx deployment.

## 4. Seed Demo Data (Optional)

```powershell
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec backend python seed_data.py
```

## 5. Logs And Operations

```powershell
# Backend
docker compose --env-file .env -f deploy/docker-compose.prod.yml logs -f backend

# Celery worker (background tasks)
docker compose --env-file .env -f deploy/docker-compose.prod.yml logs -f celery-worker

# Django deployment checks
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec backend python manage.py check --deploy

# Apply migrations
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec backend python manage.py migrate --noinput
```

## 6. Backup Postgres

```powershell
.\deploy\backup_postgres.ps1
```

Linux/macOS:

```bash
./deploy/backup_postgres.sh
```

This creates a compressed `pg_dump` file in `deploy/backups`.

## 7. Restore Postgres

Restoring replaces existing database objects. Take a fresh backup first.

```powershell
.\deploy\restore_postgres.ps1 -InputFile .\deploy\backups\vastrabook-YYYYMMDD-HHMMSS.dump
```

Linux/macOS:

```bash
CONFIRM_RESTORE=RESTORE ./deploy/restore_postgres.sh ./deploy/backups/vastrabook-YYYYMMDD-HHMMSS.dump
```

For non-interactive restore jobs:

```powershell
.\deploy\restore_postgres.ps1 -InputFile .\deploy\backups\vastrabook-YYYYMMDD-HHMMSS.dump -Yes
```

## Architecture

```
                         ┌──────────────┐
                         │   Browser    │
                         └──────┬───────┘
                                │
                    ┌───────────┴───────────┐
                    │                       │
             (Option A)              (Option B)
       app.example.com          example.com
       api.example.com              │
                    │               │
                    │    ┌──────────┴──────────┐
                    │    │ nginx-reverse-proxy │
                    │    │  (port 80/443)      │
                    │    └──┬─────────────┬────┘
                    │       │             │
            ┌───────┴──┐  ┌┴──────┐  ┌───┴────┐
            │ Frontend │  │Frontend│  │ Backend│
            │ :8080    │  │ :80   │  │ :8001  │
            └──────────┘  └───────┘  └────────┘
                         ┌───────┐  ┌────────┐
                         │ Redis │  │Postgres│
                         └───────┘  └────────┘
                         ┌──────────────┐
                         │celery-worker │
                         └──────────────┘
```
