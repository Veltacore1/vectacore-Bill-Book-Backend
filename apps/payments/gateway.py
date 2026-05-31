import base64
import hashlib
import hmac
import json
from decimal import Decimal, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.accounts.activity import write_activity
from apps.parties.models import Party
from apps.sales.models import SalesInvoice

from .models import PaymentGatewayEvent, PaymentGatewayOrder, PaymentIn
from .serializers import _apply_payment_in_settlements, _next_payment_number


class PaymentGatewayConfigurationError(Exception):
    pass


class PaymentGatewayDeliveryError(Exception):
    pass


def razorpay_ready():
    provider = (getattr(settings, "PAYMENT_GATEWAY_PROVIDER", "disabled") or "disabled").strip().lower()
    if provider != "razorpay":
        return False, provider, "Razorpay payment gateway is not configured."
    missing = [
        name
        for name in ("RAZORPAY_API_URL", "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET")
        if not getattr(settings, name, "")
    ]
    if missing:
        return False, provider, f"Missing payment gateway settings: {', '.join(missing)}."
    return True, provider, ""


def amount_to_subunits(amount):
    value = Decimal(str(amount or "0"))
    if value <= 0:
        raise serializers.ValidationError({"amount": "Payment amount must be greater than zero."})
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def verify_checkout_signature(*, order_id, payment_id, signature):
    message = f"{order_id}|{payment_id}".encode("utf-8")
    expected = hmac.new(settings.RAZORPAY_KEY_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def verify_webhook_signature(raw_body, signature):
    expected = hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _provider_error_message(error):
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    description = (
        payload.get("error", {}).get("description")
        or payload.get("message")
        or payload.get("detail")
        or ""
    )
    return f"Razorpay returned HTTP {error.code}{f': {description}' if description else ''}", payload


def create_razorpay_order(*, amount_subunits, currency, receipt, notes):
    ready, _provider, message = razorpay_ready()
    if not ready:
        raise PaymentGatewayConfigurationError(message)

    auth = base64.b64encode(f"{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}".encode("utf-8")).decode("ascii")
    request = Request(
        f"{settings.RAZORPAY_API_URL.rstrip('/')}/orders",
        data=json.dumps({
            "amount": amount_subunits,
            "currency": currency,
            "receipt": receipt,
            "notes": notes,
        }).encode("utf-8"),
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "VastraBook/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        message, payload = _provider_error_message(error)
        raise PaymentGatewayDeliveryError(message) from error
    except (URLError, TimeoutError) as error:
        raise PaymentGatewayDeliveryError("Razorpay order API could not be reached.") from error
    except json.JSONDecodeError as error:
        raise PaymentGatewayDeliveryError("Razorpay order API returned invalid JSON.") from error


def _receipt_for(business, invoice=None):
    prefix = (business.invoice_prefix or "INV").upper()[:8]
    timestamp = int(timezone.now().timestamp() * 1000)
    invoice_part = str(invoice.id).replace("-", "")[-8:] if invoice else "ADVANCE"
    return f"{prefix}-RZP-{invoice_part}-{timestamp}"[:40]


def create_gateway_order(*, request, party, amount, invoice=None, notes=None):
    if not request.business:
        raise serializers.ValidationError("No active tenant business.")
    if party.business_id != request.business.id or party.party_type != "customer":
        raise serializers.ValidationError("Select a customer from the active tenant.")
    if invoice:
        if invoice.business_id != request.business.id or invoice.party_id != party.id:
            raise serializers.ValidationError("Invoice is not available for this customer.")
        if invoice.status == "cancelled":
            raise serializers.ValidationError("Cancelled invoices cannot receive gateway payments.")

    amount_value = Decimal(str(amount))
    amount_subunits = amount_to_subunits(amount_value)
    receipt = _receipt_for(request.business, invoice)
    provider_notes = {
        "business_id": str(request.business.id),
        "party_id": str(party.id),
        "invoice_id": str(invoice.id) if invoice else "",
        "source": "vastrabook",
        **(notes or {}),
    }
    provider_response = create_razorpay_order(
        amount_subunits=amount_subunits,
        currency="INR",
        receipt=receipt,
        notes=provider_notes,
    )
    provider_order_id = provider_response.get("id")
    if not provider_order_id:
        raise PaymentGatewayDeliveryError("Razorpay order response did not include an order id.")

    return PaymentGatewayOrder.objects.create(
        business=request.business,
        party=party,
        invoice=invoice,
        provider="razorpay",
        provider_order_id=provider_order_id,
        provider_status=provider_response.get("status", ""),
        receipt=receipt,
        amount=amount_value,
        amount_subunits=amount_subunits,
        currency=provider_response.get("currency") or "INR",
        status=provider_response.get("status") or "created",
        provider_payload=provider_response,
        notes=provider_notes,
        created_by=request.user,
    )


def mark_gateway_order_paid(*, order, payment_id, signature_verified, payload=None, user=None):
    with transaction.atomic():
        PaymentGatewayOrder.objects.select_for_update().get(id=order.id)
        order = PaymentGatewayOrder.objects.select_related("business", "party", "invoice").get(id=order.id)
        if order.payment_in_id:
            return order.payment_in, False

        payment = PaymentIn.objects.create(
            business=order.business,
            payment_number=_next_payment_number(PaymentIn, order.business, "PMTIN"),
            party=order.party,
            amount_received=order.amount,
            payment_mode="upi",
            reference_number=payment_id,
            notes=f"Razorpay payment for order {order.provider_order_id}",
            created_by=user or order.created_by,
        )
        allocations = None
        if order.invoice_id:
            unpaid = max(Decimal("0.00"), order.invoice.total_amount - order.invoice.paid_amount)
            if unpaid > 0:
                allocations = [{"invoice": str(order.invoice_id), "settled_amount": str(min(order.amount, unpaid))}]
        _apply_payment_in_settlements(payment, allocations)

        order.payment_in = payment
        order.provider_payment_id = payment_id
        order.signature_verified = signature_verified
        order.status = "paid"
        order.provider_status = "paid"
        order.paid_at = timezone.now()
        if payload is not None:
            order.provider_payload = payload
        order.save(update_fields=[
            "payment_in", "provider_payment_id", "signature_verified", "status",
            "provider_status", "paid_at", "provider_payload", "updated_at",
        ])
        write_activity(
            business=order.business,
            user=user or order.created_by,
            action="gateway_payment_captured",
            entity_type="payment_gateway_order",
            entity_id=order.id,
            details={
                "provider": order.provider,
                "providerOrderId": order.provider_order_id,
                "providerPaymentId": payment_id,
                "paymentNumber": payment.payment_number,
                "amount": float(order.amount),
            },
        )
        return payment, True


def process_razorpay_webhook(*, raw_body, signature, event_id):
    ready, _provider, message = razorpay_ready()
    if not ready:
        raise PaymentGatewayConfigurationError(message)
    if not signature or not verify_webhook_signature(raw_body, signature):
        raise PaymentGatewayConfigurationError("Invalid Razorpay webhook signature.")
    if not event_id:
        raise PaymentGatewayConfigurationError("Missing x-razorpay-event-id header.")

    payload = json.loads(raw_body.decode("utf-8"))
    event_type = payload.get("event", "")
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    order_entity = payload.get("payload", {}).get("order", {}).get("entity", {})
    provider_order_id = payment_entity.get("order_id") or order_entity.get("id")
    provider_payment_id = payment_entity.get("id") or ""
    order = PaymentGatewayOrder.objects.filter(provider="razorpay", provider_order_id=provider_order_id).select_related("business", "party", "invoice").first()

    event, created = PaymentGatewayEvent.objects.get_or_create(
        provider="razorpay",
        event_id=event_id,
        defaults={
            "business": order.business if order else None,
            "order": order,
            "event_type": event_type,
            "status": "processed",
            "signature_valid": True,
            "payload": payload,
            "processed_at": timezone.now(),
        },
    )
    if not created:
        event.status = "duplicate"
        event.save(update_fields=["status"])
        return event, False

    if not order:
        event.status = "failed"
        event.message = "Gateway order not found for webhook."
        event.save(update_fields=["status", "message"])
        return event, False

    if event_type in {"payment.captured", "order.paid"}:
        mark_gateway_order_paid(
            order=order,
            payment_id=provider_payment_id,
            signature_verified=True,
            payload=payload,
        )
    elif event_type in {"payment.failed", "order.payment_failed"}:
        order.status = "failed"
        order.provider_status = event_type
        order.provider_payload = payload
        order.save(update_fields=["status", "provider_status", "provider_payload", "updated_at"])

    return event, True
