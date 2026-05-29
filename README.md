# vectacore-Bill-Book-Backend

Django REST backend for the Vectacore Bill Book application.

## Local Setup

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8001
```

The backend is Postgres-first. Configure database and provider settings through environment variables, for example `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT`.
