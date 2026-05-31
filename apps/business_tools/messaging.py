import base64
import json
import re
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings

from apps.accounts.email_delivery import send_email


LOCAL_SMS_PROVIDERS = {"local_stub", "demo", "stub"}
LOCAL_WHATSAPP_PROVIDERS = {"local_stub", "demo", "stub"}


@dataclass(frozen=True)
class MessageDeliveryResult:
    delivered: bool
    provider: str
    message: str
    provider_message_id: str = ""
    provider_response: dict = field(default_factory=dict)
    status_code: int | None = None


def _decode_provider_body(raw_body):
    if not raw_body:
        return {}
    try:
        return json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw": raw_body.decode("utf-8", errors="replace")[:1000]}


def _provider_error_message(provider, status_code, payload):
    message = payload.get("message") or payload.get("detail") or payload.get("error") or payload.get("raw") or ""
    suffix = f": {message}" if message else ""
    return f"{provider} returned HTTP {status_code}{suffix}"


def _message_id(payload):
    if not isinstance(payload, dict):
        return ""
    candidates = [
        payload.get("messageId"),
        payload.get("message_id"),
        payload.get("sid"),
        payload.get("id"),
        payload.get("data", {}).get("messageId") if isinstance(payload.get("data"), dict) else None,
        payload.get("data", {}).get("sid") if isinstance(payload.get("data"), dict) else None,
    ]
    return str(next((value for value in candidates if value), ""))


def _http_json_request(provider, url, payload, headers):
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "VastraBook/1.0",
            **headers,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = _decode_provider_body(response.read())
            if 200 <= response.status < 300:
                return MessageDeliveryResult(
                    delivered=True,
                    provider=provider,
                    message=f"{provider} accepted message.",
                    provider_message_id=_message_id(body),
                    provider_response=body,
                    status_code=response.status,
                )
            return MessageDeliveryResult(
                delivered=False,
                provider=provider,
                message=_provider_error_message(provider, response.status, body),
                provider_response=body,
                status_code=response.status,
            )
    except HTTPError as exc:
        body = _decode_provider_body(exc.read())
        return MessageDeliveryResult(
            delivered=False,
            provider=provider,
            message=_provider_error_message(provider, exc.code, body),
            provider_response=body,
            status_code=exc.code,
        )
    except URLError:
        return MessageDeliveryResult(
            delivered=False,
            provider=provider,
            message=f"{provider} could not be reached.",
        )


def _http_form_request(provider, url, payload, headers):
    request = Request(
        url,
        data=urlencode(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "VastraBook/1.0",
            **headers,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = _decode_provider_body(response.read())
            if 200 <= response.status < 300:
                return MessageDeliveryResult(
                    delivered=True,
                    provider=provider,
                    message=f"{provider} accepted message.",
                    provider_message_id=_message_id(body),
                    provider_response=body,
                    status_code=response.status,
                )
            return MessageDeliveryResult(
                delivered=False,
                provider=provider,
                message=_provider_error_message(provider, response.status, body),
                provider_response=body,
                status_code=response.status,
            )
    except HTTPError as exc:
        body = _decode_provider_body(exc.read())
        return MessageDeliveryResult(
            delivered=False,
            provider=provider,
            message=_provider_error_message(provider, exc.code, body),
            provider_response=body,
            status_code=exc.code,
        )
    except URLError:
        return MessageDeliveryResult(
            delivered=False,
            provider=provider,
            message=f"{provider} could not be reached.",
        )


def _normalize_india_mobile(value):
    digits = re.sub(r"\D+", "", value or "")
    if len(digits) == 10:
        return f"91{digits}"
    return digits


def sms_provider_ready():
    provider = (getattr(settings, "SMS_PROVIDER", "disabled") or "disabled").strip().lower()
    if provider in LOCAL_SMS_PROVIDERS:
        if getattr(settings, "DEBUG", False):
            return True, provider, ""
        return False, provider, "Local SMS stub is disabled outside DEBUG."
    if provider in {"disabled", ""}:
        return False, "disabled", "SMS provider is not configured."
    if not getattr(settings, "SMS_PROVIDER_API_URL", "") or not getattr(settings, "SMS_PROVIDER_API_TOKEN", ""):
        return False, provider, "SMS provider API URL and token are required for production delivery."
    return True, provider, ""


def whatsapp_provider_ready():
    provider = (getattr(settings, "WHATSAPP_PROVIDER", "disabled") or "disabled").strip().lower()
    if provider in LOCAL_WHATSAPP_PROVIDERS:
        if getattr(settings, "DEBUG", False):
            return True, provider, ""
        return False, provider, "Local WhatsApp stub is disabled outside DEBUG."
    if provider in {"disabled", ""}:
        return False, "disabled", "WhatsApp provider is not configured."
    if provider == "gupshup":
        missing = [
            name
            for name in ("GUPSHUP_API_URL", "GUPSHUP_API_KEY", "GUPSHUP_APP_NAME", "GUPSHUP_SOURCE_NUMBER")
            if not getattr(settings, name, "")
        ]
        if missing:
            return False, provider, f"{', '.join(missing)} are required for Gupshup WhatsApp delivery."
        return True, provider, ""
    if provider == "twilio":
        missing = [
            name
            for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM")
            if not getattr(settings, name, "")
        ]
        if missing:
            return False, provider, f"{', '.join(missing)} are required for Twilio WhatsApp delivery."
        return True, provider, ""
    return False, provider, f"WhatsApp provider '{provider}' is not supported yet."


def send_sms_message(*, to, message, metadata=None):
    ready, provider, status_message = sms_provider_ready()
    if not ready:
        return MessageDeliveryResult(False, provider, status_message)

    recipient = _normalize_india_mobile(to)
    if len(recipient) < 10:
        return MessageDeliveryResult(False, provider, "A valid mobile number is required.")

    if provider in LOCAL_SMS_PROVIDERS:
        print("--- LOCAL SMS PROVIDER ---")
        print(f"To: {recipient}")
        print(f"Message: {message}")
        print("--------------------------")
        return MessageDeliveryResult(True, provider, f"{provider} accepted SMS message.")

    payload = {
        "to": recipient,
        "message": message,
        "metadata": metadata or {},
    }
    return _http_json_request(
        provider,
        settings.SMS_PROVIDER_API_URL,
        payload,
        {"Authorization": f"Bearer {settings.SMS_PROVIDER_API_TOKEN}"},
    )


def send_gupshup_whatsapp(*, to, message, metadata=None):
    destination = _normalize_india_mobile(to)
    if len(destination) < 10:
        return MessageDeliveryResult(False, "gupshup", "A valid WhatsApp destination number is required.")

    payload = {
        "channel": "whatsapp",
        "source": settings.GUPSHUP_SOURCE_NUMBER,
        "destination": destination,
        "src.name": settings.GUPSHUP_APP_NAME,
        "message": json.dumps({"type": "text", "text": message}),
    }
    if metadata:
        payload["metadata"] = json.dumps(metadata)
    return _http_form_request(
        "gupshup",
        settings.GUPSHUP_API_URL,
        payload,
        {"apikey": settings.GUPSHUP_API_KEY},
    )


def send_twilio_whatsapp(*, to, message):
    destination = _normalize_india_mobile(to)
    if len(destination) < 10:
        return MessageDeliveryResult(False, "twilio", "A valid WhatsApp destination number is required.")

    account_sid = settings.TWILIO_ACCOUNT_SID
    token = settings.TWILIO_AUTH_TOKEN
    credentials = base64.b64encode(f"{account_sid}:{token}".encode("utf-8")).decode("ascii")
    from_number = settings.TWILIO_WHATSAPP_FROM
    if not from_number.startswith("whatsapp:"):
        from_number = f"whatsapp:{from_number}"
    payload = {
        "From": from_number,
        "To": f"whatsapp:+{destination}",
        "Body": message,
    }
    return _http_form_request(
        "twilio",
        f"{settings.TWILIO_API_URL.rstrip('/')}/2010-04-01/Accounts/{account_sid}/Messages.json",
        payload,
        {"Authorization": f"Basic {credentials}"},
    )


def send_whatsapp_message(*, to, message, metadata=None):
    ready, provider, status_message = whatsapp_provider_ready()
    if not ready:
        return MessageDeliveryResult(False, provider, status_message)
    if provider in LOCAL_WHATSAPP_PROVIDERS:
        print("--- LOCAL WHATSAPP PROVIDER ---")
        print(f"To: {to}")
        print(f"Message: {message}")
        print("-------------------------------")
        return MessageDeliveryResult(True, provider, f"{provider} accepted WhatsApp message.")
    if provider == "gupshup":
        return send_gupshup_whatsapp(to=to, message=message, metadata=metadata)
    return send_twilio_whatsapp(to=to, message=message)


def send_reminder_message(reminder):
    party = reminder.party
    recipient_mobile = party.mobile if party else ""
    metadata = {
        "reminderId": str(reminder.id),
        "businessId": str(reminder.business_id),
        "voucherType": reminder.voucher_type or "",
        "voucherId": str(reminder.voucher_id) if reminder.voucher_id else "",
    }
    if reminder.channel == "whatsapp":
        return send_whatsapp_message(to=recipient_mobile, message=reminder.message, metadata=metadata)
    if reminder.channel == "email":
        recipient = party.email if party else ""
        delivery = send_email(
            to=recipient,
            subject=f"{reminder.business.name} reminder",
            html=f"<p>{reminder.message}</p>",
            text=reminder.message,
        )
        return MessageDeliveryResult(
            delivered=delivery.delivered,
            provider=delivery.provider,
            message=delivery.message,
            provider_message_id=str(delivery.provider_response.get("id") or ""),
            provider_response=delivery.provider_response,
            status_code=delivery.status_code,
        )
    return send_sms_message(to=recipient_mobile, message=reminder.message, metadata=metadata)
