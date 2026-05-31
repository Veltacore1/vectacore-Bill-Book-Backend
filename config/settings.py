import os
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-csm-silks-billing-software-key-2026")

DEBUG = os.getenv("DEBUG", "True") == "True"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "*").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    
    # Third party packages
    "rest_framework",
    "corsheaders",
    "django_filters",
    
    # Custom apps
    "apps.accounts.apps.AccountsConfig",
    "apps.parties",
    "apps.items",
    "apps.sales",
    "apps.purchases",
    "apps.payments",
    "apps.accounting",
    "apps.staff",
    "apps.business_tools",
    "apps.business_settings",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Custom Multi-Tenancy Middleware
    "apps.accounts.middleware.MultiTenantMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database configuration
# The app is Postgres-first because tenant data must be shared by the backend
# and frontend instead of living as local component state.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "csm_silks"),
        "USER": os.getenv("DB_USER", "csm_user"),
        "PASSWORD": os.getenv("DB_PASSWORD", "csm_password"),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5434"),
    }
}

# Explicit opt-in SQLite fallback for one-off local checks.
if os.getenv("USE_SQLITE", "False") == "True":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTH_USER_MODEL = "accounts.User"


LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# CORS configuration
CORS_ALLOW_ALL_ORIGINS = os.getenv("CORS_ALLOW_ALL_ORIGINS", "True" if DEBUG else "False") == "True"
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True

# Production security. Keep local development easy, but allow deployment to pass
# Django's security checks with explicit environment configuration.
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "False" if DEBUG else "True") == "True"
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "False" if DEBUG else "True") == "True"
CSRF_COOKIE_SECURE = os.getenv("CSRF_COOKIE_SECURE", "False" if DEBUG else "True") == "True"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SAMESITE = os.getenv("CSRF_COOKIE_SAMESITE", "Lax")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = os.getenv("SECURE_REFERRER_POLICY", "strict-origin-when-cross-origin")
X_FRAME_OPTIONS = os.getenv("X_FRAME_OPTIONS", "DENY")
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0" if DEBUG else "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv("SECURE_HSTS_INCLUDE_SUBDOMAINS", "False" if DEBUG else "True") == "True"
SECURE_HSTS_PRELOAD = os.getenv("SECURE_HSTS_PRELOAD", "False") == "True"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# REST Framework settings
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "apps.accounts.authentication.TenantJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
}

# Local demo session used by the React app to obtain a JWT for the seeded tenant.
DEMO_SESSION_ENABLED = os.getenv("DEMO_SESSION_ENABLED", "True") == "True"
DEMO_TENANT_MOBILE = os.getenv("DEMO_TENANT_MOBILE", "8608633066")

# External provider boundaries. Local development can use local_stub, but
# production should set provider names plus API endpoints/tokens so the app
# never silently marks a government/SMS lifecycle as real without a provider.
E_INVOICE_PROVIDER = os.getenv("E_INVOICE_PROVIDER", "local_stub" if DEBUG else "disabled").strip().lower()
E_INVOICE_API_URL = os.getenv("E_INVOICE_API_URL", "").strip()
E_INVOICE_API_TOKEN = os.getenv("E_INVOICE_API_TOKEN", "").strip()
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "local_stub" if DEBUG else "disabled").strip().lower()
SMS_PROVIDER_API_URL = os.getenv("SMS_PROVIDER_API_URL", "").strip()
SMS_PROVIDER_API_TOKEN = os.getenv("SMS_PROVIDER_API_TOKEN", "").strip()
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "local_stub" if DEBUG else "disabled").strip().lower()
RESEND_API_URL = os.getenv("RESEND_API_URL", "https://api.resend.com/emails").strip()
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev" if DEBUG else "").strip()
EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", "").strip()

# JWT settings
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
}
