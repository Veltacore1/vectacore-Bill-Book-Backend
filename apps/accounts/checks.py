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

    eway_provider = (getattr(settings, "E_WAY_BILL_PROVIDER", "disabled") or "disabled").strip().lower()
    if eway_provider in {"disabled", ""}:
        issues.append(Warning(
            "E-way bill generation is not connected to a production provider.",
            id="accounts.W006",
        ))
    elif not getattr(settings, "E_WAY_BILL_API_URL", "") or not getattr(settings, "E_WAY_BILL_API_TOKEN", ""):
        issues.append(Error(
            "E_WAY_BILL_API_URL and E_WAY_BILL_API_TOKEN are required for production e-way bill generation.",
            id="accounts.E019",
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

    payment_gateway_provider = (getattr(settings, "PAYMENT_GATEWAY_PROVIDER", "disabled") or "disabled").strip().lower()
    if payment_gateway_provider in {"disabled", ""}:
        issues.append(Warning(
            "Payment gateway is not connected; online payment links and subscription collection will stay disabled.",
            id="accounts.W003",
        ))
    elif payment_gateway_provider == "razorpay":
        missing = [
            name
            for name in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET")
            if not getattr(settings, name, "")
        ]
        if missing:
            issues.append(Error(
                f"{', '.join(missing)} are required for Razorpay payment gateway integration.",
                id="accounts.E011",
            ))
    else:
        issues.append(Error(
            f"PAYMENT_GATEWAY_PROVIDER '{payment_gateway_provider}' is not supported.",
            id="accounts.E012",
        ))

    shipping_provider = (getattr(settings, "SHIPPING_PROVIDER", "disabled") or "disabled").strip().lower()
    if shipping_provider in {"disabled", ""}:
        issues.append(Warning(
            "Shipping provider is not connected; online order dispatch and courier tracking will stay disabled.",
            id="accounts.W004",
        ))
    elif shipping_provider in {"local_stub", "demo", "stub"}:
        pass
    elif shipping_provider == "shiprocket":
        missing = [
            name
            for name in ("SHIPROCKET_API_URL", "SHIPROCKET_EMAIL", "SHIPROCKET_PASSWORD", "SHIPROCKET_PICKUP_LOCATION")
            if not getattr(settings, name, "")
        ]
        if missing:
            issues.append(Error(
                f"{', '.join(missing)} are required for Shiprocket shipping integration.",
                id="accounts.E013",
            ))
    else:
        issues.append(Error(
            f"SHIPPING_PROVIDER '{shipping_provider}' is not supported.",
            id="accounts.E014",
        ))

    whatsapp_provider = (getattr(settings, "WHATSAPP_PROVIDER", "disabled") or "disabled").strip().lower()
    if whatsapp_provider in {"disabled", ""}:
        issues.append(Warning(
            "WhatsApp provider is not connected; WhatsApp reminders and order notifications will stay disabled.",
            id="accounts.W005",
        ))
    elif whatsapp_provider == "gupshup":
        missing = [
            name
            for name in ("GUPSHUP_API_URL", "GUPSHUP_API_KEY", "GUPSHUP_APP_NAME", "GUPSHUP_SOURCE_NUMBER")
            if not getattr(settings, name, "")
        ]
        if missing:
            issues.append(Error(
                f"{', '.join(missing)} are required for Gupshup WhatsApp integration.",
                id="accounts.E015",
            ))
    elif whatsapp_provider == "twilio":
        missing = [
            name
            for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")
            if not getattr(settings, name, "")
        ]
        if missing:
            issues.append(Error(
                f"{', '.join(missing)} are required for Twilio WhatsApp integration.",
                id="accounts.E016",
            ))
    else:
        issues.append(Error(
            f"WHATSAPP_PROVIDER '{whatsapp_provider}' is not supported.",
            id="accounts.E017",
        ))

    messaging_callbacks_enabled = sms_provider not in {"disabled", ""} and sms_provider not in LOCAL_SMS_PROVIDERS
    messaging_callbacks_enabled = messaging_callbacks_enabled or whatsapp_provider not in {"disabled", ""}
    if messaging_callbacks_enabled and not getattr(settings, "MESSAGING_WEBHOOK_SECRET", ""):
        issues.append(Error(
            "MESSAGING_WEBHOOK_SECRET is required for SMS/WhatsApp delivery receipt callbacks.",
            id="accounts.E018",
        ))

    engine = settings.DATABASES.get("default", {}).get("ENGINE", "")
    if engine.endswith("sqlite3"):
        issues.append(Error(
            "Production must use Postgres, not SQLite.",
            id="accounts.E008",
        ))

    return issues
