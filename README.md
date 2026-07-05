# VastraBook Backend

Django REST backend for **VastraBook by Veltacore**, a multi-tenant textile billing and inventory application.

## Local Setup

Requires **Python 3.12** (see `.python-version`). Python 3.14 is not supported because `django-filter` still relies on removed stdlib APIs.

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo_data
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

Successful login, registration, and demo-session responses set the refresh token as an HttpOnly cookie and return only the short-lived access token in JSON. The React app keeps access tokens in memory and refreshes through:

```bash
POST /api/v1/auth/token/refresh
```

Cookie-backed refresh and logout require a CSRF token. Browser clients should call `GET /api/v1/auth/csrf`, keep the returned `csrfToken` in memory, and send it as `X-CSRFToken` on unsafe API requests.

Configure cookie scope through `AUTH_REFRESH_COOKIE_NAME`, `AUTH_REFRESH_COOKIE_PATH`, `AUTH_REFRESH_COOKIE_SAMESITE`, and `AUTH_REFRESH_COOKIE_SECURE`. Production should keep `AUTH_REFRESH_COOKIE_SECURE=True` behind HTTPS.

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
RAZORPAY_API_URL=https://api.razorpay.com/v1
RAZORPAY_KEY_ID=...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...

SHIPPING_PROVIDER=shiprocket
SHIPROCKET_API_URL=https://apiv2.shiprocket.in/v1/external
SHIPROCKET_EMAIL=...
SHIPROCKET_PASSWORD=...
SHIPROCKET_PICKUP_LOCATION=Primary
SHIPROCKET_DEFAULT_LENGTH_CM=30
SHIPROCKET_DEFAULT_BREADTH_CM=24
SHIPROCKET_DEFAULT_HEIGHT_CM=5
SHIPROCKET_DEFAULT_WEIGHT_KG=0.5

WHATSAPP_PROVIDER=gupshup # or twilio
GUPSHUP_API_KEY=...
GUPSHUP_APP_NAME=...
GUPSHUP_SOURCE_NUMBER=...
MESSAGING_WEBHOOK_SECRET=...
```

If you use Twilio for WhatsApp, configure `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_WHATSAPP_FROM` instead of the Gupshup variables.
Optionally override `TWILIO_API_URL` for test doubles; production defaults to `https://api.twilio.com`.
Configure provider delivery receipts to `POST /api/v1/business-tools/webhooks/messaging/<provider>/` with either `Authorization: Bearer $MESSAGING_WEBHOOK_SECRET`, `X-VastraBook-Webhook-Secret`, or `X-VastraBook-Signature: sha256=<hmac_sha256_raw_body>`.

Provider readiness can be checked without printing token values:

```bash
python manage.py integration_smoke --json
```

Production e-invoice and e-way bill readiness are checked independently. Configure `E_INVOICE_PROVIDER`, `E_INVOICE_API_URL`, `E_INVOICE_API_TOKEN`, plus `E_WAY_BILL_PROVIDER`, `E_WAY_BILL_API_URL`, and `E_WAY_BILL_API_TOKEN` before running deployment checks.

Live provider calls require an explicit network opt-in. For example, after configuring rotated Resend credentials in the environment:

```bash
python manage.py integration_smoke --allow-network --email-to owner@example.com
```

Public/auth surfaces are scoped-throttled in production. Tune `THROTTLE_TENANT_REGISTER`, `THROTTLE_AUTH_OTP`, `THROTTLE_AUTH_VERIFY`, `THROTTLE_DEMO_SESSION`, `THROTTLE_PAYMENT_WEBHOOK`, `THROTTLE_MESSAGING_WEBHOOK`, and `THROTTLE_PUBLIC_SHARE` for your traffic profile.

## CI/CD

GitHub Actions are included:

- `Backend CI`: runs a tracked-file secret scan, Django system checks, migration-drift checks, migrations, and the app test suite against a real Postgres service.
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
