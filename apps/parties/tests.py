from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Business, User
from apps.parties.models import Party
from apps.payments.models import PaymentIn
from apps.sales.models import SalesInvoice


class PartiesSharedLedgerTests(APITestCase):
    def auth_as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def make_user(self, business, mobile="9100000001"):
        return User.objects.create_user(
            mobile=mobile,
            business=business,
            role="admin",
            first_name="Admin",
            is_active=True,
        )

    def test_shared_ledger_register_and_public_portal_are_token_scoped(self):
        business = Business.objects.create(name="CSM SILKS", phone="8608633066")
        other_business = Business.objects.create(name="Other Textile", phone="9000000002")
        user = self.make_user(business)
        customer = Party.objects.create(business=business, name="AARTHI", party_type="customer")
        other_customer = Party.objects.create(business=other_business, name="OTHER PARTY", party_type="customer")

        SalesInvoice.objects.create(
            business=business,
            invoice_number="CSM/26-27/2001",
            party=customer,
            subtotal=1000,
            taxable_amount=1000,
            total_amount=1000,
            paid_amount=0,
            status="unpaid",
        )
        PaymentIn.objects.create(
            business=business,
            payment_number="PMTIN-2001",
            party=customer,
            amount_received=250,
            payment_mode="cash",
        )
        SalesInvoice.objects.create(
            business=other_business,
            invoice_number="OTH/0001",
            party=other_customer,
            subtotal=9999,
            taxable_amount=9999,
            total_amount=9999,
            paid_amount=0,
            status="unpaid",
        )

        self.auth_as(user)
        generate_response = self.client.post(f"/api/v1/parties/parties/{customer.id}/generate_shared_ledger/")
        self.assertEqual(generate_response.status_code, status.HTTP_200_OK)
        self.assertTrue(generate_response.data["url"].startswith("/shared-ledger/"))

        register_response = self.client.get("/api/v1/parties/parties/shared-ledgers/")
        self.assertEqual(register_response.status_code, status.HTTP_200_OK)
        numbers = {row["transactionNumber"] for row in register_response.data["rows"]}
        self.assertIn("CSM/26-27/2001", numbers)
        self.assertIn("PMTIN-2001", numbers)
        self.assertNotIn("OTH/0001", numbers)

        self.client.credentials()
        token = generate_response.data["shared_ledger_token"]
        portal_response = self.client.get(f"/api/v1/parties/shared-ledger/{token}/")
        self.assertEqual(portal_response.status_code, status.HTTP_200_OK)
        self.assertEqual(portal_response.data["business"]["name"], "CSM SILKS")
        self.assertEqual(portal_response.data["party"]["name"], "AARTHI")
        portal_numbers = {row["number"] for row in portal_response.data["ledger"]}
        self.assertIn("CSM/26-27/2001", portal_numbers)
        self.assertIn("PMTIN-2001", portal_numbers)

        missing_response = self.client.get("/api/v1/parties/shared-ledger/not-a-real-token/")
        self.assertEqual(missing_response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_party_soft_deletes_inside_active_tenant(self):
        business = Business.objects.create(name="Delete Tenant", phone="9100000010")
        user = self.make_user(business, "9100000011")
        party = Party.objects.create(business=business, name="OLD PARTY", party_type="customer")

        self.auth_as(user)
        delete_response = self.client.delete(f"/api/v1/parties/parties/{party.id}/")
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)

        party.refresh_from_db()
        self.assertFalse(party.is_active)
        list_response = self.client.get("/api/v1/parties/parties/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data, [])
