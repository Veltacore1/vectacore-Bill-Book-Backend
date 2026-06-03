import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email

from apps.accounts.email_delivery import email_provider_status, send_email
from apps.business_tools.messaging import (
    send_sms_message,
    send_whatsapp_message,
    sms_provider_ready,
    whatsapp_provider_ready,
)
from apps.business_tools.shipping import authenticate_shiprocket, shiprocket_ready
from apps.payments.gateway import razorpay_ready


NETWORK_OPTIONS = ("email_to", "sms_to", "whatsapp_to", "shiprocket_auth_check")


def _status_payload(check):
    ready, provider, message = check()
    return {
        "ready": ready,
        "provider": provider,
        "message": message,
    }


def _delivery_payload(result):
    return {
        "delivered": result.delivered,
        "provider": result.provider,
        "message": result.message,
        "statusCode": result.status_code,
        "providerIdPresent": bool(
            getattr(result, "provider_message_id", "")
            or getattr(result, "provider_response", {}).get("id")
        ),
    }


def _einvoice_ready():
    provider = (getattr(settings, "E_INVOICE_PROVIDER", "disabled") or "disabled").strip().lower()
    if provider in {"disabled", ""}:
        return False, "disabled", "E-invoice provider is not configured."
    if provider in {"local_stub", "demo", "stub"}:
        if getattr(settings, "DEBUG", False):
            return True, provider, "Local e-invoice stub is ready for development only."
        return False, provider, "Local e-invoice stub is disabled outside DEBUG."
    missing = [
        name
        for name in ("E_INVOICE_API_URL", "E_INVOICE_API_TOKEN")
        if not getattr(settings, name, "")
    ]
    if missing:
        return False, provider, f"Missing e-invoice settings: {', '.join(missing)}."
    return True, provider, ""


def _eway_bill_ready():
    provider = (getattr(settings, "E_WAY_BILL_PROVIDER", "disabled") or "disabled").strip().lower()
    if provider in {"disabled", ""}:
        return False, "disabled", "E-way bill provider is not configured."
    missing = [
        name
        for name in ("E_WAY_BILL_API_URL", "E_WAY_BILL_API_TOKEN")
        if not getattr(settings, name, "")
    ]
    if missing:
        return False, provider, f"Missing e-way bill settings: {', '.join(missing)}."
    return True, provider, ""


def _validate_email(value):
    try:
        validate_email(value)
    except ValidationError as exc:
        raise CommandError("Provide a valid email address for --email-to.") from exc


class Command(BaseCommand):
    help = "Check external provider readiness and optionally run controlled live provider smoke tests."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
        parser.add_argument(
            "--allow-network",
            action="store_true",
            help="Required before sending live provider requests.",
        )
        parser.add_argument("--email-to", help="Send a Resend email smoke test to this address.")
        parser.add_argument("--sms-to", help="Send an SMS smoke test to this mobile number.")
        parser.add_argument("--whatsapp-to", help="Send a WhatsApp smoke test to this mobile number.")
        parser.add_argument(
            "--shiprocket-auth-check",
            action="store_true",
            help="Call Shiprocket auth and confirm a token is returned without printing it.",
        )

    def handle(self, *args, **options):
        if any(options.get(name) for name in NETWORK_OPTIONS) and not options["allow_network"]:
            raise CommandError("Live provider smoke tests require --allow-network.")

        payload = {
            "checks": {
                "eInvoice": _status_payload(_einvoice_ready),
                "eWayBill": _status_payload(_eway_bill_ready),
                "email": _status_payload(email_provider_status),
                "sms": _status_payload(sms_provider_ready),
                "whatsapp": _status_payload(whatsapp_provider_ready),
                "paymentGateway": _status_payload(razorpay_ready),
                "shipping": _status_payload(shiprocket_ready),
            },
            "live": {},
        }

        if options.get("email_to"):
            _validate_email(options["email_to"])
            payload["live"]["email"] = _delivery_payload(send_email(
                to=options["email_to"],
                subject="VastraBook provider smoke test",
                html="<p>VastraBook email provider smoke test succeeded.</p>",
                text="VastraBook email provider smoke test succeeded.",
            ))

        if options.get("sms_to"):
            payload["live"]["sms"] = _delivery_payload(send_sms_message(
                to=options["sms_to"],
                message="VastraBook SMS provider smoke test.",
                metadata={"source": "integration_smoke"},
            ))

        if options.get("whatsapp_to"):
            payload["live"]["whatsapp"] = _delivery_payload(send_whatsapp_message(
                to=options["whatsapp_to"],
                message="VastraBook WhatsApp provider smoke test.",
                metadata={"source": "integration_smoke"},
            ))

        if options.get("shiprocket_auth_check"):
            token = authenticate_shiprocket()
            payload["live"]["shiprocketAuth"] = {
                "provider": "shiprocket",
                "tokenReceived": bool(token),
            }

        if options["json"]:
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            return

        self.stdout.write("Provider readiness")
        for name, status in payload["checks"].items():
            state = "ready" if status["ready"] else "not ready"
            message = f" - {status['message']}" if status["message"] else ""
            self.stdout.write(f"- {name}: {state} ({status['provider']}){message}")

        if payload["live"]:
            self.stdout.write("")
            self.stdout.write("Live smoke results")
            for name, result in payload["live"].items():
                if name == "shiprocketAuth":
                    state = "token received" if result["tokenReceived"] else "no token"
                    self.stdout.write(f"- {name}: {state}")
                    continue
                state = "accepted" if result["delivered"] else "failed"
                code = f" HTTP {result['statusCode']}" if result["statusCode"] else ""
                self.stdout.write(f"- {name}: {state} ({result['provider']}){code} - {result['message']}")
