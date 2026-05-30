import hashlib
import hmac
import json
import secrets
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


LOCAL_SMS_PROVIDERS = {"local_stub", "demo", "stub"}
OTP_TTL_MINUTES = 10


def generate_login_otp():
    return f"{secrets.randbelow(1_000_000):06d}"


def otp_digest(mobile, otp):
    payload = f"{mobile}:{otp}".encode("utf-8")
    secret = settings.SECRET_KEY.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def otp_matches(mobile, raw_otp, stored_digest):
    return hmac.compare_digest(otp_digest(mobile, raw_otp), stored_digest or "")


def sms_provider_status():
    provider = (getattr(settings, "SMS_PROVIDER", "disabled") or "disabled").strip().lower()
    if provider in LOCAL_SMS_PROVIDERS:
        if getattr(settings, "DEBUG", False):
            return True, provider, ""
        return False, provider, "Local SMS stub is disabled outside DEBUG."
    if provider in {"disabled", ""}:
        return False, "disabled", "SMS provider is not configured."
    if not getattr(settings, "SMS_PROVIDER_API_URL", "") or not getattr(settings, "SMS_PROVIDER_API_TOKEN", ""):
        return False, provider, "SMS provider API URL and token are required."
    return True, provider, ""


def dispatch_login_otp(mobile, otp):
    ready, provider, message = sms_provider_status()
    if not ready:
        return False, provider, message

    sms_text = f"Your login OTP is {otp}. Valid for {OTP_TTL_MINUTES} minutes."
    if provider in LOCAL_SMS_PROVIDERS:
        print("--- LOCAL SMS PROVIDER ---")
        print(f"To: {mobile}")
        print(f"Message: {sms_text}")
        print("--------------------------")
        return True, provider, "OTP sent through local debug SMS provider."

    payload = {
        "to": mobile,
        "message": sms_text,
        "template": "login_otp",
        "variables": {
            "otp": otp,
            "ttl_minutes": OTP_TTL_MINUTES,
        },
    }
    request = Request(
        settings.SMS_PROVIDER_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.SMS_PROVIDER_API_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=10) as response:
            if 200 <= response.status < 300:
                return True, provider, "OTP sent through configured SMS provider."
            return False, provider, f"SMS provider returned HTTP {response.status}."
    except HTTPError as exc:
        return False, provider, f"SMS provider returned HTTP {exc.code}."
    except URLError:
        return False, provider, "SMS provider could not be reached."
