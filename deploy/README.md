# VastraBook Production Runbook

This deploy bundle runs the real multi-tenant app with Postgres as the only data source.

## 1. Prepare Environment

From the backend repo root:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and replace every placeholder secret. Production must use:

```text
DEBUG=False
DEMO_SESSION_ENABLED=False
CORS_ALLOW_ALL_ORIGINS=False
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
E_INVOICE_PROVIDER=<real provider or disabled>
```

The frontend image must be built with a browser-reachable API URL, for example:

```text
VITE_API_URL=https://api.vastrabook.in/api/v1
```

Set that GitHub Actions repository variable before publishing the frontend container.

## 2. Start The Stack

```powershell
docker compose --env-file .env -f deploy/docker-compose.prod.yml pull
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d
docker compose --env-file .env -f deploy/docker-compose.prod.yml ps
```

The backend service runs migrations before Gunicorn starts. Confirm health:

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

## 4. Logs And Operations

```powershell
docker compose --env-file .env -f deploy/docker-compose.prod.yml logs -f backend
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec backend python manage.py check --deploy
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec backend python manage.py migrate --noinput
```

## 5. Backup Postgres

```powershell
.\deploy\backup_postgres.ps1
```

This creates a compressed `pg_dump` file in `deploy/backups`.

## 6. Restore Postgres

Restoring replaces existing database objects. Take a fresh backup first.

```powershell
.\deploy\restore_postgres.ps1 -InputFile .\deploy\backups\vastrabook-YYYYMMDD-HHMMSS.dump
```

For non-interactive restore jobs:

```powershell
.\deploy\restore_postgres.ps1 -InputFile .\deploy\backups\vastrabook-YYYYMMDD-HHMMSS.dump -Yes
```
