import json
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import Business, User
from apps.business_settings.models import Reminder
from apps.business_tools.models import (
    MessagingDeliveryEvent,
    OnlineOrder,
    SMSCampaign,
    SMSCreditLedger,
    SMSRecipient,
    SMSTemplate,
)
from apps.items.models import Godown, Item, ItemGodownStock
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

    def provider_response(self, payload, status_code=200):
        response = mock.MagicMock()
        response.status = status_code
        response.read.return_value = json.dumps(payload).encode("utf-8")
        response.__enter__.return_value = response
        return response

    def make_online_order(self, *, delivery_pincode="600001", dispatch_status="new", shipping_status="not_created"):
        godown = Godown.objects.create(business=self.business, name="Main")
        item = Item.objects.create(
            business=self.business,
            name="Kanjivaram Silk Saree",
            item_code="KSS-001",
            hsn_code="50072010",
            selling_price=1000,
            purchase_price=700,
            gst_rate=5,
            current_stock=5,
            godown=godown,
        )
        ItemGodownStock.objects.create(
            business=self.business,
            item=item,
            godown=godown,
            opening_stock=5,
            current_stock=5,
        )
        return OnlineOrder.objects.create(
            business=self.business,
            order_number="SMT/ONL/25-26/0001",
            party=self.party,
            item=item,
            customer_name=self.party.name,
            customer_mobile=self.party.mobile,
            customer_email="customer@example.com",
            delivery_address="12 Silk Street",
            delivery_city="Chennai",
            delivery_state="Tamil Nadu",
            delivery_pincode=delivery_pincode,
            quantity=1,
            unit_price=1000,
            taxable_amount=1000,
            tax_amount=50,
            total_amount=1050,
            payment_status="cod",
            dispatch_status=dispatch_status,
            shipping_status=shipping_status,
            source="online_store",
            created_by=self.user,
        )

    def make_campaign_with_sent_recipient(self):
        campaign = SMSCampaign.objects.create(
            business=self.business,
            campaign_number="SMT/SMS/26-27/0001",
            name="Webhook Campaign",
            message="Webhook status test.",
            audience="all_customers",
            recipient_count=1,
            delivered_count=1,
            credit_cost=1,
            status="completed",
            created_by=self.user,
        )
        recipient = SMSRecipient.objects.create(
            business=self.business,
            campaign=campaign,
            party=self.party,
            party_name=self.party.name,
            mobile=self.party.mobile,
            status="sent",
            provider="sms_gateway",
            provider_message_id="sms-msg-123",
        )
        return campaign, recipient

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

    @override_settings(
        SMS_PROVIDER="sms_gateway",
        SMS_PROVIDER_API_URL="https://sms.example.test/send",
        SMS_PROVIDER_API_TOKEN="sms-token",
    )
    def test_sms_campaign_sync_calls_provider_and_records_delivery_status(self):
        create_response = self.client.post("/api/v1/business-tools/sms-campaigns/", {
            "name": "Festival Campaign",
            "template": str(self.template.id),
            "audience": "all_customers",
            "message": "Silk saree festival offer",
            "send_now": True,
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        campaign = SMSCampaign.objects.get()
        provider_response = self.provider_response({"id": "sms-msg-123"})

        with mock.patch("apps.business_tools.messaging.urlopen", return_value=provider_response) as provider_call:
            sync_response = self.client.post(f"/api/v1/business-tools/sms-campaigns/{campaign.id}/sync_delivery/", {}, format="json")

        self.assertEqual(sync_response.status_code, status.HTTP_200_OK)
        self.assertEqual(sync_response.data["campaign"]["delivered_count"], 1)
        self.assertEqual(sync_response.data["campaign"]["failed_count"], 0)
        recipient = SMSRecipient.objects.get(campaign=campaign)
        self.assertEqual(recipient.status, "sent")
        self.assertEqual(recipient.provider, "sms_gateway")
        self.assertEqual(recipient.provider_message_id, "sms-msg-123")
        request = provider_call.call_args.args[0]
        self.assertEqual(request.headers.get("Authorization"), "Bearer sms-token")
        self.assertNotIn("sms-token", str(sync_response.data))
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["to"], "919999999999")
        self.assertEqual(payload["metadata"]["campaignNumber"], campaign.campaign_number)

    @override_settings(
        SMS_PROVIDER="sms_gateway",
        SMS_PROVIDER_API_URL="https://sms.example.test/send",
        SMS_PROVIDER_API_TOKEN="sms-token",
    )
    def test_sms_campaign_sync_records_provider_failure_per_recipient(self):
        create_response = self.client.post("/api/v1/business-tools/sms-campaigns/", {
            "name": "Festival Campaign",
            "template": str(self.template.id),
            "audience": "all_customers",
            "message": "Silk saree festival offer",
            "send_now": True,
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        campaign = SMSCampaign.objects.get()
        provider_response = self.provider_response({"message": "Invalid sender"}, status_code=400)

        with mock.patch("apps.business_tools.messaging.urlopen", return_value=provider_response):
            sync_response = self.client.post(f"/api/v1/business-tools/sms-campaigns/{campaign.id}/sync_delivery/", {}, format="json")

        self.assertEqual(sync_response.status_code, status.HTTP_200_OK)
        self.assertEqual(sync_response.data["campaign"]["delivered_count"], 0)
        self.assertEqual(sync_response.data["campaign"]["failed_count"], 1)
        recipient = SMSRecipient.objects.get(campaign=campaign)
        self.assertEqual(recipient.status, "failed")
        self.assertEqual(recipient.provider, "sms_gateway")
        self.assertIn("HTTP 400", recipient.error_message)

    @override_settings(
        SMS_PROVIDER="sms_gateway",
        SMS_PROVIDER_API_URL="https://sms.example.test/send",
        SMS_PROVIDER_API_TOKEN="sms-token",
    )
    def test_draft_sms_campaign_can_be_queued_and_cancelled_with_credit_refund(self):
        create_response = self.client.post("/api/v1/business-tools/sms-campaigns/", {
            "name": "Draft Campaign",
            "template": str(self.template.id),
            "audience": "all_customers",
            "message": "Silk saree festival offer",
            "send_now": False,
        }, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        campaign = SMSCampaign.objects.get()
        self.assertEqual(campaign.status, "draft")
        self.assertEqual(SMSCreditLedger.objects.filter(entry_type="debit").count(), 0)

        queue_response = self.client.post(f"/api/v1/business-tools/sms-campaigns/{campaign.id}/queue/", {}, format="json")
        self.assertEqual(queue_response.status_code, status.HTTP_200_OK)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "queued")
        self.assertIsNotNone(campaign.queued_at)
        self.assertEqual(SMSCreditLedger.objects.filter(entry_type="debit", reference=campaign.campaign_number).count(), 1)

        cancel_response = self.client.post(f"/api/v1/business-tools/sms-campaigns/{campaign.id}/cancel/", {}, format="json")
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "cancelled")
        self.assertIsNotNone(campaign.completed_at)
        self.assertEqual(
            SMSCreditLedger.objects.filter(entry_type="credit", reference=f"{campaign.campaign_number}:cancelled").count(),
            1,
        )

    def test_campaign_creation_dedupes_parties_sharing_a_mobile_number(self):
        # Two distinct party records with the same mobile number is a real
        # scenario (data entry duplicates, shared family phone), but
        # SMSRecipient has a unique (campaign, mobile) constraint — creating
        # a campaign against both used to crash with an IntegrityError.
        Party.objects.create(
            business=self.business,
            name="Duplicate Mobile Customer",
            party_type="customer",
            mobile=self.party.mobile,
        )

        response = self.client.post("/api/v1/business-tools/sms-campaigns/", {
            "name": "Shared Mobile Campaign",
            "audience": "all_customers",
            "message": "Silk saree festival offer",
            "send_now": False,
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        campaign = SMSCampaign.objects.get()
        self.assertEqual(campaign.recipient_count, 1)
        self.assertEqual(SMSRecipient.objects.filter(campaign=campaign).count(), 1)

    @override_settings(
        SMS_PROVIDER="sms_gateway",
        SMS_PROVIDER_API_URL="https://sms.example.test/send",
        SMS_PROVIDER_API_TOKEN="sms-token",
    )
    def test_sms_campaign_cancel_rejects_provider_sent_campaign(self):
        campaign, recipient = self.make_campaign_with_sent_recipient()
        campaign.status = "queued"
        campaign.queued_at = campaign.created_at
        campaign.save(update_fields=["status", "queued_at", "updated_at"])
        recipient.status = "sent"
        recipient.save(update_fields=["status"])

        response = self.client.post(f"/api/v1/business-tools/sms-campaigns/{campaign.id}/cancel/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, "queued")

    @override_settings(MESSAGING_WEBHOOK_SECRET="webhook-secret")
    def test_messaging_webhook_updates_sms_recipient_and_is_idempotent(self):
        campaign, recipient = self.make_campaign_with_sent_recipient()
        payload = {
            "eventId": "evt-sms-delivered-1",
            "messageId": "sms-msg-123",
            "status": "delivered",
        }

        first_response = self.client.post(
            "/api/v1/business-tools/webhooks/messaging/sms_gateway/",
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            HTTP_X_VASTRABOOK_WEBHOOK_SECRET="webhook-secret",
        )
        second_response = self.client.post(
            "/api/v1/business-tools/webhooks/messaging/sms_gateway/",
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            HTTP_X_VASTRABOOK_WEBHOOK_SECRET="webhook-secret",
        )

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertTrue(first_response.data["processed"])
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertFalse(second_response.data["processed"])
        self.assertEqual(MessagingDeliveryEvent.objects.filter(provider="sms_gateway", event_id="evt-sms-delivered-1").count(), 1)
        recipient.refresh_from_db()
        campaign.refresh_from_db()
        self.assertEqual(recipient.status, "delivered")
        self.assertIsNotNone(recipient.delivered_at)
        self.assertEqual(campaign.delivered_count, 1)
        self.assertEqual(campaign.failed_count, 0)

    @override_settings(MESSAGING_WEBHOOK_SECRET="webhook-secret")
    def test_messaging_webhook_rejects_invalid_secret_without_updating_delivery(self):
        _campaign, recipient = self.make_campaign_with_sent_recipient()

        response = self.client.post(
            "/api/v1/business-tools/webhooks/messaging/sms_gateway/",
            {"eventId": "evt-bad-secret", "messageId": "sms-msg-123", "status": "failed"},
            format="json",
            HTTP_X_VASTRABOOK_WEBHOOK_SECRET="wrong-secret",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(MessagingDeliveryEvent.objects.filter(event_id="evt-bad-secret").exists())
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, "sent")

    @override_settings(
        MESSAGING_WEBHOOK_SECRET="webhook-secret",
        REST_FRAMEWORK={"DEFAULT_THROTTLE_RATES": {"messaging_webhook": "2/minute"}},
    )
    def test_messaging_webhook_is_scoped_throttled(self):
        cache.clear()

        responses = [
            self.client.post(
                "/api/v1/business-tools/webhooks/messaging/gupshup/",
                {"eventId": f"evt-throttle-{index}", "messageId": f"wa-throttle-{index}", "status": "delivered"},
                format="json",
                HTTP_X_VASTRABOOK_WEBHOOK_SECRET="bad-secret",
            )
            for index in range(3)
        ]

        self.assertEqual([response.status_code for response in responses], [
            status.HTTP_403_FORBIDDEN,
            status.HTTP_403_FORBIDDEN,
            status.HTTP_429_TOO_MANY_REQUESTS,
        ])
        self.assertFalse(MessagingDeliveryEvent.objects.filter(event_id__startswith="evt-throttle").exists())

    @override_settings(MESSAGING_WEBHOOK_SECRET="webhook-secret")
    def test_messaging_webhook_updates_whatsapp_reminder_failure(self):
        reminder = Reminder.objects.create(
            business=self.business,
            party=self.party,
            message="Webhook reminder status test.",
            channel="whatsapp",
            status="sent",
            delivery_provider="gupshup",
            provider_message_id="wa-msg-123",
        )

        response = self.client.post(
            "/api/v1/business-tools/webhooks/messaging/gupshup/",
            {
                "eventId": "evt-wa-failed-1",
                "messageId": "wa-msg-123",
                "status": "failed",
                "error": "User cannot receive WhatsApp messages",
            },
            format="json",
            HTTP_X_VASTRABOOK_WEBHOOK_SECRET="webhook-secret",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reminder.refresh_from_db()
        event = MessagingDeliveryEvent.objects.get(event_id="evt-wa-failed-1")
        self.assertEqual(event.business, self.business)
        self.assertEqual(event.target_type, "reminder")
        self.assertEqual(reminder.status, "failed")
        self.assertIn("failed", reminder.delivery_message.lower())

    @override_settings(
        SHIPPING_PROVIDER="shiprocket",
        SHIPROCKET_API_URL="https://shiprocket.example.test",
        SHIPROCKET_EMAIL="ship@example.com",
        SHIPROCKET_PASSWORD="ship-password",
        SHIPROCKET_PICKUP_LOCATION="Primary",
        SHIPROCKET_DEFAULT_LENGTH_CM="30",
        SHIPROCKET_DEFAULT_BREADTH_CM="24",
        SHIPROCKET_DEFAULT_HEIGHT_CM="5",
        SHIPROCKET_DEFAULT_WEIGHT_KG="0.5",
    )
    def test_create_shipment_calls_shiprocket_and_updates_order(self):
        order = self.make_online_order()
        auth_response = self.provider_response({"token": "ship-token"})
        create_response = self.provider_response({
            "order_id": 987654,
            "shipment_id": 456789,
            "awb_code": "AWB123456789",
            "courier_name": "Shiprocket Surface",
            "label_url": "https://shiprocket.example.test/label.pdf",
            "tracking_url": "https://shiprocket.example.test/track/AWB123456789",
        })

        with mock.patch("apps.business_tools.shipping.urlopen", side_effect=[auth_response, create_response]) as provider_call:
            response = self.client.post(f"/api/v1/business-tools/online-orders/{order.id}/create_shipment/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["order"]["shipping_status"], "awb_assigned")
        self.assertEqual(response.data["order"]["shiprocket_awb_code"], "AWB123456789")
        self.assertEqual(response.data["order"]["dispatch_status"], "shipped")
        order.refresh_from_db()
        order.item.refresh_from_db()
        self.assertEqual(order.shiprocket_order_id, "987654")
        self.assertEqual(order.shiprocket_shipment_id, "456789")
        self.assertEqual(order.item.current_stock, 4)
        self.assertEqual(provider_call.call_count, 2)
        create_request = provider_call.call_args_list[1].args[0]
        payload = json.loads(create_request.data.decode("utf-8"))
        self.assertEqual(payload["pickup_location"], "Primary")
        self.assertEqual(payload["billing_pincode"], "600001")
        self.assertEqual(create_request.headers.get("Authorization"), "Bearer ship-token")
        self.assertNotIn("ship-password", str(response.data))

    @override_settings(
        SHIPPING_PROVIDER="shiprocket",
        SHIPROCKET_API_URL="https://shiprocket.example.test",
        SHIPROCKET_EMAIL="ship@example.com",
        SHIPROCKET_PASSWORD="ship-password",
        SHIPROCKET_PICKUP_LOCATION="Primary",
        SHIPROCKET_DEFAULT_LENGTH_CM="30",
        SHIPROCKET_DEFAULT_BREADTH_CM="24",
        SHIPROCKET_DEFAULT_HEIGHT_CM="5",
        SHIPROCKET_DEFAULT_WEIGHT_KG="0.5",
    )
    def test_create_shipment_assigns_awb_when_create_order_returns_only_shipment(self):
        order = self.make_online_order()
        auth_response = self.provider_response({"token": "ship-token"})
        create_response = self.provider_response({"order_id": 987654, "shipment_id": 456789})
        awb_response = self.provider_response({
            "awb_code": "AWB987654321",
            "courier_name": "Delhivery Surface",
        })

        with mock.patch("apps.business_tools.shipping.urlopen", side_effect=[auth_response, create_response, awb_response]) as provider_call:
            response = self.client.post(f"/api/v1/business-tools/online-orders/{order.id}/create_shipment/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.shiprocket_awb_code, "AWB987654321")
        self.assertEqual(order.shiprocket_courier_name, "Delhivery Surface")
        self.assertEqual(order.dispatch_status, "shipped")
        self.assertEqual(provider_call.call_count, 3)
        awb_request = provider_call.call_args_list[2].args[0]
        self.assertTrue(awb_request.full_url.endswith("/courier/assign/awb"))
        self.assertEqual(json.loads(awb_request.data.decode("utf-8"))["shipment_id"], 456789)

    @override_settings(
        SHIPPING_PROVIDER="shiprocket",
        SHIPROCKET_API_URL="https://shiprocket.example.test",
        SHIPROCKET_EMAIL="ship@example.com",
        SHIPROCKET_PASSWORD="ship-password",
        SHIPROCKET_PICKUP_LOCATION="Primary",
    )
    def test_create_shipment_validates_delivery_before_stock_out(self):
        order = self.make_online_order(delivery_pincode="")

        with mock.patch("apps.business_tools.shipping.urlopen") as provider_call:
            response = self.client.post(f"/api/v1/business-tools/online-orders/{order.id}/create_shipment/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("pincode", response.data["message"].lower())
        self.assertFalse(provider_call.called)
        order.refresh_from_db()
        order.item.refresh_from_db()
        self.assertEqual(order.dispatch_status, "new")
        self.assertEqual(order.item.current_stock, 5)

    @override_settings(SHIPPING_PROVIDER="disabled")
    def test_create_shipment_requires_provider_before_stock_out(self):
        order = self.make_online_order()

        response = self.client.post(f"/api/v1/business-tools/online-orders/{order.id}/create_shipment/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        order.refresh_from_db()
        order.item.refresh_from_db()
        self.assertEqual(order.dispatch_status, "new")
        self.assertEqual(order.item.current_stock, 5)

    def test_online_order_manual_lifecycle_deducts_stock_and_marks_cod_paid_on_delivery(self):
        order = self.make_online_order()

        pack_response = self.client.post(
            f"/api/v1/business-tools/online-orders/{order.id}/set_status/",
            {"dispatch_status": "packed"},
            format="json",
        )
        self.assertEqual(pack_response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        order.item.refresh_from_db()
        self.assertEqual(order.dispatch_status, "packed")
        self.assertTrue(order.stock_deducted)
        self.assertEqual(order.item.current_stock, 4)

        ship_response = self.client.post(
            f"/api/v1/business-tools/online-orders/{order.id}/set_status/",
            {"dispatch_status": "shipped"},
            format="json",
        )
        self.assertEqual(ship_response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.dispatch_status, "shipped")
        self.assertIsNotNone(order.shipped_at)

        deliver_response = self.client.post(
            f"/api/v1/business-tools/online-orders/{order.id}/set_status/",
            {"dispatch_status": "delivered"},
            format="json",
        )
        self.assertEqual(deliver_response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.dispatch_status, "delivered")
        self.assertEqual(order.payment_status, "paid")
        self.assertIsNotNone(order.delivered_at)

    def test_online_order_blocks_invalid_or_terminal_dispatch_transitions(self):
        order = self.make_online_order()

        invalid_response = self.client.post(
            f"/api/v1/business-tools/online-orders/{order.id}/set_status/",
            {"dispatch_status": "delivered"},
            format="json",
        )
        self.assertEqual(invalid_response.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        order.item.refresh_from_db()
        self.assertEqual(order.dispatch_status, "new")
        self.assertFalse(order.stock_deducted)
        self.assertEqual(order.item.current_stock, 5)

        cancel_response = self.client.post(
            f"/api/v1/business-tools/online-orders/{order.id}/set_status/",
            {"dispatch_status": "cancelled"},
            format="json",
        )
        self.assertEqual(cancel_response.status_code, status.HTTP_200_OK)
        blocked_response = self.client.post(
            f"/api/v1/business-tools/online-orders/{order.id}/set_status/",
            {"dispatch_status": "packed"},
            format="json",
        )
        self.assertEqual(blocked_response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(
        SHIPPING_PROVIDER="shiprocket",
        SHIPROCKET_API_URL="https://shiprocket.example.test",
        SHIPROCKET_EMAIL="ship@example.com",
        SHIPROCKET_PASSWORD="ship-password",
        SHIPROCKET_PICKUP_LOCATION="Primary",
    )
    def test_sync_shipping_updates_delivered_cod_order(self):
        order = self.make_online_order(dispatch_status="shipped", shipping_status="awb_assigned")
        order.shipping_provider = "shiprocket"
        order.shiprocket_awb_code = "AWB123456789"
        order.save(update_fields=["shipping_provider", "shiprocket_awb_code"])
        auth_response = self.provider_response({"token": "ship-token"})
        tracking_response = self.provider_response({
            "tracking_data": {
                "shipment_status": "Delivered",
                "track_url": "https://shiprocket.example.test/track/AWB123456789",
            }
        })

        with mock.patch("apps.business_tools.shipping.urlopen", side_effect=[auth_response, tracking_response]):
            response = self.client.post(f"/api/v1/business-tools/online-orders/{order.id}/sync_shipping/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.shipping_status, "delivered")
        self.assertEqual(order.dispatch_status, "delivered")
        self.assertEqual(order.payment_status, "paid")
        self.assertTrue(order.tracking_payload)
