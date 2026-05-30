import json
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


LOCAL_EMAIL_PROVIDERS = {"local_stub", "demo", "stub"}


@dataclass(frozen=True)
class EmailDeliveryResult:
    delivered: bool
    provider: str
    message: str
    provider_response: dict = field(default_factory=dict)
    status_code: int | None = None

    def as_dict(self):
        payload = {
            "delivered": self.delivered,
            "provider": self.provider,
            "message": self.message,
        }
        if self.status_code is not None:
            payload["statusCode"] = self.status_code
        if self.provider_response:
            payload["providerResponse"] = self.provider_response
        return payload


def is_email_recipient(value):
    try:
        validate_email((value or "").strip())
    except ValidationError:
        return False
    return True


def email_provider_status():
    provider = (getattr(settings, "EMAIL_PROVIDER", "disabled") or "disabled").strip().lower()
    if provider in LOCAL_EMAIL_PROVIDERS:
        if getattr(settings, "DEBUG", False):
            return True, provider, ""
        return False, provider, "Local email stub is disabled outside DEBUG."
    if provider in {"disabled", ""}:
        return False, "disabled", "Email provider is not configured."
    if provider != "resend":
        return False, provider, f"Email provider '{provider}' is not supported yet."
    if not getattr(settings, "RESEND_API_KEY", ""):
        return False, provider, "RESEND_API_KEY is required for Resend email delivery."
    if not getattr(settings, "RESEND_FROM_EMAIL", ""):
        return False, provider, "RESEND_FROM_EMAIL is required for Resend email delivery."
    return True, provider, ""


def _decode_provider_body(raw_body):
    if not raw_body:
        return {}
    try:
        return json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw": raw_body.decode("utf-8", errors="replace")[:500]}


def _provider_error_message(provider, status_code, payload):
    details = payload.get("message") or payload.get("detail") or payload.get("title") or ""
    suffix = f": {details}" if details else ""
    return f"{provider} returned HTTP {status_code}{suffix}"


def send_email(*, to, subject, html, text=""):
    recipient = (to or "").strip()
    if not is_email_recipient(recipient):
        return EmailDeliveryResult(
            delivered=False,
            provider="skipped",
            message="Recipient is not an email address; share link was prepared only.",
        )

    ready, provider, message = email_provider_status()
    if not ready:
        return EmailDeliveryResult(delivered=False, provider=provider, message=message)

    if provider in LOCAL_EMAIL_PROVIDERS:
        print("--- LOCAL EMAIL PROVIDER ---")
        print(f"To: {recipient}")
        print(f"Subject: {subject}")
        print(text or html)
        print("----------------------------")
        return EmailDeliveryResult(
            delivered=True,
            provider=provider,
            message="Email accepted by local debug email provider.",
        )

    payload = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": [recipient],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    reply_to = getattr(settings, "EMAIL_REPLY_TO", "")
    if reply_to:
        payload["reply_to"] = reply_to

    request = Request(
        getattr(settings, "RESEND_API_URL", "https://api.resend.com/emails"),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.RESEND_API_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "VastraBook/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=15) as response:
            body = _decode_provider_body(response.read())
            if 200 <= response.status < 300:
                return EmailDeliveryResult(
                    delivered=True,
                    provider=provider,
                    message="Email accepted by Resend.",
                    provider_response=body,
                    status_code=response.status,
                )
            return EmailDeliveryResult(
                delivered=False,
                provider=provider,
                message=_provider_error_message(provider, response.status, body),
                provider_response=body,
                status_code=response.status,
            )
    except HTTPError as exc:
        body = _decode_provider_body(exc.read())
        return EmailDeliveryResult(
            delivered=False,
            provider=provider,
            message=_provider_error_message(provider, exc.code, body),
            provider_response=body,
            status_code=exc.code,
        )
    except URLError:
        return EmailDeliveryResult(
            delivered=False,
            provider=provider,
            message="Email provider could not be reached.",
        )
