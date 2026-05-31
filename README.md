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

Health checks are exposed at:

```bash
curl http://127.0.0.1:8001/healthz
```

The endpoint verifies Django can reach the configured database and is intentionally public for Docker/load balancer health probes.

## Production Auth/SMS

OTP login is tenant-safe and provider-backed. In production set:

```bash
DEBUG=False
SMS_PROVIDER=your_provider_name
SMS_PROVIDER_API_URL=https://provider.example/send
SMS_PROVIDER_API_TOKEN=...
```

When `DEBUG=False`, the local SMS stub is rejected and OTP codes are never returned in API responses. OTPs are stored as HMAC digests, expire after 10 minutes, and only existing active tenant users can receive or verify login codes.

## Email Delivery

Report sharing and CA report bundles can send real read-only report links through Resend. Configure the provider through environment variables only:

```bash
EMAIL_PROVIDER=resend
RESEND_API_KEY=...
RESEND_FROM_EMAIL=reports@your-domain.example
EMAIL_REPLY_TO=owner@your-domain.example
```

Local development defaults to the `local_stub` provider. In production, Django deployment checks warn when email delivery is not connected, and fail if Resend is selected without the required key/from address.

## Production Provider Boundaries

Keep all third-party credentials in environment variables or deployment secrets. The workspace API returns only provider names, modes, configured flags, and missing variable names; it never returns token values.

Supported production provider settings:

```bash
PAYMENT_GATEWAY_PROVIDER=razorpay
RAZORPAY_KEY_ID=...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...

SHIPPING_PROVIDER=shiprocket
SHIPROCKET_API_URL=https://apiv2.shiprocket.in/v1/external
SHIPROCKET_EMAIL=...
SHIPROCKET_PASSWORD=...

WHATSAPP_PROVIDER=gupshup # or twilio
GUPSHUP_API_KEY=...
GUPSHUP_APP_NAME=...
GUPSHUP_SOURCE_NUMBER=...
```

If you use Twilio for WhatsApp, configure `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_WHATSAPP_FROM` instead of the Gupshup variables.

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

A production Docker Compose bundle, health checks, and Postgres backup/restore scripts live in `deploy/`. See `deploy/README.md` for the runbook.
