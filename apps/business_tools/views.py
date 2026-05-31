from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import permissions, serializers, status, views, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.accounts.throttles import TenantScopedRateThrottle
from django.utils import timezone
from .models import SMSCampaign, SMSCreditLedger, SMSRecipient, SMSTemplate, OnlineOrder
from .serializers import (
    SMSCampaignSerializer,
    SMSCreditLedgerSerializer,
    SMSTemplateSerializer,
    OnlineOrderSerializer,
)
from .messaging import (
    process_messaging_delivery_webhook,
    send_sms_message,
    sms_provider_ready,
    verify_messaging_webhook,
)
from .shipping import (
    ShippingConfigurationError,
    ShippingDeliveryError,
    ShippingOrderValidationError,
    build_shiprocket_order_payload,
    create_shiprocket_order,
    shiprocket_ready,
    sync_shiprocket_tracking,
)
from apps.items.models import Item, ItemGodownStock, apply_stock_movement


STOCK_OUT_STATUSES = {"packed", "shipped", "delivered"}


class OnlineOrderViewSet(viewsets.ModelViewSet):
    serializer_class = OnlineOrderSerializer
    permission_classes = [permissions.IsAuthenticated]
    search_fields = ["order_number", "customer_name", "customer_mobile", "item__name", "item__item_code"]
    ordering_fields = ["order_date", "created_at", "total_amount"]

    def get_queryset(self):
        if not self.request.business:
            return OnlineOrder.objects.none()

        queryset = OnlineOrder.objects.filter(business=self.request.business).select_related("party", "item")
        dispatch_status = self.request.query_params.get("dispatch_status")
        payment_status = self.request.query_params.get("payment_status")
        if dispatch_status:
            queryset = queryset.filter(dispatch_status=dispatch_status)
        if payment_status:
            queryset = queryset.filter(payment_status=payment_status)
        return queryset.order_by("-order_date", "-created_at")

    def _deduct_stock(self, order):
        if order.stock_deducted:
            return

        item = Item.objects.select_for_update().get(id=order.item_id, business=order.business)
        source_stock = ItemGodownStock.objects.select_for_update().filter(
            business=order.business,
            item=item,
            godown=item.godown,
        ).first()
        available_stock = source_stock.current_stock if source_stock else item.current_stock
        if available_stock < order.quantity:
            raise serializers.ValidationError({
                "dispatch_status": f"Only {available_stock:g} PCS available for {item.name}."
            })

        apply_stock_movement(
            business=order.business,
            item=item,
            godown=item.godown,
            movement_type="sale",
            reference_type="online_order",
            reference_id=order.id,
            quantity=-order.quantity,
            rate=order.unit_price,
            created_by=self.request.user,
            notes=f"Online order dispatch {order.order_number}",
        )
        order.stock_deducted = True

    def _restore_stock(self, order):
        if not order.stock_deducted:
            return

        item = Item.objects.select_for_update().get(id=order.item_id, business=order.business)
        apply_stock_movement(
            business=order.business,
            item=item,
            godown=item.godown,
            movement_type="sales_return",
            reference_type="online_order_cancelled",
            reference_id=order.id,
            quantity=order.quantity,
            rate=order.unit_price,
            created_by=self.request.user,
            notes=f"Online order cancelled {order.order_number}",
        )
        order.stock_deducted = False

    @action(detail=True, methods=["post"])
    def set_status(self, request, pk=None):
        dispatch_status = request.data.get("dispatch_status")
        payment_status_value = request.data.get("payment_status")
        allowed_dispatches = {choice[0] for choice in OnlineOrder.DISPATCH_STATUS_CHOICES}
        allowed_payments = {choice[0] for choice in OnlineOrder.PAYMENT_STATUS_CHOICES}

        if dispatch_status and dispatch_status not in allowed_dispatches:
            return Response({"success": False, "message": "Invalid dispatch status"}, status=status.HTTP_400_BAD_REQUEST)
        if payment_status_value and payment_status_value not in allowed_payments:
            return Response({"success": False, "message": "Invalid payment status"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            order = get_object_or_404(
                OnlineOrder.objects.select_for_update().select_related("item"),
                id=pk,
                business=request.business,
            )

            if dispatch_status:
                if dispatch_status in STOCK_OUT_STATUSES:
                    self._deduct_stock(order)
                elif dispatch_status == "cancelled":
                    self._restore_stock(order)
                order.dispatch_status = dispatch_status

            if payment_status_value:
                order.payment_status = payment_status_value

            order.save(update_fields=["dispatch_status", "payment_status", "stock_deducted", "updated_at"])

        serializer = self.get_serializer(order)
        return Response({"success": True, "message": "Online order updated", "order": serializer.data})

    @action(detail=True, methods=["post"])
    def create_shipment(self, request, pk=None):
        order = get_object_or_404(
            OnlineOrder.objects.select_related("business", "party", "item"),
            id=pk,
            business=request.business,
        )
        if order.dispatch_status in {"cancelled", "delivered"}:
            return Response(
                {"success": False, "message": "Cancelled or delivered orders cannot be shipped."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order.shiprocket_order_id or order.shiprocket_shipment_id:
            serializer = self.get_serializer(order)
            return Response({
                "success": True,
                "message": "Shipment already exists for this order.",
                "order": serializer.data,
            })
        try:
            request_payload = build_shiprocket_order_payload(order)
        except ShippingOrderValidationError as exc:
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ShippingConfigurationError as exc:
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        ready, provider, message = shiprocket_ready()
        if not ready:
            return Response(
                {"success": False, "message": message or f"Shipping provider {provider} is not ready."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        with transaction.atomic():
            order = get_object_or_404(
                OnlineOrder.objects.select_for_update().select_related("business", "item"),
                id=pk,
                business=request.business,
            )
            if order.dispatch_status in {"cancelled", "delivered"}:
                return Response(
                    {"success": False, "message": "Cancelled or delivered orders cannot be shipped."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if order.shiprocket_order_id or order.shiprocket_shipment_id:
                serializer = self.get_serializer(order)
                return Response({
                    "success": True,
                    "message": "Shipment already exists for this order.",
                    "order": serializer.data,
                })
            if not order.stock_deducted:
                self._deduct_stock(order)
            if order.dispatch_status == "new":
                order.dispatch_status = "packed"
            order.save(update_fields=["dispatch_status", "stock_deducted", "updated_at"])

        try:
            shipment = create_shiprocket_order(order, request_payload=request_payload)
        except ShippingOrderValidationError as exc:
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ShippingConfigurationError as exc:
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except ShippingDeliveryError as exc:
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        extracted = shipment["extracted"]
        with transaction.atomic():
            order = OnlineOrder.objects.select_for_update().get(id=order.id, business=request.business)
            order.shipping_provider = "shiprocket"
            order.shipping_status = "awb_assigned" if extracted["awb_code"] else "order_created"
            order.shiprocket_order_id = extracted["order_id"]
            order.shiprocket_shipment_id = extracted["shipment_id"]
            order.shiprocket_awb_code = extracted["awb_code"]
            order.shiprocket_courier_name = extracted["courier_name"]
            order.shipping_label_url = extracted["label_url"]
            order.tracking_url = extracted["tracking_url"]
            order.shipping_payload = {
                "request": shipment["request"],
                "response": shipment["response"],
                "awbResponse": shipment["awb_response"],
            }
            if extracted["awb_code"]:
                order.dispatch_status = "shipped"
                order.shipped_at = timezone.now()
            order.save(update_fields=[
                "shipping_provider", "shipping_status", "shiprocket_order_id",
                "shiprocket_shipment_id", "shiprocket_awb_code", "shiprocket_courier_name",
                "shipping_label_url", "tracking_url", "shipping_payload",
                "dispatch_status", "shipped_at", "updated_at",
            ])

        serializer = self.get_serializer(order)
        return Response({
            "success": True,
            "message": "Shiprocket shipment created.",
            "order": serializer.data,
        })

    @action(detail=True, methods=["post"])
    def sync_shipping(self, request, pk=None):
        order = get_object_or_404(
            OnlineOrder.objects.select_related("business", "party", "item"),
            id=pk,
            business=request.business,
        )
        try:
            updates = sync_shiprocket_tracking(order)
        except ShippingOrderValidationError as exc:
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ShippingConfigurationError as exc:
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except ShippingDeliveryError as exc:
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        with transaction.atomic():
            order = OnlineOrder.objects.select_for_update().get(id=order.id, business=request.business)
            for field, value in updates.items():
                setattr(order, field, value)
            order.save(update_fields=[*updates.keys(), "updated_at"])

        serializer = self.get_serializer(order)
        return Response({
            "success": True,
            "message": "Shiprocket tracking synced.",
            "order": serializer.data,
        })


class SMSTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = SMSTemplateSerializer
    permission_classes = [permissions.IsAuthenticated]
    search_fields = ["name", "message", "category"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self):
        if not self.request.business:
            return SMSTemplate.objects.none()
        return SMSTemplate.objects.filter(business=self.request.business, is_active=True).order_by("category", "name")


class SMSCampaignViewSet(viewsets.ModelViewSet):
    serializer_class = SMSCampaignSerializer
    permission_classes = [permissions.IsAuthenticated]
    search_fields = ["campaign_number", "name", "message"]
    ordering_fields = ["created_at", "queued_at", "recipient_count", "credit_cost"]

    def get_queryset(self):
        if not self.request.business:
            return SMSCampaign.objects.none()

        queryset = SMSCampaign.objects.filter(business=self.request.business).select_related("template").prefetch_related("recipients")
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)
        return queryset.order_by("-created_at")

    @action(detail=True, methods=["post"])
    def sync_delivery(self, request, pk=None):
        campaign = self.get_object()
        if campaign.status not in ["queued", "draft"]:
            return Response({"success": False, "message": "Campaign delivery is already closed"}, status=status.HTTP_400_BAD_REQUEST)
        if campaign.status == "draft":
            return Response({"success": False, "message": "Draft campaigns must be queued before delivery sync"}, status=status.HTTP_400_BAD_REQUEST)
        ready, provider, message = sms_provider_ready()
        if not ready:
            return Response(
                {"success": False, "message": message or f"SMS provider {provider} is not ready"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        queued_recipients = list(
            SMSRecipient.objects.filter(
                business=request.business,
                campaign=campaign,
                status="queued",
            ).order_by("created_at", "party_name")
        )
        sent_count = 0
        failed_count = 0
        for recipient in queued_recipients:
            delivery = send_sms_message(
                to=recipient.mobile,
                message=campaign.message,
                metadata={
                    "campaignId": str(campaign.id),
                    "campaignNumber": campaign.campaign_number,
                    "recipientId": str(recipient.id),
                },
            )
            now = timezone.now()
            recipient.provider = delivery.provider
            recipient.provider_message_id = delivery.provider_message_id
            recipient.provider_response = delivery.provider_response
            if delivery.delivered:
                recipient.status = "sent"
                recipient.sent_at = now
                recipient.error_message = ""
                sent_count += 1
            else:
                recipient.status = "failed"
                recipient.error_message = delivery.message
                failed_count += 1
            recipient.save(update_fields=[
                "status", "provider", "provider_message_id", "provider_response",
                "sent_at", "error_message",
            ])

        campaign.delivered_count = campaign.recipients.filter(status__in=["sent", "delivered"]).count()
        campaign.failed_count = campaign.recipients.filter(status="failed").count()
        campaign.status = "completed"
        campaign.completed_at = timezone.now()
        campaign.save(update_fields=["delivered_count", "failed_count", "status", "completed_at", "updated_at"])

        serializer = self.get_serializer(campaign)
        return Response({
            "success": True,
            "message": f"{sent_count} messages accepted, {failed_count} failed",
            "campaign": serializer.data,
        })


class SMSCreditLedgerViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SMSCreditLedgerSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return SMSCreditLedger.objects.none()
        return SMSCreditLedger.objects.filter(business=self.request.business).order_by("-created_at")


class MessagingWebhookView(views.APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [TenantScopedRateThrottle]
    throttle_scope = "messaging_webhook"

    def post(self, request, provider):
        verified, message = verify_messaging_webhook(
            request.body,
            request.headers,
            request.query_params.get("token", ""),
        )
        if not verified:
            return Response({"success": False, "message": message}, status=status.HTTP_403_FORBIDDEN)

        event, processed = process_messaging_delivery_webhook(
            provider=provider,
            payload=request.data,
            raw_body=request.body,
            headers=request.headers,
        )
        return Response({
            "success": True,
            "processed": processed,
            "eventId": str(event.id),
            "status": event.status,
            "targetType": event.target_type,
        })
