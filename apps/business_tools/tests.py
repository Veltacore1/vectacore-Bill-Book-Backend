from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Business, User
from apps.business_tools.models import SMSCampaign, SMSCreditLedger, SMSTemplate
from apps.parties.models import Party


class SMSProviderBoundaryTests(APITestCase):
    def setUp(self):
        self.business = Business.objects.create(name="SMS Textile", phone="9200000001", invoice_prefix="SMT")
        self.user = User.objects.create_user(
            mobile="9200000002",
            business=self.business,
            first_name="Admin",
            role="admin",
            is_active=True,
        )
        self.party = Party.objects.create(
            business=self.business,
            name="Reachable Customer",
            party_type="customer",
            mobile="9999999999",
        )
        self.template = SMSTemplate.objects.create(
            business=self.business,
            name="Offer",
            category="offer",
            message="Silk saree festival offer",
        )
        SMSCreditLedger.objects.create(
            business=self.business,
            entry_type="credit",
            credits=25,
            reference="test-credit",
        )
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    @override_settings(SMS_PROVIDER="disabled", SMS_PROVIDER_API_URL="", SMS_PROVIDER_API_TOKEN="")
    def test_send_now_requires_configured_provider(self):
        response = self.client.post("/api/v1/business-tools/sms-campaigns/", {
            "name": "Festival Campaign",
            "template": str(self.template.id),
            "audience": "all_customers",
            "message": "Silk saree festival offer",
            "send_now": True,
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(SMSCampaign.objects.count(), 0)
