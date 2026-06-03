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

# CORS / CSRF configuration
def csv_env(name: str) -> list[str]:
    return [
        value.strip()
        for value in os.getenv(name, "").split(",")
        if value.strip()
    ]


LOCAL_DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]


def unique_origins(origins: list[str]) -> list[str]:
    return list(dict.fromkeys(origins))


CORS_ALLOW_ALL_ORIGINS = os.getenv("CORS_ALLOW_ALL_ORIGINS", "True" if DEBUG else "False") == "True"
CORS_ALLOWED_ORIGINS = csv_env("CORS_ALLOWED_ORIGINS")
if DEBUG and not CORS_ALLOW_ALL_ORIGINS:
    CORS_ALLOWED_ORIGINS = unique_origins(CORS_ALLOWED_ORIGINS + LOCAL_DEV_ORIGINS)
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
CSRF_TRUSTED_ORIGINS = csv_env("CSRF_TRUSTED_ORIGINS")
if DEBUG:
    CSRF_TRUSTED_ORIGINS = unique_origins(CSRF_TRUSTED_ORIGINS + LOCAL_DEV_ORIGINS)

# Browser auth hardening. Access tokens are short lived and returned to the
# React app, while refresh tokens are also set as HttpOnly cookies so the app
# does not need to persist them in localStorage.
AUTH_REFRESH_COOKIE_NAME = os.getenv("AUTH_REFRESH_COOKIE_NAME", "vastrabook_refresh")
AUTH_REFRESH_COOKIE_PATH = os.getenv("AUTH_REFRESH_COOKIE_PATH", "/api/v1/auth")
AUTH_REFRESH_COOKIE_SAMESITE = os.getenv("AUTH_REFRESH_COOKIE_SAMESITE", "Lax")
AUTH_REFRESH_COOKIE_SECURE = os.getenv("AUTH_REFRESH_COOKIE_SECURE", "False" if DEBUG else "True") == "True"

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
    "DEFAULT_THROTTLE_RATES": {
        "tenant_register": os.getenv("THROTTLE_TENANT_REGISTER", "5/hour"),
        "auth_otp": os.getenv("THROTTLE_AUTH_OTP", "6/minute"),
        "auth_verify": os.getenv("THROTTLE_AUTH_VERIFY", "12/minute"),
        "demo_session": os.getenv("THROTTLE_DEMO_SESSION", "20/minute"),
        "payment_webhook": os.getenv("THROTTLE_PAYMENT_WEBHOOK", "120/minute"),
        "messaging_webhook": os.getenv("THROTTLE_MESSAGING_WEBHOOK", "240/minute"),
        "public_share": os.getenv("THROTTLE_PUBLIC_SHARE", "120/minute"),
    },
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
E_WAY_BILL_PROVIDER = os.getenv("E_WAY_BILL_PROVIDER", "disabled").strip().lower()
E_WAY_BILL_API_URL = os.getenv("E_WAY_BILL_API_URL", "").strip()
E_WAY_BILL_API_TOKEN = os.getenv("E_WAY_BILL_API_TOKEN", "").strip()
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "local_stub" if DEBUG else "disabled").strip().lower()
SMS_PROVIDER_API_URL = os.getenv("SMS_PROVIDER_API_URL", "").strip()
SMS_PROVIDER_API_TOKEN = os.getenv("SMS_PROVIDER_API_TOKEN", "").strip()
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "local_stub" if DEBUG else "disabled").strip().lower()
RESEND_API_URL = os.getenv("RESEND_API_URL", "https://api.resend.com/emails").strip()
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev" if DEBUG else "").strip()
EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", "").strip()
PAYMENT_GATEWAY_PROVIDER = os.getenv("PAYMENT_GATEWAY_PROVIDER", "disabled").strip().lower()
RAZORPAY_API_URL = os.getenv("RAZORPAY_API_URL", "https://api.razorpay.com/v1").strip()
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
SHIPPING_PROVIDER = os.getenv("SHIPPING_PROVIDER", "disabled").strip().lower()
SHIPROCKET_API_URL = os.getenv("SHIPROCKET_API_URL", "https://apiv2.shiprocket.in/v1/external").strip()
SHIPROCKET_EMAIL = os.getenv("SHIPROCKET_EMAIL", "").strip()
SHIPROCKET_PASSWORD = os.getenv("SHIPROCKET_PASSWORD", "").strip()
SHIPROCKET_PICKUP_LOCATION = os.getenv("SHIPROCKET_PICKUP_LOCATION", "Primary" if DEBUG else "").strip()
SHIPROCKET_DEFAULT_LENGTH_CM = os.getenv("SHIPROCKET_DEFAULT_LENGTH_CM", "30").strip()
SHIPROCKET_DEFAULT_BREADTH_CM = os.getenv("SHIPROCKET_DEFAULT_BREADTH_CM", "24").strip()
SHIPROCKET_DEFAULT_HEIGHT_CM = os.getenv("SHIPROCKET_DEFAULT_HEIGHT_CM", "5").strip()
SHIPROCKET_DEFAULT_WEIGHT_KG = os.getenv("SHIPROCKET_DEFAULT_WEIGHT_KG", "0.5").strip()
WHATSAPP_PROVIDER = os.getenv("WHATSAPP_PROVIDER", "disabled").strip().lower()
GUPSHUP_API_URL = os.getenv("GUPSHUP_API_URL", "https://api.gupshup.io/wa/api/v1/msg").strip()
GUPSHUP_API_KEY = os.getenv("GUPSHUP_API_KEY", "").strip()
GUPSHUP_APP_NAME = os.getenv("GUPSHUP_APP_NAME", "").strip()
GUPSHUP_SOURCE_NUMBER = os.getenv("GUPSHUP_SOURCE_NUMBER", "").strip()
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
TWILIO_API_URL = os.getenv("TWILIO_API_URL", "https://api.twilio.com").strip()
MESSAGING_WEBHOOK_SECRET = os.getenv("MESSAGING_WEBHOOK_SECRET", "").strip()

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
