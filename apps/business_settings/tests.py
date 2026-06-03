import json
from unittest import mock
from datetime import timedelta
from urllib.parse import parse_qs

from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import ActivityLog, Business, User
from apps.accounting.models import ReportShare
from apps.business_settings.models import BusinessNotification, BusinessPreference, ReferralInvite, Reminder, SupportTicket
from apps.parties.models import Party


class ReminderDeliveryLifecycleTests(APITestCase):
    def setUp(self):
        self.business = Business.objects.create(name="Reminder Textile", phone="9300000001")
        self.other_business = Business.objects.create(name="Other Reminder Textile", phone="9300000002")
        self.user = User.objects.create_user(
            mobile="9300000003",
            business=self.business,
            first_name="Admin",
            role="admin",
            is_active=True,
        )
        self.party = Party.objects.create(
            business=self.business,
            name="Reminder Customer",
            party_type="customer",
            mobile="9999990001",
        )
        self.other_party = Party.objects.create(
            business=self.other_business,
            name="Other Customer",
            party_type="customer",
            mobile="9999990002",
        )
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def provider_response(self, payload, status_code=200):
        response = mock.MagicMock()
        response.status = status_code
        response.read.return_value = json.dumps(payload).encode("utf-8")
        response.__enter__.return_value = response
        return response

    @override_settings(DEBUG=True, SMS_PROVIDER="local_stub")
    def test_reminder_create_pending_and_dispatch_sent_with_local_stub(self):
        create_response = self.client.post("/api/v1/settings/reminders/", {
            "party": str(self.party.id),
            "voucher_type": "sales_invoice",
            "message": "Please clear your pending balance.",
            "channel": "sms",
            "scheduled_at": timezone.now().isoformat(),
        }, format="json")

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        reminder = Reminder.objects.get(business=self.business)
        self.assertEqual(reminder.status, "pending")

        pending_response = self.client.get("/api/v1/settings/reminders/pending_actions/")
        self.assertEqual(pending_response.status_code, status.HTTP_200_OK)
        self.assertEqual(pending_response.data["data"]["counts"]["pendingReminders"], 1)

        dispatch_response = self.client.post("/api/v1/settings/reminders/dispatch_due/", {}, format="json")
        self.assertEqual(dispatch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(dispatch_response.data["data"]["sentCount"], 1)

        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "sent")
        self.assertEqual(reminder.attempt_count, 1)
        self.assertIsNotNone(reminder.sent_at)
        self.assertIn("local_stub", reminder.delivery_message)
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="reminder_dispatched",
                entity_type="reminder",
                entity_id=reminder.id,
            ).exists()
        )

    @override_settings(SMS_PROVIDER="disabled", SMS_PROVIDER_API_URL="", SMS_PROVIDER_API_TOKEN="")
    def test_dispatch_due_records_failed_delivery_when_provider_disabled(self):
        reminder = Reminder.objects.create(
            business=self.business,
            party=self.party,
            message="Disabled provider test.",
            channel="sms",
            scheduled_at=timezone.now(),
        )

        response = self.client.post("/api/v1/settings/reminders/dispatch_due/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["failedCount"], 1)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "failed")
        self.assertEqual(reminder.attempt_count, 1)
        self.assertIn("not configured", reminder.delivery_message)

    @override_settings(
        WHATSAPP_PROVIDER="gupshup",
        GUPSHUP_API_URL="https://gupshup.example.test/wa/api/v1/msg",
        GUPSHUP_API_KEY="gupshup-token",
        GUPSHUP_APP_NAME="VastraBook",
        GUPSHUP_SOURCE_NUMBER="919000000000",
    )
    def test_dispatch_due_sends_whatsapp_through_gupshup_and_persists_provider_id(self):
        reminder = Reminder.objects.create(
            business=self.business,
            party=self.party,
            message="WhatsApp provider test.",
            channel="whatsapp",
            scheduled_at=timezone.now(),
        )
        provider_response = self.provider_response({"messageId": "wa-msg-123", "status": "submitted"})

        with mock.patch("apps.business_tools.messaging.urlopen", return_value=provider_response) as provider_call:
            response = self.client.post("/api/v1/settings/reminders/dispatch_due/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["sentCount"], 1)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "sent")
        self.assertEqual(reminder.delivery_provider, "gupshup")
        self.assertEqual(reminder.provider_message_id, "wa-msg-123")
        request = provider_call.call_args.args[0]
        self.assertEqual(request.headers.get("Apikey"), "gupshup-token")
        payload = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(payload["destination"][0], "919999990001")
        self.assertEqual(json.loads(payload["message"][0])["text"], "WhatsApp provider test.")
        self.assertNotIn("gupshup-token", str(response.data))

    @override_settings(
        WHATSAPP_PROVIDER="twilio",
        TWILIO_ACCOUNT_SID="AC123456789",
        TWILIO_AUTH_TOKEN="twilio-token",
        TWILIO_WHATSAPP_FROM="+14155238886",
        TWILIO_API_URL="https://twilio.example.test",
    )
    def test_dispatch_due_supports_twilio_whatsapp_provider(self):
        reminder = Reminder.objects.create(
            business=self.business,
            party=self.party,
            message="Twilio WhatsApp provider test.",
            channel="whatsapp",
            scheduled_at=timezone.now(),
        )
        provider_response = self.provider_response({"sid": "SM123"})

        with mock.patch("apps.business_tools.messaging.urlopen", return_value=provider_response) as provider_call:
            response = self.client.post("/api/v1/settings/reminders/dispatch_due/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "sent")
        self.assertEqual(reminder.delivery_provider, "twilio")
        self.assertEqual(reminder.provider_message_id, "SM123")
        request = provider_call.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/2010-04-01/Accounts/AC123456789/Messages.json"))
        payload = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(payload["To"][0], "whatsapp:+919999990001")
        self.assertEqual(payload["Body"][0], "Twilio WhatsApp provider test.")
        self.assertNotIn("twilio-token", str(response.data))

    def test_reminder_rejects_cross_tenant_party(self):
        response = self.client.post("/api/v1/settings/reminders/", {
            "party": str(self.other_party.id),
            "message": "Cross tenant should fail.",
            "channel": "sms",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Reminder.objects.filter(business=self.business, message__icontains="Cross tenant").exists())

    def test_notification_inbox_syncs_real_pending_data_and_marks_read(self):
        Reminder.objects.create(
            business=self.business,
            party=self.party,
            message="Follow up pending invoice.",
            channel="whatsapp",
            scheduled_at=timezone.now(),
        )

        sync_response = self.client.post("/api/v1/settings/notifications/sync_pending/", {}, format="json")
        self.assertEqual(sync_response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(sync_response.data["counts"]["unread"], 1)
        self.assertTrue(BusinessNotification.objects.filter(business=self.business, status="unread").exists())

        list_response = self.client.get("/api/v1/settings/notifications/?status=unread")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        notification_id = list_response.data["notifications"][0]["id"]
        self.assertEqual(list_response.data["notifications"][0]["status"], "unread")

        mark_response = self.client.post(
            f"/api/v1/settings/notifications/{notification_id}/mark_read/",
            {},
            format="json",
        )
        self.assertEqual(mark_response.status_code, status.HTTP_200_OK)
        self.assertEqual(mark_response.data["notification"]["status"], "read")

        second_sync = self.client.post("/api/v1/settings/notifications/sync_pending/", {}, format="json")
        self.assertEqual(second_sync.status_code, status.HTTP_200_OK)
        self.assertEqual(
            BusinessNotification.objects.filter(business=self.business, source_type="reminder_due").count(),
            1,
        )

        BusinessNotification.objects.create(
            business=self.business,
            source_type="manual_test",
            source_id=None,
            title="Manual test",
            message="Extra unread notification",
        )
        mark_all = self.client.post("/api/v1/settings/notifications/mark_all_read/", {}, format="json")
        self.assertEqual(mark_all.status_code, status.HTTP_200_OK)
        self.assertEqual(mark_all.data["counts"]["unread"], 0)

    def test_notification_wait_updates_returns_tenant_scoped_changes(self):
        own_notification = BusinessNotification.objects.create(
            business=self.business,
            source_type="manual_wait_test",
            source_id=None,
            title="Own tenant alert",
            message="Visible only to this business.",
        )
        BusinessNotification.objects.create(
            business=self.other_business,
            source_type="manual_wait_test",
            source_id=None,
            title="Other tenant alert",
            message="Must not leak across tenants.",
        )
        since = (own_notification.updated_at - timedelta(seconds=1)).isoformat()

        response = self.client.get("/api/v1/settings/notifications/wait_updates/", {
            "since": since,
            "timeout": 0,
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["hasUpdates"])
        titles = [row["title"] for row in response.data["notifications"]]
        self.assertIn("Own tenant alert", titles)
        self.assertNotIn("Other tenant alert", titles)
        self.assertIn("serverTime", response.data)

    def test_notification_wait_updates_times_out_without_false_sync_updates(self):
        self.client.post("/api/v1/settings/notifications/sync_pending/", {}, format="json")
        since = timezone.now().isoformat()

        response = self.client.get("/api/v1/settings/notifications/wait_updates/", {
            "since": since,
            "timeout": 0,
        })

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["hasUpdates"])
        self.assertIn("notifications", response.data)

    def test_ca_report_sharing_prepares_bundle_and_revoke_is_tenant_scoped(self):
        other_user = User.objects.create_user(
            mobile="9300000099",
            business=self.other_business,
            first_name="Other Admin",
            role="admin",
            is_active=True,
        )
        ReportShare.objects.create(
            business=self.other_business,
            report_id="sales-register",
            report_name="Sales Register",
            recipient="other-ca@example.com",
            created_by=other_user,
        )

        share_response = self.client.post("/api/v1/settings/business-preferences/share_ca_reports/", {
            "ca_name": "Raman CA",
            "ca_email": "ca@example.com",
            "date_range": "This Month",
            "report_ids": ["sales-register", "gstr-1", "profit-loss"],
        }, format="json")

        self.assertEqual(share_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(share_response.data["shares"]), 3)
        self.assertEqual(ReportShare.objects.filter(business=self.business, recipient="ca@example.com").count(), 3)
        self.assertEqual(ReportShare.objects.filter(business=self.other_business, status="prepared").count(), 1)
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="ca_reports_shared",
                entity_type="business_preference",
            ).exists()
        )
        self.assertTrue(BusinessNotification.objects.filter(business=self.business, source_type="ca_report_share").exists())

        summary_response = self.client.get("/api/v1/settings/business-preferences/ca_report_sharing/")
        self.assertEqual(summary_response.status_code, status.HTTP_200_OK)
        self.assertEqual(summary_response.data["data"]["summary"]["activeShares"], 3)
        self.assertEqual(summary_response.data["data"]["caEmail"], "ca@example.com")

        revoke_response = self.client.post("/api/v1/settings/business-preferences/revoke_ca_reports/", {
            "recipient": "ca@example.com",
        }, format="json")

        self.assertEqual(revoke_response.status_code, status.HTTP_200_OK)
        self.assertEqual(revoke_response.data["revokedCount"], 3)
        self.assertEqual(ReportShare.objects.filter(business=self.business, status="revoked").count(), 3)
        self.assertEqual(ReportShare.objects.filter(business=self.other_business, status="prepared").count(), 1)
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="ca_reports_revoked",
                entity_type="business_preference",
            ).exists()
        )

    def test_referral_invite_create_activate_and_duplicate_guard_are_tenant_scoped(self):
        BusinessPreference.objects.create(business=self.business, referral_code="SMT2026")
        ReferralInvite.objects.create(
            business=self.other_business,
            referral_code="OTH2026",
            business_name="Other Tenant Invite",
            mobile="9876543210",
        )

        create_response = self.client.post("/api/v1/settings/referral-invites/", {
            "business_name": "Mecheri Textiles",
            "contact_name": "Ravi",
            "mobile": "9876543210",
            "notes": "Met at Salem textile market",
        }, format="json")

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(create_response.data["referral_code"], "SMT2026")
        self.assertEqual(ReferralInvite.objects.filter(business=self.business).count(), 1)
        self.assertEqual(ReferralInvite.objects.filter(business=self.other_business).count(), 1)
        self.assertTrue(BusinessNotification.objects.filter(business=self.business, source_type="referral_invite").exists())

        duplicate_response = self.client.post("/api/v1/settings/referral-invites/", {
            "business_name": "Duplicate Textiles",
            "mobile": "9876543210",
        }, format="json")
        self.assertEqual(duplicate_response.status_code, status.HTTP_400_BAD_REQUEST)

        invite = ReferralInvite.objects.get(business=self.business)
        activate_response = self.client.post(f"/api/v1/settings/referral-invites/{invite.id}/mark_activated/", {}, format="json")
        self.assertEqual(activate_response.status_code, status.HTTP_200_OK)
        invite.refresh_from_db()
        self.assertEqual(invite.status, "activated")
        self.assertEqual(invite.reward_label, "Rs 500 Credit")
        self.assertIsNotNone(invite.activated_at)

    def test_support_ticket_create_resolve_and_list_are_tenant_scoped(self):
        SupportTicket.objects.create(
            business=self.other_business,
            ticket_number="OTH/SUP/0001",
            subject="Other tenant issue",
            message="Must not be visible to this tenant.",
        )

        create_response = self.client.post("/api/v1/settings/support-tickets/", {
            "subject": "Barcode print help",
            "category": "technical",
            "channel": "chat",
            "priority": "high",
            "message": "Barcode labels are not aligned in the thermal printer.",
            "contact_name": "Admin",
            "contact_mobile": "9300000003",
        }, format="json")

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(create_response.data["ticket_number"].endswith("0001"))
        ticket = SupportTicket.objects.get(business=self.business)
        self.assertEqual(ticket.status, "open")
        self.assertTrue(BusinessNotification.objects.filter(business=self.business, source_type="support_ticket").exists())

        list_response = self.client.get("/api/v1/settings/support-tickets/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data), 1)
        self.assertNotIn("Other tenant issue", str(list_response.data))

        resolve_response = self.client.post(f"/api/v1/settings/support-tickets/{ticket.id}/resolve/", {}, format="json")
        self.assertEqual(resolve_response.status_code, status.HTTP_200_OK)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "resolved")
        self.assertIsNotNone(ticket.resolved_at)
