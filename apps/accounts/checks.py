from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

from .email_delivery import LOCAL_EMAIL_PROVIDERS
from .otp import LOCAL_SMS_PROVIDERS


@register(Tags.security, deploy=True)
def production_safety_checks(app_configs, **kwargs):
    issues = []

    if settings.DEBUG:
        return issues

    if not settings.SECRET_KEY or settings.SECRET_KEY.startswith("django-insecure"):
        issues.append(Error(
            "Set a strong SECRET_KEY before production deployment.",
            id="accounts.E001",
        ))

    if "*" in settings.ALLOWED_HOSTS:
        issues.append(Error(
            "ALLOWED_HOSTS cannot contain '*' when DEBUG=False.",
            id="accounts.E002",
        ))

    if getattr(settings, "CORS_ALLOW_ALL_ORIGINS", False):
        issues.append(Error(
            "CORS_ALLOW_ALL_ORIGINS must be disabled in production.",
            id="accounts.E003",
        ))

    if getattr(settings, "DEMO_SESSION_ENABLED", False):
        issues.append(Error(
            "DEMO_SESSION_ENABLED must be false in production.",
            id="accounts.E004",
        ))

    sms_provider = (getattr(settings, "SMS_PROVIDER", "disabled") or "disabled").strip().lower()
    if sms_provider in {"disabled", ""} or sms_provider in LOCAL_SMS_PROVIDERS:
        issues.append(Error(
            "Configure a real SMS provider for production OTP login.",
            id="accounts.E005",
        ))
    elif not getattr(settings, "SMS_PROVIDER_API_URL", "") or not getattr(settings, "SMS_PROVIDER_API_TOKEN", ""):
        issues.append(Error(
            "SMS_PROVIDER_API_URL and SMS_PROVIDER_API_TOKEN are required for production OTP login.",
            id="accounts.E006",
        ))

    einvoice_provider = (getattr(settings, "E_INVOICE_PROVIDER", "disabled") or "disabled").strip().lower()
    if einvoice_provider in {"disabled", ""} or einvoice_provider in {"local_stub", "demo", "stub"}:
        issues.append(Warning(
            "E-invoicing is not connected to a production provider.",
            id="accounts.W001",
        ))
    elif not getattr(settings, "E_INVOICE_API_URL", "") or not getattr(settings, "E_INVOICE_API_TOKEN", ""):
        issues.append(Error(
            "E_INVOICE_API_URL and E_INVOICE_API_TOKEN are required for production e-invoicing.",
            id="accounts.E007",
        ))

    email_provider = (getattr(settings, "EMAIL_PROVIDER", "disabled") or "disabled").strip().lower()
    if email_provider in {"disabled", ""} or email_provider in LOCAL_EMAIL_PROVIDERS:
        issues.append(Warning(
            "Email delivery is not connected to a production provider.",
            id="accounts.W002",
        ))
    elif email_provider == "resend":
        if not getattr(settings, "RESEND_API_KEY", "") or not getattr(settings, "RESEND_FROM_EMAIL", ""):
            issues.append(Error(
                "RESEND_API_KEY and RESEND_FROM_EMAIL are required for Resend email delivery.",
                id="accounts.E009",
            ))
    else:
        issues.append(Error(
            f"EMAIL_PROVIDER '{email_provider}' is not supported.",
            id="accounts.E010",
        ))

    engine = settings.DATABASES.get("default", {}).get("ENGINE", "")
    if engine.endswith("sqlite3"):
        issues.append(Error(
            "Production must use Postgres, not SQLite.",
            id="accounts.E008",
        ))

    return issues
