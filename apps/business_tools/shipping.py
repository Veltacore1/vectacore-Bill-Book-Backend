import json
import re
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone


class ShippingConfigurationError(Exception):
    pass


class ShippingOrderValidationError(Exception):
    pass


class ShippingDeliveryError(Exception):
    pass


def shiprocket_ready():
    provider = (getattr(settings, "SHIPPING_PROVIDER", "disabled") or "disabled").strip().lower()
    if provider in {"disabled", ""}:
        return False, "disabled", "Shipping provider is not configured."
    if provider != "shiprocket":
        return False, provider, f"Shipping provider '{provider}' is not supported yet."
    missing = [
        name
        for name in ("SHIPROCKET_API_URL", "SHIPROCKET_EMAIL", "SHIPROCKET_PASSWORD", "SHIPROCKET_PICKUP_LOCATION")
        if not getattr(settings, name, "")
    ]
    if missing:
        return False, provider, f"{', '.join(missing)} are required for Shiprocket shipping."
    return True, provider, ""


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


def _shiprocket_request(method, path, payload=None, token=None):
    base_url = getattr(settings, "SHIPROCKET_API_URL", "").rstrip("/")
    if not base_url:
        raise ShippingConfigurationError("SHIPROCKET_API_URL is required for Shiprocket shipping.")

    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "VastraBook/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(f"{base_url}/{path.lstrip('/')}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=25) as response:
            body = _decode_provider_body(response.read())
            if 200 <= response.status < 300:
                return body
            raise ShippingDeliveryError(_provider_error_message("shiprocket", response.status, body))
    except HTTPError as exc:
        body = _decode_provider_body(exc.read())
        raise ShippingDeliveryError(_provider_error_message("shiprocket", exc.code, body)) from exc
    except URLError as exc:
        raise ShippingDeliveryError("Shiprocket could not be reached.") from exc


def authenticate_shiprocket():
    ready, provider, message = shiprocket_ready()
    if not ready:
        raise ShippingConfigurationError(message or f"Shipping provider {provider} is not ready.")

    payload = {
        "email": settings.SHIPROCKET_EMAIL,
        "password": settings.SHIPROCKET_PASSWORD,
    }
    response = _shiprocket_request("POST", "auth/login", payload)
    token = response.get("token") or response.get("data", {}).get("token")
    if not token:
        raise ShippingDeliveryError("Shiprocket authentication response did not include a token.")
    return token


def _as_decimal(setting_name, default):
    value = getattr(settings, setting_name, default) or default
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ShippingConfigurationError(f"{setting_name} must be a positive number.") from exc
    if parsed <= 0:
        raise ShippingConfigurationError(f"{setting_name} must be a positive number.")
    return parsed


def _clean_phone(value):
    digits = re.sub(r"\D+", "", value or "")
    if len(digits) < 10:
        raise ShippingOrderValidationError("Customer mobile number is required before creating a shipment.")
    return digits[-10:]


def _clean_pincode(value):
    digits = re.sub(r"\D+", "", value or "")
    if len(digits) != 6:
        raise ShippingOrderValidationError("A valid 6 digit delivery pincode is required before creating a shipment.")
    return digits


def _required(value, message):
    text = (value or "").strip()
    if not text:
        raise ShippingOrderValidationError(message)
    return text


def _extract_provider_values(payload):
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    order_id = payload.get("order_id") or payload.get("id") or data.get("order_id") or data.get("id")
    shipment_id = payload.get("shipment_id") or data.get("shipment_id")
    awb_code = (
        payload.get("awb_code")
        or payload.get("awb")
        or data.get("awb_code")
        or data.get("awb")
        or data.get("awb_data", {}).get("awb_code")
    )
    courier_name = (
        payload.get("courier_name")
        or data.get("courier_name")
        or data.get("awb_data", {}).get("courier_name")
        or data.get("assigned_courier_name")
    )
    label_url = payload.get("label_url") or data.get("label_url") or data.get("label")
    tracking_url = payload.get("tracking_url") or data.get("tracking_url")
    return {
        "order_id": str(order_id or ""),
        "shipment_id": str(shipment_id or ""),
        "awb_code": str(awb_code or ""),
        "courier_name": str(courier_name or ""),
        "label_url": str(label_url or ""),
        "tracking_url": str(tracking_url or ""),
    }


def build_shiprocket_order_payload(order):
    business = order.business
    item = order.item
    party = order.party
    address = _required(order.delivery_address or (party.address if party else "") or business.address, "Delivery address is required before creating a shipment.")
    city = _required(order.delivery_city or (party.city if party else "") or business.city, "Delivery city is required before creating a shipment.")
    state = _required(order.delivery_state or (party.state if party else "") or business.state, "Delivery state is required before creating a shipment.")
    pincode = _clean_pincode(order.delivery_pincode or (party.pincode if party else "") or business.pincode)
    phone = _clean_phone(order.customer_mobile or (party.mobile if party else "") or business.phone)
    email = order.customer_email or (party.email if party else "") or business.email or ""
    weight = _as_decimal("SHIPROCKET_DEFAULT_WEIGHT_KG", "0.5")
    length = _as_decimal("SHIPROCKET_DEFAULT_LENGTH_CM", "30")
    breadth = _as_decimal("SHIPROCKET_DEFAULT_BREADTH_CM", "24")
    height = _as_decimal("SHIPROCKET_DEFAULT_HEIGHT_CM", "5")
    quantity = Decimal(str(order.quantity))

    return {
        "order_id": order.order_number,
        "order_date": order.order_date.isoformat(),
        "pickup_location": settings.SHIPROCKET_PICKUP_LOCATION,
        "billing_customer_name": _required(order.customer_name, "Customer name is required before creating a shipment."),
        "billing_last_name": "",
        "billing_address": address,
        "billing_address_2": "",
        "billing_city": city,
        "billing_pincode": pincode,
        "billing_state": state,
        "billing_country": "India",
        "billing_email": email,
        "billing_phone": phone,
        "shipping_is_billing": True,
        "order_items": [
            {
                "name": item.name,
                "sku": item.item_code or item.barcode or str(item.id),
                "units": float(quantity),
                "selling_price": str(order.unit_price),
                "discount": "0",
                "tax": str(item.gst_rate or 0),
                "hsn": item.hsn_code or "",
            }
        ],
        "payment_method": "COD" if order.payment_status == "cod" else "Prepaid",
        "sub_total": float(order.taxable_amount),
        "length": float(length),
        "breadth": float(breadth),
        "height": float(height),
        "weight": float(weight),
        "comment": order.notes or "",
    }


def create_shiprocket_order(order, request_payload=None):
    token = authenticate_shiprocket()
    request_payload = request_payload or build_shiprocket_order_payload(order)
    response_payload = _shiprocket_request("POST", "orders/create/adhoc", request_payload, token=token)
    extracted = _extract_provider_values(response_payload)
    if not extracted["order_id"] and not extracted["shipment_id"]:
        raise ShippingDeliveryError("Shiprocket create order response did not include order_id or shipment_id.")
    awb_response_payload = {}
    if extracted["shipment_id"] and not extracted["awb_code"]:
        shipment_id = extracted["shipment_id"]
        awb_payload = {"shipment_id": int(shipment_id) if shipment_id.isdigit() else shipment_id}
        awb_response_payload = _shiprocket_request("POST", "courier/assign/awb", awb_payload, token=token)
        awb_extracted = _extract_provider_values(awb_response_payload)
        for key, value in awb_extracted.items():
            if value:
                extracted[key] = value
    return {
        "request": request_payload,
        "response": response_payload,
        "awb_response": awb_response_payload,
        "extracted": extracted,
    }


def _tracking_status(payload, fallback="awb_assigned"):
    text = json.dumps(payload, default=str).lower()
    if "delivered" in text:
        return "delivered"
    if "out for delivery" in text or "in transit" in text or "shipped" in text or "picked up" in text:
        return "in_transit"
    if "pickup" in text:
        return "pickup_scheduled"
    if "failed" in text or "cancelled" in text or "rto" in text:
        return "failed"
    return fallback


def sync_shiprocket_tracking(order):
    if not order.shiprocket_awb_code:
        raise ShippingOrderValidationError("AWB code is required before syncing Shiprocket tracking.")

    token = authenticate_shiprocket()
    awb_code = quote(order.shiprocket_awb_code)
    response_payload = _shiprocket_request("GET", f"courier/track/awb/{awb_code}", token=token)
    status = _tracking_status(response_payload, fallback=order.shipping_status or "awb_assigned")
    updates = {
        "tracking_payload": response_payload,
        "shipping_status": status,
    }
    if status == "delivered":
        updates["dispatch_status"] = "delivered"
        updates["delivered_at"] = timezone.now()
        updates["payment_status"] = "paid" if order.payment_status == "cod" else order.payment_status
    elif status in {"in_transit", "pickup_scheduled"}:
        updates["dispatch_status"] = "shipped"
    elif status == "failed":
        updates["shipping_status"] = "failed"
    return updates
