import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.activity import write_activity
from apps.accounts.email_delivery import send_email


LOCAL_SMS_PROVIDERS = {"local_stub", "demo", "stub"}
LOCAL_WHATSAPP_PROVIDERS = {"local_stub", "demo", "stub"}
DELIVERED_STATUSES = {"delivered", "read", "success", "succeeded"}
SENT_STATUSES = {"sent", "submitted", "queued", "accepted", "dispatched", "enroute", "in_progress"}
FAILED_STATUSES = {"failed", "undelivered", "rejected", "blocked", "error", "errored", "expired", "cancelled"}


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


def _nested_value(payload, paths):
    if not isinstance(payload, dict):
        return ""
    for path in paths:
        value = payload
        for key in path:
            if not isinstance(value, dict) or key not in value:
                value = None
                break
            value = value[key]
        if value not in (None, ""):
            return str(value)
    return ""


def _parse_json_value(value):
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _plain_payload(payload):
    if hasattr(payload, "lists"):
        normalized = {
            key: values[-1] if len(values) == 1 else values
            for key, values in payload.lists()
        }
    elif isinstance(payload, dict):
        normalized = dict(payload)
    else:
        normalized = {}

    for key in list(normalized.keys()):
        normalized[key] = _parse_json_value(normalized[key])
    return normalized


def _payload_from_raw_body(raw_body):
    if not raw_body:
        return {}
    try:
        return json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = parse_qs(raw_body.decode("utf-8", errors="replace"))
        return {
            key: values[-1] if len(values) == 1 else values
            for key, values in parsed.items()
        }


def _metadata(payload):
    metadata = None
    for path in (("metadata",), ("meta",), ("data", "metadata"), ("payload", "metadata")):
        current = payload
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current:
            metadata = current
            break
    if isinstance(metadata, str):
        metadata = _parse_json_value(metadata)
    return metadata if isinstance(metadata, dict) else {}


def _provider_message_id(payload):
    direct = next((
        payload.get(key)
        for key in ("messageId", "message_id", "sid", "MessageSid", "SmsSid", "messageSid")
        if isinstance(payload, dict) and payload.get(key)
    ), "")
    if direct:
        return str(direct)
    return _nested_value(payload, [
        ("MessageSid",),
        ("SmsSid",),
        ("messageSid",),
        ("message_id",),
        ("message", "id"),
        ("message", "sid"),
        ("message", "messageId"),
        ("payload", "id"),
        ("payload", "sid"),
        ("payload", "messageId"),
        ("payload", "message", "id"),
        ("payload", "message", "sid"),
        ("data", "id"),
        ("data", "sid"),
        ("data", "messageId"),
    ])


def _provider_status(payload):
    return _nested_value(payload, [
        ("status",),
        ("Status",),
        ("SmsStatus",),
        ("MessageStatus",),
        ("messageStatus",),
        ("eventType",),
        ("event",),
        ("type",),
        ("payload", "status"),
        ("payload", "type"),
        ("payload", "message", "status"),
        ("message", "status"),
        ("data", "status"),
    ])


def _event_id(provider, payload, headers, raw_body, provider_message_id, provider_status):
    header_event_id = (
        headers.get("X-Gupshup-Event-Id")
        or headers.get("X-Twilio-Request-Id")
        or headers.get("X-Message-Event-Id")
        or headers.get("X-Webhook-Event-Id")
    )
    explicit_event_id = _nested_value(payload, [
        ("eventId",),
        ("event_id",),
        ("requestId",),
        ("id",),
        ("payload", "eventId"),
        ("payload", "id"),
        ("data", "eventId"),
        ("data", "id"),
    ])
    if header_event_id or explicit_event_id:
        return str(header_event_id or explicit_event_id)
    digest = hashlib.sha256(
        b"|".join([
            provider.encode("utf-8"),
            provider_message_id.encode("utf-8"),
            provider_status.encode("utf-8"),
            raw_body or json.dumps(payload, sort_keys=True).encode("utf-8"),
        ])
    ).hexdigest()
    return f"derived-{digest[:48]}"


def normalize_delivery_status(provider_status):
    status = (provider_status or "").strip().lower().replace("-", "_").replace(" ", "_")
    if status in DELIVERED_STATUSES:
        return "delivered"
    if status in SENT_STATUSES:
        return "sent"
    if status in FAILED_STATUSES:
        return "failed"
    return ""


def verify_messaging_webhook(raw_body, headers, query_token=""):
    secret = getattr(settings, "MESSAGING_WEBHOOK_SECRET", "")
    if not secret:
        return False, "Messaging webhook secret is not configured."

    supplied_secret = (
        headers.get("X-VastraBook-Webhook-Secret")
        or headers.get("X-Webhook-Secret")
        or query_token
        or ""
    )
    authorization = headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied_secret = supplied_secret or authorization.split(" ", 1)[1]
    if supplied_secret and hmac.compare_digest(supplied_secret, secret):
        return True, ""

    signature = headers.get("X-VastraBook-Signature") or headers.get("X-Messaging-Signature") or ""
    expected = hmac.new(secret.encode("utf-8"), raw_body or b"", hashlib.sha256).hexdigest()
    accepted_signatures = {expected, f"sha256={expected}"}
    if signature and any(hmac.compare_digest(signature, candidate) for candidate in accepted_signatures):
        return True, ""

    return False, "Invalid messaging webhook signature."


def _find_target(provider, provider_message_id, metadata):
    from apps.business_settings.models import Reminder
    from apps.business_tools.models import SMSRecipient

    recipient_id = metadata.get("recipientId") or metadata.get("recipient_id")
    if recipient_id:
        recipient = SMSRecipient.objects.filter(id=recipient_id).select_related("business", "campaign").first()
        if recipient:
            return "sms_recipient", recipient

    reminder_id = metadata.get("reminderId") or metadata.get("reminder_id")
    if reminder_id:
        reminder = Reminder.objects.filter(id=reminder_id).select_related("business", "party").first()
        if reminder:
            return "reminder", reminder

    if not provider_message_id:
        return "unknown", None

    recipient_matches = list(
        SMSRecipient.objects.filter(
            provider=provider,
            provider_message_id=provider_message_id,
        ).select_related("business", "campaign")[:2]
    )
    if len(recipient_matches) == 1:
        return "sms_recipient", recipient_matches[0]

    reminder_matches = list(
        Reminder.objects.filter(
            delivery_provider=provider,
            provider_message_id=provider_message_id,
        ).select_related("business", "party")[:2]
    )
    if len(reminder_matches) == 1:
        return "reminder", reminder_matches[0]
    return "unknown", None


def _append_webhook_payload(existing_payload, payload, provider_status, normalized_status):
    value = existing_payload if isinstance(existing_payload, dict) else {}
    return {
        **value,
        "lastWebhook": {
            "providerStatus": provider_status,
            "normalizedStatus": normalized_status,
            "receivedAt": timezone.now().isoformat(),
            "payload": payload,
        },
    }


def _update_campaign_counters(campaign):
    campaign.delivered_count = campaign.recipients.filter(status__in=["sent", "delivered"]).count()
    campaign.failed_count = campaign.recipients.filter(status="failed").count()
    campaign.save(update_fields=["delivered_count", "failed_count", "updated_at"])


def _apply_delivery_update(target_type, target, payload, provider_status, normalized_status):
    if target_type == "sms_recipient":
        from apps.business_tools.models import SMSRecipient

        recipient = SMSRecipient.objects.select_for_update().select_related("campaign", "business").get(id=target.id)
        now = timezone.now()
        recipient.provider_response = _append_webhook_payload(
            recipient.provider_response,
            payload,
            provider_status,
            normalized_status,
        )
        if normalized_status == "delivered":
            recipient.status = "delivered"
            recipient.delivered_at = recipient.delivered_at or now
            recipient.error_message = ""
        elif normalized_status == "sent":
            recipient.status = "sent"
            recipient.sent_at = recipient.sent_at or now
            recipient.error_message = ""
        elif normalized_status == "failed":
            recipient.status = "failed"
            recipient.error_message = _nested_value(payload, [
                ("error",),
                ("errorMessage",),
                ("message", "error"),
                ("payload", "error"),
                ("data", "error"),
            ]) or f"Provider reported {provider_status or 'failed'}."
        recipient.save(update_fields=[
            "status", "sent_at", "delivered_at", "error_message",
            "provider_response",
        ])
        _update_campaign_counters(recipient.campaign)
        write_activity(
            business=recipient.business,
            action="sms_delivery_receipt",
            entity_type="sms_recipient",
            entity_id=recipient.id,
            details={
                "campaignNumber": recipient.campaign.campaign_number,
                "provider": recipient.provider,
                "providerMessageId": recipient.provider_message_id,
                "providerStatus": provider_status,
                "status": recipient.status,
            },
        )
        return recipient.business, recipient.id

    if target_type == "reminder":
        from apps.business_settings.models import Reminder

        reminder = Reminder.objects.select_for_update().select_related("business").get(id=target.id)
        reminder.provider_response = _append_webhook_payload(
            reminder.provider_response,
            payload,
            provider_status,
            normalized_status,
        )
        if normalized_status == "failed":
            reminder.status = "failed"
            reminder.delivery_message = f"Provider reported {provider_status or 'failed'}."
        elif normalized_status in {"sent", "delivered"}:
            reminder.status = "sent"
            reminder.sent_at = reminder.sent_at or timezone.now()
            reminder.delivery_message = f"Provider reported {provider_status or normalized_status}."
        reminder.save(update_fields=[
            "status", "sent_at", "provider_response", "delivery_message",
        ])
        write_activity(
            business=reminder.business,
            action="reminder_delivery_receipt",
            entity_type="reminder",
            entity_id=reminder.id,
            details={
                "provider": reminder.delivery_provider,
                "providerMessageId": reminder.provider_message_id,
                "providerStatus": provider_status,
                "status": reminder.status,
            },
        )
        return reminder.business, reminder.id

    return None, None


def process_messaging_delivery_webhook(*, provider, payload, raw_body, headers):
    from apps.business_tools.models import MessagingDeliveryEvent

    provider = (provider or "").strip().lower()
    payload = _plain_payload(payload) or _payload_from_raw_body(raw_body)
    if not payload:
        payload = _payload_from_raw_body(raw_body)
    provider_message_id = _provider_message_id(payload)
    provider_status = _provider_status(payload)
    normalized_status = normalize_delivery_status(provider_status)
    metadata = _metadata(payload)
    event_id = _event_id(provider, payload, headers, raw_body, provider_message_id, provider_status)
    channel = "whatsapp" if provider in {"gupshup", "twilio"} else "sms"

    target_type, target = _find_target(provider, provider_message_id, metadata)
    business = getattr(target, "business", None)
    event, created = MessagingDeliveryEvent.objects.get_or_create(
        provider=provider,
        event_id=event_id,
        defaults={
            "business": business,
            "channel": channel,
            "provider_message_id": provider_message_id,
            "provider_status": provider_status,
            "normalized_status": normalized_status,
            "target_type": target_type,
            "target_id": getattr(target, "id", None),
            "status": "processed",
            "payload": payload,
            "processed_at": timezone.now(),
        },
    )
    if not created:
        return event, False

    if not normalized_status:
        event.status = "ignored"
        event.message = "Delivery status was not recognized."
        event.save(update_fields=["status", "message"])
        return event, True

    if not target:
        event.status = "failed"
        event.message = "No matching SMS recipient or reminder was found for this provider message id."
        event.save(update_fields=["status", "message"])
        return event, True

    with transaction.atomic():
        business, target_id = _apply_delivery_update(target_type, target, payload, provider_status, normalized_status)
        event.business = business
        event.target_id = target_id
        event.target_type = target_type
        event.processed_at = timezone.now()
        event.save(update_fields=["business", "target_id", "target_type", "processed_at"])
    return event, True


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
