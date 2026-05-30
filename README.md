# VastraBook Backend

Django REST backend for **VastraBook by Veltacore**, a multi-tenant textile billing and inventory application.

## Local Setup

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8001
```

The backend is Postgres-first. Configure database and provider settings through environment variables, for example `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT`.

Copy `.env.example` to `.env` for local/prod configuration, then replace every `change-this-*` value before deployment.

## Production Auth/SMS

OTP login is tenant-safe and provider-backed. In production set:

```bash
DEBUG=False
SMS_PROVIDER=your_provider_name
SMS_PROVIDER_API_URL=https://provider.example/send
SMS_PROVIDER_API_TOKEN=...
```

When `DEBUG=False`, the local SMS stub is rejected and OTP codes are never returned in API responses. OTPs are stored as HMAC digests, expire after 10 minutes, and only existing active tenant users can receive or verify login codes.

## CI/CD

GitHub Actions are included:

- `Backend CI`: runs Django system checks, migration-drift checks, migrations, and the app test suite against a real Postgres service.
- `Backend Container CD`: builds and publishes a production Gunicorn image to GitHub Container Registry on `main`, tags, or manual dispatch.

Published image:

```text
ghcr.io/veltacore1/vectacore-bill-book-backend
```

The backend image expects environment variables for database credentials and provider configuration. Run migrations before or during deployment:

```bash
python manage.py migrate --noinput
```
