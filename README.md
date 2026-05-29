# vectacore-Bill-Book-Backend

Django REST backend for the Vectacore Bill Book application.

## Local Setup

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8001
```

The backend is Postgres-first. Configure database and provider settings through environment variables, for example `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT`.

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
