from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from .models import SMSCampaign, SMSCreditLedger, SMSRecipient, SMSTemplate, OnlineOrder
from .serializers import (
    SMSCampaignSerializer,
    SMSCreditLedgerSerializer,
    SMSTemplateSerializer,
    OnlineOrderSerializer,
    sms_provider_ready,
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

        with transaction.atomic():
            now = timezone.now()
            updated = SMSRecipient.objects.filter(
                business=request.business,
                campaign=campaign,
                status="queued",
            ).update(status="delivered", delivered_at=now)
            campaign.delivered_count = campaign.recipients.filter(status="delivered").count()
            campaign.failed_count = campaign.recipients.filter(status="failed").count()
            campaign.status = "completed"
            campaign.completed_at = now
            campaign.save(update_fields=["delivered_count", "failed_count", "status", "completed_at", "updated_at"])

        serializer = self.get_serializer(campaign)
        return Response({
            "success": True,
            "message": f"Delivery synced for {updated} recipients",
            "campaign": serializer.data,
        })


class SMSCreditLedgerViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SMSCreditLedgerSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return SMSCreditLedger.objects.none()
        return SMSCreditLedger.objects.filter(business=self.request.business).order_by("-created_at")
