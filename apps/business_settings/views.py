import time

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.accounts.activity import write_activity
from .models import BusinessNotification, BusinessPreference, InvoiceSettings, Reminder, ReminderPreference
from .notifications import (
    mark_notification_read,
    notification_counts,
    serialize_notifications,
    upsert_notification,
)
from .serializers import (
    BusinessNotificationSerializer,
    BusinessPreferenceSerializer,
    InvoiceSettingsSerializer,
    ReminderPreferenceSerializer,
    ReminderSerializer,
)


def _num(value):
    if value is None:
        return 0.0
    return float(value)


def _build_pending_notification_snapshot(business):
    preferences, created = ReminderPreference.objects.get_or_create(business=business)
    now = timezone.now()
    today = timezone.localdate()
    actions = []
    pending_reminders = []

    reminder_rows = (
        Reminder.objects.filter(business=business, status="pending")
        .select_related("party")
        .order_by("scheduled_at", "-created_at")[:25]
    )
    for reminder in reminder_rows:
        due_at = reminder.scheduled_at or reminder.created_at
        row = {
            "id": str(reminder.id),
            "type": reminder.voucher_type or "reminder",
            "title": reminder.party.name if reminder.party else "Scheduled Reminder",
            "message": reminder.message,
            "partyName": reminder.party.name if reminder.party else "",
            "channel": reminder.channel,
            "scheduledAt": timezone.localtime(due_at).isoformat() if due_at else "",
            "createdAt": timezone.localtime(reminder.created_at).isoformat(),
            "voucherType": reminder.voucher_type or "",
            "voucherId": str(reminder.voucher_id) if reminder.voucher_id else "",
            "status": reminder.status,
            "attemptCount": reminder.attempt_count,
            "lastAttemptAt": timezone.localtime(reminder.last_attempt_at).isoformat() if reminder.last_attempt_at else "",
            "deliveryMessage": reminder.delivery_message,
        }
        pending_reminders.append(row)
        if not reminder.scheduled_at or reminder.scheduled_at <= now:
            actions.append({
                "id": f"reminder-{reminder.id}",
                "type": "reminder",
                "title": row["title"],
                "message": reminder.message,
                "count": 1,
                "amount": 0,
                "target": "settings",
                "priority": "medium",
                "dueDate": row["scheduledAt"],
                "createdAt": row["createdAt"],
                "partyName": row["partyName"],
            })

    if preferences.payment_due:
        from apps.sales.models import SalesInvoice

        invoice_rows = SalesInvoice.objects.filter(
            business=business,
            status__in=["unpaid", "partial"],
        ).select_related("party")
        pending_amount = sum(
            max(0.0, _num(invoice.total_amount) - _num(invoice.paid_amount))
            for invoice in invoice_rows
        )
        overdue_count = invoice_rows.filter(due_date__lt=today).count()
        if invoice_rows.exists():
            actions.append({
                "id": "payment-due",
                "type": "payment_due",
                "title": "Payment due reminders",
                "message": (
                    f"{invoice_rows.count()} customer invoices need follow-up"
                    + (f"; {overdue_count} are overdue" if overdue_count else "")
                ),
                "count": invoice_rows.count(),
                "amount": pending_amount,
                "target": "sales-invoices",
                "priority": "high" if overdue_count else "medium",
                "dueDate": today.isoformat(),
                "createdAt": today.isoformat(),
                "partyName": "",
            })

    if preferences.low_stock:
        from apps.items.models import Item

        low_stock_rows = Item.objects.filter(
            business=business,
            is_active=True,
            low_stock_qty__isnull=False,
            current_stock__lte=F("low_stock_qty"),
        ).order_by("current_stock", "name")
        low_stock_names = [item.name for item in low_stock_rows[:3]]
        if low_stock_rows.exists():
            actions.append({
                "id": "low-stock",
                "type": "low_stock",
                "title": "Low stock follow-up",
                "message": ", ".join(low_stock_names) + (" need restocking" if len(low_stock_names) == 1 else " need restocking"),
                "count": low_stock_rows.count(),
                "amount": 0,
                "target": "items",
                "priority": "high",
                "dueDate": today.isoformat(),
                "createdAt": now.isoformat(),
                "partyName": "",
            })

    if preferences.daily_summary:
        from apps.accounting.models import Expense
        from apps.payments.models import PaymentIn, PaymentOut
        from apps.purchases.models import PurchaseInvoice
        from apps.sales.models import SalesInvoice

        sales_today = SalesInvoice.objects.filter(business=business, invoice_date=today).exclude(status="cancelled")
        purchases_today = PurchaseInvoice.objects.filter(business=business, invoice_date=today).exclude(status="cancelled")
        payments_in_today = PaymentIn.objects.filter(business=business, payment_date=today, status="active")
        payments_out_today = PaymentOut.objects.filter(business=business, payment_date=today, status="active")
        expenses_today = Expense.objects.filter(business=business, expense_date=today)
        summary_amount = (
            _num(sales_today.aggregate(total=Sum("total_amount"))["total"])
            + _num(payments_in_today.aggregate(total=Sum("amount_received"))["total"])
            + _num(payments_out_today.aggregate(total=Sum("amount_paid"))["total"])
            + _num(expenses_today.aggregate(total=Sum("total_amount"))["total"])
        )
        activity_count = (
            sales_today.count()
            + purchases_today.count()
            + payments_in_today.count()
            + payments_out_today.count()
            + expenses_today.count()
        )
        actions.append({
            "id": "daily-summary",
            "type": "daily_summary",
            "title": "Daily business summary",
            "message": f"{activity_count} transactions are ready for today's summary",
            "count": activity_count,
            "amount": summary_amount,
            "target": "reports",
            "priority": "low" if activity_count else "medium",
            "dueDate": today.isoformat(),
            "createdAt": today.isoformat(),
            "partyName": "",
        })

    priority_rank = {"high": 0, "medium": 1, "low": 2}
    actions.sort(key=lambda row: (priority_rank.get(row["priority"], 3), row["title"]))
    return {
        "actions": actions,
        "pendingReminders": pending_reminders,
        "counts": {
            "actions": len(actions),
            "pendingReminders": len(pending_reminders),
        },
    }


def _sync_pending_snapshot_to_notifications(business):
    snapshot = _build_pending_notification_snapshot(business)
    synced = []
    for action_row in snapshot["actions"]:
        notification, created = upsert_notification(
            business=business,
            source_type="pending_action",
            source_key=action_row["id"],
            title=action_row["title"],
            message=action_row["message"],
            priority=action_row["priority"],
            target=action_row["target"],
            metadata=action_row,
        )
        synced.append(notification)

    for reminder in snapshot["pendingReminders"]:
        notification, created = upsert_notification(
            business=business,
            source_type="reminder_due",
            source_key=reminder["id"],
            title=f"{reminder['channel'].upper()} reminder due",
            message=reminder["message"],
            priority="medium",
            target="settings",
            metadata=reminder,
        )
        synced.append(notification)

    return snapshot, synced


def _parse_notification_cursor(value):
    if not value:
        return timezone.now()

    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


CA_REPORT_BUNDLE = [
    ("sales-register", "Sales Register"),
    ("purchase-register", "Purchase Register"),
    ("gstr-1", "GSTR-1 (Sales)"),
    ("gstr-2", "GSTR-2 (Purchase)"),
    ("gstr-3b", "GSTR-3b"),
    ("tax-summary", "GST / Tax Summary"),
    ("profit-loss", "Profit And Loss Report"),
    ("expense-register", "Expense Register"),
    ("party-statement", "Party Statement (Ledger)"),
    ("stock-summary", "Stock Summary"),
]


def _ca_recipient(preferences, request_data=None):
    request_data = request_data or {}
    return (
        (request_data.get("recipient") or "").strip()
        or (request_data.get("ca_email") or "").strip()
        or (preferences.ca_email or "").strip()
        or (request_data.get("ca_mobile") or "").strip()
        or (preferences.ca_mobile or "").strip()
    )


def _serialize_ca_report_share(share):
    return {
        "id": str(share.id),
        "reportId": share.report_id,
        "reportName": share.report_name,
        "recipient": share.recipient,
        "dateRange": share.date_range,
        "status": share.status,
        "shareToken": share.share_token,
        "createdAt": timezone.localtime(share.created_at).isoformat(),
        "updatedAt": timezone.localtime(share.updated_at).isoformat(),
    }


def _ca_report_payload(business, preferences):
    from apps.accounting.models import ReportShare

    shares = ReportShare.objects.filter(business=business).order_by("-created_at")[:25]
    active_shares = ReportShare.objects.filter(business=business).exclude(status="revoked")
    last_share = shares[0] if shares else None
    return {
        "enabled": preferences.ca_reports_enabled,
        "caName": preferences.ca_name,
        "caEmail": preferences.ca_email,
        "caMobile": preferences.ca_mobile,
        "bundleReports": [
            {"reportId": report_id, "reportName": report_name}
            for report_id, report_name in CA_REPORT_BUNDLE
        ],
        "summary": {
            "totalShares": ReportShare.objects.filter(business=business).count(),
            "activeShares": active_shares.count(),
            "revokedShares": ReportShare.objects.filter(business=business, status="revoked").count(),
            "lastSharedAt": timezone.localtime(last_share.created_at).isoformat() if last_share else "",
            "lastRecipient": last_share.recipient if last_share else "",
        },
        "shares": [_serialize_ca_report_share(share) for share in shares],
    }

class InvoiceSettingsViewSet(viewsets.ModelViewSet):
    serializer_class = InvoiceSettingsSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return InvoiceSettings.objects.none()
        return InvoiceSettings.objects.filter(business=self.request.business)

    @action(detail=False, methods=["get", "put", "patch"])
    def active_settings(self, request):
        """Fetch or update active invoice customization settings for tenant."""
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND
            )
            
        settings_obj, created = InvoiceSettings.objects.get_or_create(
            business=business,
            defaults={
                "theme": "advanced_gst",
                "theme_color": "#5B48F5",
                "paper_size": "A4",
                "thermal_paper_size": "2inch",
                "invoice_prefix": business.invoice_prefix or "INV",
            }
        )
        
        if request.method in ["PUT", "PATCH"]:
            serializer = self.get_serializer(settings_obj, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            saved = serializer.save()
            if "invoice_prefix" in request.data and saved.invoice_prefix != business.invoice_prefix:
                business.invoice_prefix = saved.invoice_prefix
                business.save(update_fields=["invoice_prefix"])
            return Response({"success": True, "data": serializer.data})
            
        serializer = self.get_serializer(settings_obj)
        return Response({"success": True, "data": serializer.data})

class BusinessPreferenceViewSet(viewsets.ModelViewSet):
    serializer_class = BusinessPreferenceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return BusinessPreference.objects.none()
        return BusinessPreference.objects.filter(business=self.request.business)

    @action(detail=False, methods=["get", "put", "patch"])
    def active_preferences(self, request):
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND
            )

        preferences, created = BusinessPreference.objects.get_or_create(
            business=business,
            defaults={
                "business_category": "",
                "enable_gst_billing": bool(business.gstin),
                "show_upi_on_invoice": bool(business.upi_id),
            }
        )

        if request.method in ["PUT", "PATCH"]:
            serializer = self.get_serializer(preferences, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response({"success": True, "data": serializer.data})

        serializer = self.get_serializer(preferences)
        return Response({"success": True, "data": serializer.data})

    @action(detail=False, methods=["get"])
    def ca_report_sharing(self, request):
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND,
            )

        preferences, created = BusinessPreference.objects.get_or_create(business=business)
        return Response({
            "success": True,
            "data": _ca_report_payload(business, preferences),
        })

    @action(detail=False, methods=["post"])
    def share_ca_reports(self, request):
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND,
            )

        from apps.accounting.models import ReportShare

        preferences, created = BusinessPreference.objects.get_or_create(business=business)
        ca_name = (request.data.get("ca_name") or preferences.ca_name or "").strip()
        ca_email = (request.data.get("ca_email") or preferences.ca_email or "").strip()
        ca_mobile = (request.data.get("ca_mobile") or preferences.ca_mobile or "").strip()
        recipient = _ca_recipient(preferences, request.data)
        if not recipient:
            return Response(
                {"success": False, "message": "CA email or mobile is required before sharing reports"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        requested_reports = request.data.get("report_ids") or []
        bundle_lookup = dict(CA_REPORT_BUNDLE)
        if requested_reports:
            selected_reports = [
                (report_id, bundle_lookup[report_id])
                for report_id in requested_reports
                if report_id in bundle_lookup
            ]
        else:
            selected_reports = CA_REPORT_BUNDLE
        if not selected_reports:
            return Response(
                {"success": False, "message": "Choose at least one valid report to share"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        date_range = (request.data.get("date_range") or "This Month").strip() or "This Month"
        with transaction.atomic():
            preferences.ca_reports_enabled = True
            preferences.ca_name = ca_name
            preferences.ca_email = ca_email
            preferences.ca_mobile = ca_mobile
            preferences.save(update_fields=[
                "ca_reports_enabled", "ca_name", "ca_email", "ca_mobile", "updated_at",
            ])

            shares = [
                ReportShare.objects.create(
                    business=business,
                    report_id=report_id,
                    report_name=report_name,
                    recipient=recipient,
                    date_range=date_range,
                    filters={"Date Range": date_range, "Access": "CA read only"},
                    status="prepared",
                    created_by=request.user if request.user and request.user.is_authenticated else None,
                )
                for report_id, report_name in selected_reports
            ]
            write_activity(
                business=business,
                user=request.user,
                action="ca_reports_shared",
                entity_type="business_preference",
                entity_id=preferences.id,
                details={
                    "recipient": recipient,
                    "caName": ca_name,
                    "dateRange": date_range,
                    "reportCount": len(shares),
                    "reports": [share.report_id for share in shares],
                },
            )
            upsert_notification(
                business=business,
                source_type="ca_report_share",
                source_key=f"{recipient}:{timezone.now().isoformat()}",
                title="CA reports prepared",
                message=f"{len(shares)} report links prepared for {recipient}",
                priority="medium",
                target="settings",
                metadata={
                    "recipient": recipient,
                    "reportCount": len(shares),
                    "dateRange": date_range,
                },
            )

        return Response({
            "success": True,
            "message": f"{len(shares)} CA report links prepared from live tenant data",
            "data": _ca_report_payload(business, preferences),
            "shares": [_serialize_ca_report_share(share) for share in shares],
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def revoke_ca_reports(self, request):
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND,
            )

        from apps.accounting.models import ReportShare

        preferences, created = BusinessPreference.objects.get_or_create(business=business)
        recipient = _ca_recipient(preferences, request.data)
        shares = ReportShare.objects.filter(business=business).exclude(status="revoked")
        if recipient:
            shares = shares.filter(recipient=recipient)

        now = timezone.now()
        revoked_count = shares.update(status="revoked", updated_at=now)
        preferences.ca_reports_enabled = False
        preferences.save(update_fields=["ca_reports_enabled", "updated_at"])
        write_activity(
            business=business,
            user=request.user,
            action="ca_reports_revoked",
            entity_type="business_preference",
            entity_id=preferences.id,
            details={
                "recipient": recipient,
                "revokedCount": revoked_count,
            },
        )
        upsert_notification(
            business=business,
            source_type="ca_report_revoke",
            source_key=f"{recipient or 'all'}:{now.isoformat()}",
            title="CA report access revoked",
            message=f"{revoked_count} report link{' was' if revoked_count == 1 else 's were'} revoked",
            priority="high" if revoked_count else "low",
            target="settings",
            metadata={"recipient": recipient, "revokedCount": revoked_count},
        )

        return Response({
            "success": True,
            "message": f"{revoked_count} CA report link{' revoked' if revoked_count == 1 else 's revoked'}",
            "data": _ca_report_payload(business, preferences),
            "revokedCount": revoked_count,
        })

class ReminderPreferenceViewSet(viewsets.ModelViewSet):
    serializer_class = ReminderPreferenceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return ReminderPreference.objects.none()
        return ReminderPreference.objects.filter(business=self.request.business)

    @action(detail=False, methods=["get", "put", "patch"])
    def active_reminders(self, request):
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND
            )

        preferences, created = ReminderPreference.objects.get_or_create(business=business)

        if request.method in ["PUT", "PATCH"]:
            serializer = self.get_serializer(preferences, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response({"success": True, "data": serializer.data})

        serializer = self.get_serializer(preferences)
        return Response({"success": True, "data": serializer.data})

class ReminderViewSet(viewsets.ModelViewSet):
    serializer_class = ReminderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return Reminder.objects.none()
        return Reminder.objects.filter(business=self.request.business).select_related("party").order_by("-created_at")

    def perform_create(self, serializer):
        serializer.save(business=self.request.business)

    @action(detail=False, methods=["post"])
    def dispatch_due(self, request):
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND
            )

        limit = min(int(request.data.get("limit") or 50), 100)
        now = timezone.now()
        from apps.business_tools.serializers import sms_provider_ready

        ready, provider, provider_message = sms_provider_ready()
        sent_rows = []
        failed_rows = []

        with transaction.atomic():
            due_reminders = list(
                Reminder.objects.select_for_update()
                .filter(business=business, status="pending")
                .filter(Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=now))
                .order_by("scheduled_at", "created_at")[:limit]
            )

            for reminder in due_reminders:
                reminder.attempt_count += 1
                reminder.last_attempt_at = now
                if ready:
                    reminder.status = "sent"
                    reminder.sent_at = now
                    reminder.delivery_message = (
                        f"{provider} accepted {reminder.channel} reminder"
                        if provider
                        else f"{reminder.channel} reminder delivered"
                    )
                    sent_rows.append(reminder)
                else:
                    reminder.status = "failed"
                    reminder.delivery_message = provider_message or f"{provider} provider is not ready"
                    failed_rows.append(reminder)
                reminder.save(update_fields=[
                    "status", "sent_at", "attempt_count", "last_attempt_at",
                    "delivery_message",
                ])
                try:
                    from apps.accounts.models import ActivityLog

                    ActivityLog.objects.create(
                        business=business,
                        user=request.user if request.user and request.user.is_authenticated else None,
                        action="reminder_dispatched",
                        entity_type="reminder",
                        entity_id=reminder.id,
                        details={
                            "party": reminder.party.name if reminder.party_id else "",
                            "channel": reminder.channel,
                            "status": reminder.status,
                            "attemptCount": reminder.attempt_count,
                            "deliveryMessage": reminder.delivery_message,
                        },
                    )
                    upsert_notification(
                        business=business,
                        source_type="reminder_delivery",
                        source_key=reminder.id,
                        title="Reminder delivery updated",
                        message=reminder.delivery_message or reminder.message,
                        priority="low" if reminder.status == "sent" else "high",
                        target="settings",
                        metadata={
                            "partyName": reminder.party.name if reminder.party_id else "",
                            "channel": reminder.channel,
                            "status": reminder.status,
                            "attemptCount": reminder.attempt_count,
                            "reminderId": str(reminder.id),
                        },
                    )
                except Exception:
                    pass

        return Response({
            "success": True,
            "message": f"{len(sent_rows)} reminders sent, {len(failed_rows)} failed",
            "data": {
                "sentCount": len(sent_rows),
                "failedCount": len(failed_rows),
                "provider": provider,
                "reminders": ReminderSerializer(sent_rows + failed_rows, many=True, context={"request": request}).data,
            },
        })

    @action(detail=False, methods=["get"])
    def pending_actions(self, request):
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response({
            "success": True,
            "data": _build_pending_notification_snapshot(business),
        })


class BusinessNotificationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BusinessNotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return BusinessNotification.objects.none()

        queryset = BusinessNotification.objects.filter(business=self.request.business)
        status_param = self.request.query_params.get("status")
        if status_param and status_param != "all":
            queryset = queryset.filter(status=status_param)
        return queryset.order_by("-created_at")

    def list(self, request, *args, **kwargs):
        if not request.business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND,
            )

        limit = min(int(request.query_params.get("limit") or 50), 100)
        queryset = self.get_queryset()[:limit]
        return Response({
            "success": True,
            "notifications": serialize_notifications(queryset),
            "counts": notification_counts(request.business),
            "serverTime": timezone.now().isoformat(),
        })

    @action(detail=False, methods=["post"])
    def sync_pending(self, request):
        if not request.business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND,
            )

        snapshot, synced = _sync_pending_snapshot_to_notifications(request.business)
        queryset = self.get_queryset()[:50]
        return Response({
            "success": True,
            "message": f"{len(synced)} notifications synced from live tenant data",
            "notifications": serialize_notifications(queryset),
            "counts": notification_counts(request.business),
            "pending": snapshot,
            "serverTime": timezone.now().isoformat(),
        })

    @action(detail=False, methods=["get"])
    def wait_updates(self, request):
        if not request.business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND,
            )

        since = _parse_notification_cursor(request.query_params.get("since"))
        if since is None:
            return Response(
                {"success": False, "message": "Invalid notification cursor"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        status_param = request.query_params.get("status") or "all"
        if status_param not in {"all", "unread", "read", "dismissed"}:
            return Response(
                {"success": False, "message": "Invalid notification status filter"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            timeout_seconds = float(request.query_params.get("timeout") or 20)
        except (TypeError, ValueError):
            timeout_seconds = 20
        timeout_seconds = min(max(timeout_seconds, 0), 25)
        deadline = time.monotonic() + timeout_seconds
        snapshot = None

        while True:
            snapshot, synced = _sync_pending_snapshot_to_notifications(request.business)
            changed_queryset = BusinessNotification.objects.filter(
                business=request.business,
                updated_at__gt=since,
            )
            if status_param != "all":
                changed_queryset = changed_queryset.filter(status=status_param)

            if changed_queryset.exists():
                queryset = self.get_queryset()[:50]
                return Response({
                    "success": True,
                    "hasUpdates": True,
                    "notifications": serialize_notifications(queryset),
                    "counts": notification_counts(request.business),
                    "pending": snapshot,
                    "serverTime": timezone.now().isoformat(),
                })

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                queryset = self.get_queryset()[:50]
                return Response({
                    "success": True,
                    "hasUpdates": False,
                    "notifications": serialize_notifications(queryset),
                    "counts": notification_counts(request.business),
                    "pending": snapshot,
                    "serverTime": timezone.now().isoformat(),
                })
            time.sleep(min(1, remaining))

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        notification = mark_notification_read(self.get_object())
        return Response({
            "success": True,
            "notification": BusinessNotificationSerializer(notification).data,
            "counts": notification_counts(request.business),
            "serverTime": timezone.now().isoformat(),
        })

    @action(detail=False, methods=["post"])
    def mark_all_read(self, request):
        if not request.business:
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND,
            )
        now = timezone.now()
        BusinessNotification.objects.filter(
            business=request.business,
            status="unread",
        ).update(status="read", read_at=now, updated_at=now)
        return Response({
            "success": True,
            "counts": notification_counts(request.business),
            "notifications": serialize_notifications(self.get_queryset()[:50]),
            "serverTime": timezone.now().isoformat(),
        })
