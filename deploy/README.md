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
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SMS_PROVIDER=<real provider>
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

## 3. Logs And Operations

```powershell
docker compose --env-file .env -f deploy/docker-compose.prod.yml logs -f backend
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec backend python manage.py check --deploy
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec backend python manage.py migrate --noinput
```

## 4. Backup Postgres

```powershell
.\deploy\backup_postgres.ps1
```

This creates a compressed `pg_dump` file in `deploy/backups`.

## 5. Restore Postgres

Restoring replaces existing database objects. Take a fresh backup first.

```powershell
.\deploy\restore_postgres.ps1 -InputFile .\deploy\backups\vastrabook-YYYYMMDD-HHMMSS.dump
```

For non-interactive restore jobs:

```powershell
.\deploy\restore_postgres.ps1 -InputFile .\deploy\backups\vastrabook-YYYYMMDD-HHMMSS.dump -Yes
```
