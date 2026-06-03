import json
from io import StringIO
from unittest import mock

from django.core.checks import Tags, run_checks
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounting.models import BankAccount, Expense
from apps.accounts.email_delivery import send_email
from apps.accounts.models import ActivityLog, Business, OTPToken, User
from apps.accounts.otp import otp_matches
from deploy import smoke_check
from apps.items.models import Item
from apps.parties.models import Party
from apps.payments.models import PaymentIn, PaymentOut
from apps.purchases.models import PurchaseInvoice
from apps.sales.models import SalesInvoice


class TenantOnboardingPermissionTests(APITestCase):
    def auth_as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def make_user(self, business, mobile, role="admin", name="Tenant User"):
        return User.objects.create_user(
            mobile=mobile,
            business=business,
            role=role,
            first_name=name,
            is_active=True,
        )

    def frontend_security_headers(self, csp):
        return {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
            "Content-Security-Policy": csp,
            "Cache-Control": "no-store",
        }

    def test_healthz_is_public_and_checks_database(self):
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["success"], True)
        self.assertEqual(response.data["status"], "ok")
        self.assertEqual(response.data["database"], "ok")

    @override_settings(DEBUG=False, SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^healthz$"])
    def test_healthz_stays_available_for_internal_http_health_checks(self):
        response = self.client.get("/healthz", secure=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ok")

    @override_settings(
        DEBUG=False,
        SECURE_SSL_REDIRECT=False,
        SECURE_CONTENT_TYPE_NOSNIFF=True,
        SECURE_REFERRER_POLICY="strict-origin-when-cross-origin",
        X_FRAME_OPTIONS="DENY",
    )
    def test_security_headers_are_set_on_public_health_response(self):
        response = self.client.get("/healthz", secure=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(response["X-Frame-Options"], "DENY")

    @override_settings(DEBUG=True, SMS_PROVIDER="local_stub")
    def test_otp_login_uses_random_hashed_code_for_existing_tenant_user(self):
        business = Business.objects.create(name="OTP Tenant", phone="9000000091")
        self.make_user(business, "9000000092", "admin", "OTP Admin")

        send_response = self.client.post("/api/v1/auth/send-otp", {
            "mobile": "9000000092",
        }, format="json")

        self.assertEqual(send_response.status_code, status.HTTP_200_OK)
        raw_otp = send_response.data["otp_simulated"]
        self.assertRegex(raw_otp, r"^\d{6}$")
        self.assertNotEqual(raw_otp, "123456")
        token = OTPToken.objects.get(mobile="9000000092")
        self.assertNotEqual(token.otp, raw_otp)
        self.assertTrue(otp_matches("9000000092", raw_otp, token.otp))

        verify_response = self.client.post("/api/v1/auth/verify-otp", {
            "mobile": "9000000092",
            "otp": raw_otp,
        }, format="json")

        self.assertEqual(verify_response.status_code, status.HTTP_200_OK)
        self.assertEqual(verify_response.data["user"]["mobile"], "9000000092")
        self.assertIn("access", verify_response.data["tokens"])
        self.assertNotIn("refresh", verify_response.data["tokens"])
        self.assertIn("vastrabook_refresh", verify_response.cookies)
        self.assertTrue(verify_response.cookies["vastrabook_refresh"]["httponly"])
        token.refresh_from_db()
        self.assertTrue(token.used)

    @override_settings(
        DEBUG=True,
        SMS_PROVIDER="local_stub",
        REST_FRAMEWORK={"DEFAULT_THROTTLE_RATES": {"auth_otp": "2/minute"}},
    )
    def test_otp_send_is_scoped_throttled_before_token_creation(self):
        cache.clear()
        business = Business.objects.create(name="Throttled OTP Tenant", phone="9000000101")
        self.make_user(business, "9000000102", "admin", "Throttled Admin")

        responses = [
            self.client.post("/api/v1/auth/send-otp", {"mobile": "9000000102"}, format="json")
            for _ in range(3)
        ]

        self.assertEqual([response.status_code for response in responses], [
            status.HTTP_200_OK,
            status.HTTP_200_OK,
            status.HTTP_429_TOO_MANY_REQUESTS,
        ])
        self.assertEqual(OTPToken.objects.filter(mobile="9000000102").count(), 2)

    @override_settings(DEBUG=False, SMS_PROVIDER="disabled")
    def test_otp_send_requires_real_sms_provider_for_registered_user(self):
        business = Business.objects.create(name="No SMS Tenant", phone="9000000093")
        self.make_user(business, "9000000094", "admin", "No SMS Admin")

        response = self.client.post("/api/v1/auth/send-otp", {
            "mobile": "9000000094",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(OTPToken.objects.filter(mobile="9000000094").count(), 0)
        self.assertNotIn("otp_simulated", response.data)

    @override_settings(
        DEBUG=False,
        SMS_PROVIDER="sms_gateway",
        SMS_PROVIDER_API_URL="https://sms.example.test/send",
        SMS_PROVIDER_API_TOKEN="sms-token",
    )
    def test_otp_send_uses_configured_sms_provider_without_leaking_code(self):
        business = Business.objects.create(name="Real SMS Tenant", phone="9000000095")
        self.make_user(business, "9000000096", "admin", "Real SMS Admin")
        provider_response = mock.MagicMock()
        provider_response.status = 202
        provider_response.__enter__.return_value = provider_response

        with mock.patch("apps.accounts.otp.urlopen", return_value=provider_response) as provider_call:
            response = self.client.post("/api/v1/auth/send-otp", {
                "mobile": "9000000096",
            }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["provider"], "sms_gateway")
        self.assertNotIn("otp_simulated", response.data)
        self.assertEqual(OTPToken.objects.filter(mobile="9000000096").count(), 1)
        self.assertTrue(provider_call.called)

    @override_settings(
        DEBUG=False,
        EMAIL_PROVIDER="resend",
        RESEND_API_URL="https://api.resend.test/emails",
        RESEND_API_KEY="test-resend-token",
        RESEND_FROM_EMAIL="reports@example.com",
    )
    def test_email_delivery_uses_resend_without_hardcoding_secret(self):
        provider_response = mock.MagicMock()
        provider_response.status = 202
        provider_response.read.return_value = b'{"id":"email_test_123"}'
        provider_response.__enter__.return_value = provider_response

        with mock.patch("apps.accounts.email_delivery.urlopen", return_value=provider_response) as provider_call:
            result = send_email(
                to="ca@example.com",
                subject="Report ready",
                html="<p>Report ready</p>",
                text="Report ready",
            )

        self.assertTrue(result.delivered)
        self.assertEqual(result.provider, "resend")
        self.assertEqual(result.provider_response["id"], "email_test_123")
        request = provider_call.call_args.args[0]
        self.assertIn("Bearer test-resend-token", request.headers.get("Authorization"))
        self.assertNotIn("test-resend-token", result.message)

    @override_settings(
        EMAIL_PROVIDER="resend",
        RESEND_API_URL="https://api.resend.test/emails",
        RESEND_API_KEY="test-resend-token",
        RESEND_FROM_EMAIL="reports@example.com",
        SMS_PROVIDER="sms_gateway",
        SMS_PROVIDER_API_URL="https://sms.example.test/send",
        SMS_PROVIDER_API_TOKEN="sms-token",
        PAYMENT_GATEWAY_PROVIDER="razorpay",
        RAZORPAY_KEY_ID="rzp_test_key",
        RAZORPAY_KEY_SECRET="razorpay-secret",
        RAZORPAY_WEBHOOK_SECRET="razorpay-webhook-secret",
        E_INVOICE_PROVIDER="gst_provider",
        E_INVOICE_API_URL="https://einvoice.example.test",
        E_INVOICE_API_TOKEN="einvoice-token",
        E_WAY_BILL_PROVIDER="eway_gateway",
        E_WAY_BILL_API_URL="https://eway.example.test",
        E_WAY_BILL_API_TOKEN="eway-token",
        SHIPPING_PROVIDER="shiprocket",
        SHIPROCKET_API_URL="https://shiprocket.example.test",
        SHIPROCKET_EMAIL="ship@example.com",
        SHIPROCKET_PASSWORD="shiprocket-password",
        SHIPROCKET_PICKUP_LOCATION="Primary",
        WHATSAPP_PROVIDER="gupshup",
        GUPSHUP_API_URL="https://gupshup.example.test",
        GUPSHUP_API_KEY="gupshup-token",
        GUPSHUP_APP_NAME="VastraBook",
        GUPSHUP_SOURCE_NUMBER="919000000000",
    )
    def test_integration_smoke_reports_readiness_without_leaking_secrets(self):
        stdout = StringIO()

        call_command("integration_smoke", "--json", stdout=stdout)

        output = stdout.getvalue()
        payload = json.loads(output)
        self.assertTrue(payload["checks"]["eInvoice"]["ready"])
        self.assertTrue(payload["checks"]["eWayBill"]["ready"])
        self.assertTrue(payload["checks"]["email"]["ready"])
        self.assertTrue(payload["checks"]["paymentGateway"]["ready"])
        self.assertTrue(payload["checks"]["shipping"]["ready"])
        self.assertTrue(payload["checks"]["whatsapp"]["ready"])
        self.assertNotIn("test-resend-token", output)
        self.assertNotIn("razorpay-secret", output)
        self.assertNotIn("einvoice-token", output)
        self.assertNotIn("eway-token", output)
        self.assertNotIn("shiprocket-password", output)
        self.assertNotIn("gupshup-token", output)

    def test_integration_smoke_live_tests_require_network_opt_in(self):
        with self.assertRaises(CommandError):
            call_command("integration_smoke", "--email-to", "owner@example.com")

    def test_deploy_smoke_demo_session_accepts_httponly_cookie_auth(self):
        with mock.patch("deploy.smoke_check.request_json") as request_json:
            request_json.return_value = (
                200,
                {"Set-Cookie": "vastrabook_refresh=token-value; Path=/api/v1/auth; HttpOnly; SameSite=Lax"},
                {"success": True, "tokens": {"access": "access-token"}},
            )

            result = smoke_check.check_demo_session(
                "https://api.example.test",
                "/api/v1",
                "8608633066",
                "vastrabook_refresh",
                10,
            )

        self.assertTrue(result.ok)
        self.assertIn("HttpOnly refresh cookie", result.message)

    def test_deploy_smoke_demo_session_rejects_refresh_json_and_redacts_tokens(self):
        with mock.patch("deploy.smoke_check.request_json") as request_json:
            request_json.return_value = (
                200,
                {},
                {"success": True, "tokens": {"access": "secret-access", "refresh": "secret-refresh"}},
            )

            result = smoke_check.check_demo_session(
                "https://api.example.test",
                "/api/v1",
                "8608633066",
                "vastrabook_refresh",
                10,
            )

        self.assertFalse(result.ok)
        self.assertTrue(result.details["hasAccess"])
        self.assertTrue(result.details["hasRefreshInJson"])
        self.assertFalse(result.details["hasRefreshCookie"])
        self.assertEqual(result.details["body"]["tokens"]["access"], "[redacted]")
        self.assertEqual(result.details["body"]["tokens"]["refresh"], "[redacted]")

    def test_deploy_smoke_frontend_security_accepts_scoped_csp(self):
        csp = "default-src 'self'; connect-src 'self' https://api.example.test"
        html = '<div id="root"></div><script type="module" src="/assets/index.js"></script>'

        with mock.patch("deploy.smoke_check.request_text", return_value=(200, {"Cache-Control": "public, immutable"}, "")):
            result = smoke_check.check_frontend_security(
                "https://app.example.test",
                self.frontend_security_headers(csp),
                html,
                10,
                "https://api.example.test",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.details["expectedApiOrigin"], "https://api.example.test")

    def test_deploy_smoke_frontend_security_rejects_broad_connect_src(self):
        csp = "default-src 'self'; connect-src 'self' https:"
        html = '<div id="root"></div><script type="module" src="/assets/index.js"></script>'

        result = smoke_check.check_frontend_security(
            "https://app.example.test",
            self.frontend_security_headers(csp),
            html,
            10,
            "https://api.example.test",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.details["reason"], "connect-src allows a broad network source.")

    def test_deploy_smoke_frontend_security_requires_expected_api_origin(self):
        csp = "default-src 'self'; connect-src 'self' https://other-api.example.test"
        html = '<div id="root"></div><script type="module" src="/assets/index.js"></script>'

        result = smoke_check.check_frontend_security(
            "https://app.example.test",
            self.frontend_security_headers(csp),
            html,
            10,
            "https://api.example.test",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.details["reason"], "connect-src does not include the expected API origin.")

    @override_settings(DEBUG=True, SMS_PROVIDER="local_stub")
    def test_otp_send_does_not_create_user_or_token_for_unknown_mobile(self):
        response = self.client.post("/api/v1/auth/send-otp", {
            "mobile": "9000000097",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(OTPToken.objects.filter(mobile="9000000097").count(), 0)
        self.assertFalse(User.objects.filter(mobile="9000000097").exists())

    def test_refresh_token_endpoint_renews_existing_tenant_session(self):
        business = Business.objects.create(name="Refresh Tenant", phone="9000000098")
        user = self.make_user(business, "9000000099", "admin", "Refresh Admin")
        refresh = RefreshToken.for_user(user)

        response = self.client.post("/api/v1/auth/token/refresh", {
            "refresh": str(refresh),
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertNotIn("refresh", response.data)
        self.assertIn("vastrabook_refresh", response.cookies)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        workspace_response = self.client.get("/api/v1/auth/workspace")
        self.assertEqual(workspace_response.status_code, status.HTTP_200_OK)
        self.assertEqual(workspace_response.data["business"]["name"], "Refresh Tenant")

    def test_refresh_token_endpoint_accepts_httponly_cookie_session(self):
        business = Business.objects.create(name="Cookie Refresh Tenant", phone="9000000198")
        user = self.make_user(business, "9000000199", "admin", "Cookie Refresh Admin")
        refresh = RefreshToken.for_user(user)
        self.client.cookies["vastrabook_refresh"] = str(refresh)

        response = self.client.post("/api/v1/auth/token/refresh", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertNotIn("refresh", response.data)
        self.assertIn("vastrabook_refresh", response.cookies)
        refresh_cookie = response.cookies["vastrabook_refresh"]
        self.assertTrue(refresh_cookie["httponly"])
        self.assertEqual(refresh_cookie["path"], "/api/v1/auth")

    def test_cookie_refresh_requires_csrf_header_when_csrf_checks_are_enforced(self):
        business = Business.objects.create(name="CSRF Refresh Tenant", phone="9000000298")
        user = self.make_user(business, "9000000299", "admin", "CSRF Refresh Admin")
        refresh = RefreshToken.for_user(user)
        client = APIClient(enforce_csrf_checks=True)
        client.cookies["vastrabook_refresh"] = str(refresh)

        missing_response = client.post("/api/v1/auth/token/refresh", {}, format="json")

        self.assertEqual(missing_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("CSRF", missing_response.data["message"])

        csrf_response = client.get("/api/v1/auth/csrf")
        self.assertEqual(csrf_response.status_code, status.HTTP_200_OK)
        csrf_token = csrf_response.data["csrfToken"]

        refresh_response = client.post(
            "/api/v1/auth/token/refresh",
            {},
            format="json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(refresh_response.status_code, status.HTTP_200_OK)
        self.assertIn("access", refresh_response.data)
        self.assertNotIn("refresh", refresh_response.data)

    def test_logout_clears_refresh_cookie(self):
        self.client.cookies["vastrabook_refresh"] = "stale-refresh-token"

        response = self.client.post("/api/v1/auth/logout", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("vastrabook_refresh", response.cookies)
        self.assertEqual(response.cookies["vastrabook_refresh"].value, "")
        self.assertEqual(response.cookies["vastrabook_refresh"]["path"], "/api/v1/auth")

    def test_cookie_logout_requires_csrf_header_when_csrf_checks_are_enforced(self):
        client = APIClient(enforce_csrf_checks=True)
        client.cookies["vastrabook_refresh"] = "stale-refresh-token"

        missing_response = client.post("/api/v1/auth/logout", {}, format="json")

        self.assertEqual(missing_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("CSRF", missing_response.data["message"])

        csrf_response = client.get("/api/v1/auth/csrf")
        self.assertEqual(csrf_response.status_code, status.HTTP_200_OK)
        csrf_token = csrf_response.data["csrfToken"]

        logout_response = client.post(
            "/api/v1/auth/logout",
            {},
            format="json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(logout_response.status_code, status.HTTP_200_OK)
        self.assertEqual(logout_response.cookies["vastrabook_refresh"].value, "")

    @override_settings(
        DEBUG=False,
        SECRET_KEY="django-insecure-test",
        ALLOWED_HOSTS=["*"],
        CORS_ALLOW_ALL_ORIGINS=True,
        DEMO_SESSION_ENABLED=True,
        SMS_PROVIDER="local_stub",
        E_INVOICE_PROVIDER="disabled",
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    )
    def test_production_checks_block_insecure_deploy_defaults(self):
        messages = run_checks(tags=[Tags.security], include_deployment_checks=True)
        ids = {message.id for message in messages}

        self.assertTrue({
            "accounts.E001",
            "accounts.E002",
            "accounts.E003",
            "accounts.E004",
            "accounts.E005",
            "accounts.E008",
        }.issubset(ids))
        self.assertIn("accounts.W001", ids)
        self.assertIn("accounts.W002", ids)
        self.assertIn("accounts.W003", ids)
        self.assertIn("accounts.W004", ids)
        self.assertIn("accounts.W005", ids)
        self.assertIn("accounts.W006", ids)

    @override_settings(
        DEBUG=False,
        SECRET_KEY="production-grade-secret-key-for-test-only",
        ALLOWED_HOSTS=["api.vastrabook.test"],
        CORS_ALLOW_ALL_ORIGINS=False,
        DEMO_SESSION_ENABLED=False,
        SMS_PROVIDER="sms_gateway",
        SMS_PROVIDER_API_URL="https://sms.example.test/send",
        SMS_PROVIDER_API_TOKEN="sms-token",
        E_INVOICE_PROVIDER="gst_provider",
        E_INVOICE_API_URL="https://einvoice.example.test",
        E_INVOICE_API_TOKEN="einvoice-token",
        E_WAY_BILL_PROVIDER="eway_gateway",
        E_WAY_BILL_API_URL="https://eway.example.test",
        E_WAY_BILL_API_TOKEN="eway-token",
        EMAIL_PROVIDER="resend",
        RESEND_API_KEY="resend-token",
        RESEND_FROM_EMAIL="reports@example.com",
        PAYMENT_GATEWAY_PROVIDER="razorpay",
        RAZORPAY_KEY_ID="rzp_test_key",
        RAZORPAY_KEY_SECRET="razorpay-secret",
        RAZORPAY_WEBHOOK_SECRET="razorpay-webhook-secret",
        SHIPPING_PROVIDER="shiprocket",
        SHIPROCKET_API_URL="https://shiprocket.example.test",
        SHIPROCKET_EMAIL="ship@example.com",
        SHIPROCKET_PASSWORD="shiprocket-password",
        SHIPROCKET_PICKUP_LOCATION="Primary",
        WHATSAPP_PROVIDER="gupshup",
        GUPSHUP_API_URL="https://gupshup.example.test",
        GUPSHUP_API_KEY="gupshup-token",
        GUPSHUP_APP_NAME="VastraBook",
        GUPSHUP_SOURCE_NUMBER="919000000000",
        MESSAGING_WEBHOOK_SECRET="messaging-webhook-secret",
        DATABASES={"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "vastrabook_test"}},
    )
    def test_production_checks_accept_configured_external_providers(self):
        messages = run_checks(tags=[Tags.security], include_deployment_checks=True)
        account_issue_ids = {message.id for message in messages if message.id.startswith("accounts.")}

        self.assertEqual(account_issue_ids, set())

    @override_settings(
        PAYMENT_GATEWAY_PROVIDER="razorpay",
        RAZORPAY_KEY_ID="rzp_test_key",
        RAZORPAY_KEY_SECRET="razorpay-secret",
        RAZORPAY_WEBHOOK_SECRET="",
        SHIPPING_PROVIDER="shiprocket",
        SHIPROCKET_API_URL="https://shiprocket.example.test",
        SHIPROCKET_EMAIL="ship@example.com",
        SHIPROCKET_PASSWORD="shiprocket-password",
        SHIPROCKET_PICKUP_LOCATION="Primary",
        WHATSAPP_PROVIDER="gupshup",
        GUPSHUP_API_URL="https://gupshup.example.test",
        GUPSHUP_API_KEY="gupshup-token",
        GUPSHUP_APP_NAME="VastraBook",
        GUPSHUP_SOURCE_NUMBER="919000000000",
    )
    def test_workspace_provider_status_reports_missing_config_without_leaking_secrets(self):
        business = Business.objects.create(name="Provider Tenant", phone="9000000171")
        admin = self.make_user(business, "9000000172", "admin", "Provider Admin")
        self.auth_as(admin)

        response = self.client.get("/api/v1/auth/workspace")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        provider_status = response.data["providerStatus"]
        self.assertFalse(provider_status["eWayBill"]["configured"])
        self.assertEqual(provider_status["eWayBill"]["missing"], [])
        self.assertFalse(provider_status["paymentGateway"]["configured"])
        self.assertEqual(provider_status["paymentGateway"]["missing"], ["RAZORPAY_WEBHOOK_SECRET"])
        self.assertTrue(provider_status["shipping"]["configured"])
        self.assertTrue(provider_status["whatsapp"]["configured"])
        self.assertNotIn("razorpay-secret", str(provider_status))
        self.assertNotIn("shiprocket-password", str(provider_status))
        self.assertNotIn("gupshup-token", str(provider_status))

    def test_registration_creates_clean_tenant_without_touching_demo_data(self):
        demo_business = Business.objects.create(name="CSM SILKS", phone="8608633066")
        Party.objects.create(business=demo_business, name="Demo Customer", party_type="customer")
        Item.objects.create(business=demo_business, name="Demo Saree", selling_price=1000, purchase_price=700)

        response = self.client.post("/api/v1/auth/register", {
            "business_name": "Fresh Textile House",
            "owner_name": "Fresh Owner",
            "mobile": "9000000001",
            "email": "owner@example.com",
            "state": "Tamil Nadu",
            "invoice_prefix": "FTH",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("vastrabook_refresh", response.cookies)
        self.assertTrue(response.cookies["vastrabook_refresh"]["httponly"])
        self.assertNotIn("refresh", response.data["tokens"])
        business = Business.objects.get(name="Fresh Textile House")
        self.assertEqual(User.objects.get(mobile="9000000001").business, business)
        self.assertEqual(Party.objects.filter(business=business).count(), 0)
        self.assertEqual(Item.objects.filter(business=business).count(), 0)
        self.assertEqual(Party.objects.filter(business=demo_business).count(), 1)
        self.assertEqual(Item.objects.filter(business=demo_business).count(), 1)
        self.assertEqual(response.data["counts"]["items"], 0)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['tokens']['access']}")
        workspace_response = self.client.get("/api/v1/auth/workspace")
        self.assertEqual(workspace_response.status_code, status.HTTP_200_OK)
        self.assertEqual(workspace_response.data["business"]["name"], "Fresh Textile House")
        self.assertEqual(workspace_response.data["counts"], {
            "parties": 0,
            "items": 0,
            "salesInvoices": 0,
            "purchaseInvoices": 0,
            "paymentsIn": 0,
            "paymentsOut": 0,
        })
        self.assertEqual(workspace_response.data["parties"], [])
        self.assertEqual(workspace_response.data["items"], [])
        self.assertEqual(workspace_response.data["godowns"], [])
        self.assertEqual(workspace_response.data["staff"], [])
        self.assertEqual({row["mobile"] for row in workspace_response.data["users"]}, {"9000000001"})

    def test_demo_session_sets_cookie_without_returning_refresh_token(self):
        business = Business.objects.create(name="CSM SILKS", phone="8608633066")
        self.make_user(business, "8608633066", "admin", "CSM")

        response = self.client.post("/api/v1/auth/demo-session", {
            "mobile": "8608633066",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data["tokens"])
        self.assertNotIn("refresh", response.data["tokens"])
        self.assertIn("vastrabook_refresh", response.cookies)
        self.assertTrue(response.cookies["vastrabook_refresh"]["httponly"])

    def test_registration_normalizes_and_rejects_duplicate_mobile(self):
        business = Business.objects.create(name="Existing Textile", phone="9000000101")
        self.make_user(business, "9000000101", "admin", "Existing")

        response = self.client.post("/api/v1/auth/register", {
            "business_name": "Duplicate Textile",
            "owner_name": "Duplicate Owner",
            "mobile": " 90000 00101 ",
            "invoice_prefix": "DUP",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Business.objects.filter(name="Duplicate Textile").exists())

    def test_registration_rejects_invalid_gstin_and_prefix(self):
        response = self.client.post("/api/v1/auth/register", {
            "business_name": "Invalid Textile",
            "owner_name": "Invalid Owner",
            "mobile": "9000000111",
            "gstin": "BADGST",
            "invoice_prefix": "TOO-LONG-PREFIX",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Business.objects.filter(name="Invalid Textile").exists())

    def test_workspace_and_user_list_stay_inside_active_tenant(self):
        business_a = Business.objects.create(name="Tenant A", phone="9000000011")
        business_b = Business.objects.create(name="Tenant B", phone="9000000022")
        admin_a = self.make_user(business_a, "9000000012", "admin", "Admin A")
        self.make_user(business_b, "9000000023", "admin", "Admin B")
        Party.objects.create(business=business_a, name="Tenant A Customer", party_type="customer")
        Party.objects.create(business=business_b, name="Tenant B Customer", party_type="customer")

        self.auth_as(admin_a)
        users_response = self.client.get("/api/v1/auth/users/")
        workspace_response = self.client.get("/api/v1/auth/workspace")

        self.assertEqual(users_response.status_code, status.HTTP_200_OK)
        self.assertEqual(workspace_response.status_code, status.HTTP_200_OK)
        self.assertEqual({row["mobile"] for row in users_response.data}, {"9000000012"})
        party_names = {row["name"] for row in workspace_response.data["parties"]}
        self.assertIn("Tenant A Customer", party_names)
        self.assertNotIn("Tenant B Customer", party_names)

    def test_dashboard_uses_live_tenant_transactions_and_stats(self):
        business = Business.objects.create(name="Dashboard Tenant", phone="9000000071")
        other_business = Business.objects.create(name="Other Dashboard Tenant", phone="9000000072")
        admin = self.make_user(business, "9000000073", "admin", "Admin")
        customer = Party.objects.create(business=business, name="Dashboard Customer", party_type="customer")
        supplier = Party.objects.create(business=business, name="Dashboard Supplier", party_type="supplier")
        other_customer = Party.objects.create(business=other_business, name="Other Customer", party_type="customer")

        SalesInvoice.objects.create(
            business=business,
            invoice_number="INV-LIVE-001",
            party=customer,
            subtotal=1000,
            taxable_amount=1000,
            total_amount=1000,
            paid_amount=250,
            status="partial",
        )
        PurchaseInvoice.objects.create(
            business=business,
            invoice_number="PUR-LIVE-001",
            party=supplier,
            subtotal=600,
            taxable_amount=600,
            total_amount=600,
            paid_amount=100,
            status="partial",
        )
        PaymentIn.objects.create(
            business=business,
            payment_number="PMTIN-LIVE-001",
            party=customer,
            amount_received=250,
            payment_mode="cash",
        )
        PaymentIn.objects.create(
            business=business,
            payment_number="PMTIN-VOID-001",
            party=customer,
            amount_received=250,
            payment_mode="cash",
            status="void",
        )
        PaymentOut.objects.create(
            business=business,
            payment_number="PMTOUT-LIVE-001",
            party=supplier,
            amount_paid=100,
            payment_mode="cash",
        )
        PaymentOut.objects.create(
            business=business,
            payment_number="PMTOUT-VOID-001",
            party=supplier,
            amount_paid=100,
            payment_mode="cash",
            status="void",
        )
        Expense.objects.create(
            business=business,
            expense_number="EXP-LIVE-001",
            expense_category="Packing",
            total_amount=75,
            paid_amount=75,
        )
        Item.objects.create(
            business=business,
            name="Low Stock Silk",
            selling_price=1200,
            purchase_price=800,
            current_stock=1,
            low_stock_qty=2,
        )
        Item.objects.create(
            business=other_business,
            name="Other Low Stock Silk",
            selling_price=9999,
            purchase_price=8000,
            current_stock=0,
            low_stock_qty=5,
        )
        BankAccount.objects.create(
            business=business,
            account_name="Main Cash",
            account_number="CASH",
            ifsc_code="CASH000",
            bank_name="Cash",
            current_balance=500,
        )
        SalesInvoice.objects.create(
            business=other_business,
            invoice_number="INV-OTHER-001",
            party=other_customer,
            subtotal=9999,
            taxable_amount=9999,
            total_amount=9999,
            paid_amount=0,
            status="unpaid",
        )
        SalesInvoice.objects.create(
            business=business,
            invoice_number="INV-CANCELLED-001",
            party=customer,
            subtotal=500,
            taxable_amount=500,
            total_amount=500,
            paid_amount=0,
            status="cancelled",
        )

        self.auth_as(admin)
        response = self.client.get("/api/v1/auth/workspace")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        dashboard = response.data["dashboard"]
        self.assertEqual(dashboard["stats"]["totalSales"], 1000.0)
        self.assertEqual(dashboard["stats"]["totalPurchases"], 600.0)
        self.assertEqual(dashboard["stats"]["bankBalance"], 500.0)
        self.assertEqual(dashboard["stats"]["expenseTotal"], 75.0)
        checklist = {row["id"]: row for row in dashboard["checklist"]}
        self.assertEqual(checklist["collect"]["count"], 1)
        self.assertEqual(checklist["collect"]["target"], "payment-in")
        self.assertEqual(checklist["pay"]["count"], 1)
        self.assertEqual(checklist["pay"]["target"], "payment-out")
        self.assertEqual(checklist["stock"]["count"], 1)
        self.assertEqual(checklist["stock"]["priority"], "high")
        self.assertIn("Low Stock Silk", checklist["stock"]["description"])
        self.assertNotIn("Other Low Stock Silk", checklist["stock"]["description"])
        self.assertEqual(checklist["expense"]["value"], 75.0)

        transaction_numbers = {row["txnNo"] for row in response.data["transactions"]}
        self.assertTrue({
            "INV-LIVE-001",
            "PUR-LIVE-001",
            "PMTIN-LIVE-001",
            "PMTOUT-LIVE-001",
            "EXP-LIVE-001",
        }.issubset(transaction_numbers))
        self.assertNotIn("INV-OTHER-001", transaction_numbers)
        self.assertNotIn("INV-CANCELLED-001", transaction_numbers)
        self.assertNotIn("PMTIN-VOID-001", transaction_numbers)
        self.assertNotIn("PMTOUT-VOID-001", transaction_numbers)
        invoice_statuses = {row["invoiceNumber"]: row["status"] for row in response.data["invoices"]}
        self.assertEqual(invoice_statuses["INV-CANCELLED-001"], "cancelled")

    def test_role_permissions_block_non_admin_user_management(self):
        business = Business.objects.create(name="Role Tenant", phone="9000000031")
        admin = self.make_user(business, "9000000032", "admin", "Admin")
        salesman = self.make_user(business, "9000000033", "salesman", "Sales")
        partner = self.make_user(business, "9000000036", "partner", "Partner")
        target = self.make_user(business, "9000000037", "stock_manager", "Target")

        self.auth_as(salesman)
        blocked_response = self.client.post("/api/v1/auth/users/", {
            "first_name": "Blocked",
            "mobile": "9000000034",
            "role": "salesman",
        }, format="json")
        self.assertEqual(blocked_response.status_code, status.HTTP_403_FORBIDDEN)
        blocked_delete = self.client.delete(f"/api/v1/auth/users/{target.id}/")
        self.assertEqual(blocked_delete.status_code, status.HTTP_403_FORBIDDEN)
        target.refresh_from_db()
        self.assertTrue(target.is_active)

        self.auth_as(partner)
        blocked_list = self.client.get("/api/v1/auth/users/")
        self.assertEqual(blocked_list.status_code, status.HTTP_403_FORBIDDEN)

        self.auth_as(admin)
        allowed_response = self.client.post("/api/v1/auth/users/", {
            "first_name": "Allowed",
            "mobile": "9000000035",
            "role": "stock_manager",
        }, format="json")
        self.assertEqual(allowed_response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(business=business, mobile="9000000035").exists())
        allowed_delete = self.client.delete(f"/api/v1/auth/users/{target.id}/")
        self.assertEqual(allowed_delete.status_code, status.HTTP_200_OK)
        target.refresh_from_db()
        self.assertFalse(target.is_active)

    def test_user_management_reactivates_deleted_user_and_blocks_duplicate_active_mobile(self):
        business = Business.objects.create(name="User Lifecycle Tenant", phone="9000000061")
        admin = self.make_user(business, "9000000062", "admin", "Admin")
        deleted_user = self.make_user(business, "9000000063", "salesman", "Old User")
        deleted_user.is_active = False
        deleted_user.save(update_fields=["is_active"])

        self.auth_as(admin)
        reactivate_response = self.client.post("/api/v1/auth/users/", {
            "first_name": "New User",
            "mobile": "9000000063",
            "role": "accountant",
        }, format="json")
        self.assertEqual(reactivate_response.status_code, status.HTTP_201_CREATED)
        deleted_user.refresh_from_db()
        self.assertTrue(deleted_user.is_active)
        self.assertEqual(deleted_user.first_name, "New User")
        self.assertEqual(deleted_user.role, "accountant")

        duplicate_response = self.client.post("/api/v1/auth/users/", {
            "first_name": "Duplicate",
            "mobile": "9000000063",
            "role": "salesman",
        }, format="json")
        self.assertEqual(duplicate_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_admin_can_update_tenant_user_role_and_status(self):
        business = Business.objects.create(name="User Update Tenant", phone="9000000064")
        admin = self.make_user(business, "9000000065", "admin", "Admin")
        target = self.make_user(business, "9000000066", "salesman", "Counter User")

        self.auth_as(admin)
        response = self.client.patch(f"/api/v1/auth/users/{target.id}/", {
            "first_name": "Accounts Counter",
            "role": "accountant",
            "is_active": False,
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        target.refresh_from_db()
        self.assertEqual(target.business, business)
        self.assertEqual(target.first_name, "Accounts Counter")
        self.assertEqual(target.role, "accountant")
        self.assertFalse(target.is_active)

    def test_deleted_user_token_cannot_access_workspace(self):
        business = Business.objects.create(name="Deleted Tenant", phone="9000000041")
        admin = self.make_user(business, "9000000042", "admin", "Admin")
        deleted_user = self.make_user(business, "9000000043", "salesman", "Deleted User")
        deleted_user_token = RefreshToken.for_user(deleted_user).access_token

        self.auth_as(admin)
        delete_response = self.client.delete(f"/api/v1/auth/users/{deleted_user.id}/")
        self.assertEqual(delete_response.status_code, status.HTTP_200_OK)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {deleted_user_token}")
        response = self.client.get("/api/v1/auth/workspace")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_profile_update_cannot_change_role_active_state_or_business(self):
        business_a = Business.objects.create(name="Tenant Profile A", phone="9000000051")
        business_b = Business.objects.create(name="Tenant Profile B", phone="9000000052")
        salesman = self.make_user(business_a, "9000000053", "salesman", "Sales")

        self.auth_as(salesman)
        response = self.client.patch("/api/v1/auth/profile", {
            "first_name": "Renamed",
            "role": "admin",
            "business": str(business_b.id),
            "is_active": False,
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        salesman.refresh_from_db()
        self.assertEqual(salesman.first_name, "Renamed")
        self.assertEqual(salesman.role, "salesman")
        self.assertEqual(salesman.business, business_a)
        self.assertTrue(salesman.is_active)

    def test_role_module_denials_are_blocked_and_audited(self):
        business = Business.objects.create(name="Audit Tenant", phone="9000000061")
        stock_manager = self.make_user(business, "9000000062", "stock_manager", "Stock")
        salesman = self.make_user(business, "9000000063", "salesman", "Sales")

        self.auth_as(stock_manager)
        sales_response = self.client.get("/api/v1/sales/invoices/")
        self.assertEqual(sales_response.status_code, status.HTTP_403_FORBIDDEN)

        self.auth_as(salesman)
        audit_response = self.client.get("/api/v1/auth/activity")
        self.assertEqual(audit_response.status_code, status.HTTP_403_FORBIDDEN)

        denied_modules = {
            (log.details or {}).get("module")
            for log in ActivityLog.objects.filter(business=business, action="access_denied")
        }
        self.assertIn("sales", denied_modules)
        self.assertIn("audit", denied_modules)
