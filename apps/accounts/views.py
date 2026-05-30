from datetime import datetime, timedelta
from decimal import Decimal
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework import status, views, viewsets, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework_simplejwt.tokens import RefreshToken
from .models import Business, User, OTPToken, ActivityLog
from .otp import OTP_TTL_MINUTES, dispatch_login_otp, generate_login_otp, otp_digest, otp_matches
from .permissions import RoleModulePermission, role_permissions_for
from .serializers import (
    BusinessSerializer, UserSerializer, SendOTPSerializer, 
    VerifyOTPSerializer, AddUserSerializer, ActivityLogSerializer,
    TextileTenantRegistrationSerializer
)

PROFILE_MUTABLE_FIELDS = {"first_name", "last_name", "email"}


def _num(value):
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)

def _date(value):
    return value.strftime("%d %b %Y") if value else ""

def _business_payload(business):
    return {
        "id": str(business.id),
        "name": business.name,
        "phone": business.phone or "",
        "gstin": business.gstin or "",
        "prefix": business.invoice_prefix or "CSM",
        "address": business.address or "",
        "city": business.city or "",
        "state": business.state or "",
        "pincode": business.pincode or "",
        "email": business.email or "",
    }


def _external_provider_payload():
    def status_for(provider, api_url, token):
        provider = (provider or "disabled").strip().lower()
        if provider in {"local_stub", "demo", "stub"}:
            return {"provider": provider, "mode": "development", "configured": True}
        if provider in {"disabled", ""}:
            return {"provider": "disabled", "mode": "disabled", "configured": False}
        return {
            "provider": provider,
            "mode": "production",
            "configured": bool(api_url and token),
        }

    return {
        "eInvoice": status_for(settings.E_INVOICE_PROVIDER, settings.E_INVOICE_API_URL, settings.E_INVOICE_API_TOKEN),
        "sms": status_for(settings.SMS_PROVIDER, settings.SMS_PROVIDER_API_URL, settings.SMS_PROVIDER_API_TOKEN),
        "email": {
            "provider": settings.EMAIL_PROVIDER,
            "mode": "development" if settings.EMAIL_PROVIDER in {"local_stub", "demo", "stub"} else ("disabled" if settings.EMAIL_PROVIDER in {"disabled", ""} else "production"),
            "configured": bool(
                settings.EMAIL_PROVIDER in {"local_stub", "demo", "stub"}
                or (settings.EMAIL_PROVIDER == "resend" and settings.RESEND_API_KEY and settings.RESEND_FROM_EMAIL)
            ),
        },
    }


def _token_payload(user):
    refresh = RefreshToken.for_user(user)
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }


def _activity_time(value):
    if not value:
        return ""
    return timezone.localtime(value).strftime("%d %b %Y | %I:%M %p")


def _activity_feed(business, limit=40):
    from apps.items.models import StockMovement
    from apps.payments.models import PaymentIn, PaymentOut
    from apps.purchases.models import PurchaseInvoice
    from apps.sales.models import SalesInvoice

    rows = []
    for log in ActivityLog.objects.filter(business=business).select_related("user").order_by("-created_at")[:limit]:
        actor = log.user.name if log.user_id else "System"
        rows.append({
            "id": str(log.id),
            "at": log.created_at,
            "date": _activity_time(log.created_at),
            "actor": actor,
            "action": log.action.replace("_", " ").title(),
            "module": log.entity_type or "activity",
            "entityId": str(log.entity_id) if log.entity_id else "",
            "details": log.details or {},
        })

    for movement in StockMovement.objects.filter(business=business).select_related("item", "godown", "created_by").order_by("-created_at")[:limit]:
        rows.append({
            "id": str(movement.id),
            "at": movement.created_at,
            "date": _activity_time(movement.created_at),
            "actor": movement.created_by.name if movement.created_by_id else "System",
            "action": "Stock Changed",
            "module": "stock",
            "entityId": str(movement.reference_id or movement.item_id),
            "details": {
                "item": movement.item.name,
                "godown": movement.godown.name if movement.godown else "-",
                "movementType": movement.movement_type,
                "quantity": _num(movement.quantity),
                "balanceAfter": _num(movement.balance_after),
                "referenceType": movement.reference_type or "",
                "notes": movement.notes or "",
            },
        })

    cancelled_sales = SalesInvoice.objects.filter(business=business, status="cancelled").select_related("party", "cancelled_by").order_by("-cancelled_at")[:limit]
    for invoice in cancelled_sales:
        rows.append({
            "id": str(invoice.id),
            "at": invoice.cancelled_at or invoice.updated_at,
            "date": _activity_time(invoice.cancelled_at or invoice.updated_at),
            "actor": invoice.cancelled_by.name if invoice.cancelled_by_id else "System",
            "action": "Voucher Cancelled",
            "module": "sales_invoice",
            "entityId": str(invoice.id),
            "details": {
                "number": invoice.invoice_number,
                "party": invoice.party.name,
                "reason": invoice.cancellation_reason or "",
            },
        })

    for purchase in PurchaseInvoice.objects.filter(business=business, status="cancelled").select_related("party").order_by("-updated_at")[:limit]:
        rows.append({
            "id": str(purchase.id),
            "at": purchase.updated_at,
            "date": _activity_time(purchase.updated_at),
            "actor": "System",
            "action": "Voucher Cancelled",
            "module": "purchase_invoice",
            "entityId": str(purchase.id),
            "details": {
                "number": purchase.invoice_number,
                "party": purchase.party.name,
                "reason": getattr(purchase, "cancellation_reason", "") or "",
            },
        })

    for payment in PaymentIn.objects.filter(business=business, status="void").select_related("party", "cancelled_by").order_by("-cancelled_at")[:limit]:
        rows.append({
            "id": str(payment.id),
            "at": payment.cancelled_at or payment.created_at,
            "date": _activity_time(payment.cancelled_at or payment.created_at),
            "actor": payment.cancelled_by.name if payment.cancelled_by_id else "System",
            "action": "Payment Voided",
            "module": "payment_in",
            "entityId": str(payment.id),
            "details": {
                "number": payment.payment_number,
                "party": payment.party.name,
                "amount": _num(payment.amount_received),
                "reason": payment.cancellation_reason or "",
            },
        })

    for payment in PaymentOut.objects.filter(business=business, status="void").select_related("party", "cancelled_by").order_by("-cancelled_at")[:limit]:
        rows.append({
            "id": str(payment.id),
            "at": payment.cancelled_at or payment.created_at,
            "date": _activity_time(payment.cancelled_at or payment.created_at),
            "actor": payment.cancelled_by.name if payment.cancelled_by_id else "System",
            "action": "Payment Voided",
            "module": "payment_out",
            "entityId": str(payment.id),
            "details": {
                "number": payment.payment_number,
                "party": payment.party.name,
                "amount": _num(payment.amount_paid),
                "reason": payment.cancellation_reason or "",
            },
        })

    for invoice in SalesInvoice.objects.filter(business=business).select_related("party", "created_by").order_by("-updated_at")[:limit * 2]:
        if not invoice.updated_at or not invoice.created_at:
            continue
        if (invoice.updated_at - invoice.created_at).total_seconds() <= 5:
            continue
        rows.append({
            "id": f"sales-edit-{invoice.id}",
            "at": invoice.updated_at,
            "date": _activity_time(invoice.updated_at),
            "actor": invoice.created_by.name if invoice.created_by_id else "System",
            "action": "Voucher Edited",
            "module": "sales_invoice",
            "entityId": str(invoice.id),
            "details": {
                "number": invoice.invoice_number,
                "party": invoice.party.name,
                "status": invoice.status,
                "total": _num(invoice.total_amount),
            },
        })

    for purchase in PurchaseInvoice.objects.filter(business=business).select_related("party", "created_by").order_by("-updated_at")[:limit * 2]:
        if not purchase.updated_at or not purchase.created_at:
            continue
        if (purchase.updated_at - purchase.created_at).total_seconds() <= 5:
            continue
        rows.append({
            "id": f"purchase-edit-{purchase.id}",
            "at": purchase.updated_at,
            "date": _activity_time(purchase.updated_at),
            "actor": purchase.created_by.name if purchase.created_by_id else "System",
            "action": "Voucher Edited",
            "module": "purchase_invoice",
            "entityId": str(purchase.id),
            "details": {
                "number": purchase.invoice_number,
                "party": purchase.party.name,
                "status": purchase.status,
                "total": _num(purchase.total_amount),
            },
        })

    rows.sort(key=lambda row: row["at"] or timezone.now(), reverse=True)
    return [{key: value for key, value in row.items() if key != "at"} for row in rows[:limit]]


class HealthCheckView(views.APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            return Response(
                {
                    "success": False,
                    "status": "unhealthy",
                    "app": "VastraBook",
                    "database": "unavailable",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({
            "success": True,
            "status": "ok",
            "app": "VastraBook",
            "database": "ok",
        })


class TextileTenantRegistrationView(views.APIView):
    permission_classes = [permissions.AllowAny]

    @transaction.atomic
    def post(self, request):
        serializer = TextileTenantRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        owner_name = data["owner_name"].strip()
        first_name, _, last_name = owner_name.partition(" ")
        invoice_prefix = (data.get("invoice_prefix") or "INV").strip().upper()

        business = Business.objects.create(
            name=data["business_name"].strip(),
            phone=data["mobile"].strip(),
            gstin=data.get("gstin") or None,
            state=data.get("state") or "Tamil Nadu",
            address=data.get("address") or None,
            city=data.get("city") or None,
            pincode=data.get("pincode") or None,
            email=data.get("email") or None,
            invoice_prefix=invoice_prefix,
        )

        user = User.objects.filter(mobile=data["mobile"]).first()
        if user:
            user.business = business
            user.role = "admin"
            user.first_name = first_name
            user.last_name = last_name
            user.email = data.get("email") or user.email
            user.is_active = True
            if data.get("password"):
                user.set_password(data["password"])
            user.save()
        else:
            user = User.objects.create_user(
                mobile=data["mobile"],
                password=data.get("password") or None,
                business=business,
                first_name=first_name,
                last_name=last_name,
                email=data.get("email") or "",
                role="admin",
                is_active=True,
            )

        ActivityLog.objects.create(
            business=business,
            user=user,
            action="registered_business",
            entity_type="business",
            entity_id=business.id,
            details={
                "business_name": business.name,
                "owner_mobile": user.mobile,
                "clean_tenant": True,
            },
        )

        return Response({
            "success": True,
            "message": "Textile tenant registered successfully",
            "business": _business_payload(business),
            "user": UserSerializer(user).data,
            "tokens": _token_payload(user),
            "counts": {
                "parties": 0,
                "items": 0,
                "salesInvoices": 0,
                "purchaseInvoices": 0,
                "paymentsIn": 0,
                "paymentsOut": 0,
            },
        }, status=status.HTTP_201_CREATED)

class SendOTPView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = SendOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mobile = serializer.validated_data["mobile"]

        user = User.objects.select_related("business").filter(
            mobile=mobile,
            is_active=True,
            business__isnull=False,
        ).first()
        if not user:
            return Response({
                "success": True,
                "message": "If this mobile is registered, an OTP has been sent.",
            })

        recent_cutoff = timezone.now() - timedelta(minutes=10)
        recent_count = OTPToken.objects.filter(
            mobile=mobile,
            created_at__gte=recent_cutoff,
        ).count()
        if recent_count >= 5:
            return Response(
                {"success": False, "message": "Too many OTP requests. Try again after 10 minutes."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        otp = generate_login_otp()
        delivered, provider, delivery_message = dispatch_login_otp(mobile, otp)
        if not delivered:
            return Response(
                {"success": False, "message": delivery_message, "provider": provider},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        expires_at = timezone.now() + timedelta(minutes=OTP_TTL_MINUTES)
        with transaction.atomic():
            OTPToken.objects.filter(mobile=mobile, used=False).update(used=True)
            OTPToken.objects.create(
                mobile=mobile,
                otp=otp_digest(mobile, otp),
                expires_at=expires_at,
            )

        response = {
            "success": True,
            "message": "OTP sent successfully",
            "provider": provider,
            "expiresInMinutes": OTP_TTL_MINUTES,
        }
        if settings.DEBUG and provider in {"local_stub", "demo", "stub"}:
            response["otp_simulated"] = otp
        return Response(response)

class VerifyOTPView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = VerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mobile = serializer.validated_data["mobile"]
        otp = serializer.validated_data["otp"]

        user = User.objects.select_related("business").filter(
            mobile=mobile,
            is_active=True,
            business__isnull=False,
        ).first()
        if not user:
            return Response(
                {"success": False, "message": "No active tenant user found for this mobile."},
                status=status.HTTP_404_NOT_FOUND,
            )

        tokens = list(OTPToken.objects.filter(
            mobile=mobile,
            expires_at__gt=timezone.now(),
            used=False,
        ).order_by("-created_at")[:5])
        token = next((candidate for candidate in tokens if otp_matches(mobile, otp, candidate.otp)), None)

        if not token:
            return Response(
                {"success": False, "message": "Invalid or expired OTP"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        token.used = True
        token.save(update_fields=["used"])

        user.last_login_at = timezone.now()
        user.save(update_fields=["last_login_at"])

        refresh = RefreshToken.for_user(user)
        return Response({
            "success": True,
            "user": UserSerializer(user).data,
            "tokens": {
                "access": str(refresh.access_token),
                "refresh": str(refresh)
            }
        })

class DemoSessionView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        if not settings.DEBUG and not settings.DEMO_SESSION_ENABLED:
            return Response(
                {"success": False, "message": "Demo session is disabled"},
                status=status.HTTP_403_FORBIDDEN
            )

        mobile = request.data.get("mobile") or settings.DEMO_TENANT_MOBILE
        user = User.objects.select_related("business").filter(mobile=mobile, is_active=True).first()
        if not user or not user.business:
            return Response(
                {"success": False, "message": "Seeded tenant user not found. Run seed_data.py first."},
                status=status.HTTP_404_NOT_FOUND
            )

        refresh = RefreshToken.for_user(user)
        return Response({
            "success": True,
            "user": UserSerializer(user).data,
            "business": _business_payload(user.business),
            "tokens": {
                "access": str(refresh.access_token),
                "refresh": str(refresh)
            }
        })

class UserProfileView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response({
            "success": True,
            "user": UserSerializer(request.user).data
        })

    def patch(self, request):
        profile_data = {
            field: request.data[field]
            for field in PROFILE_MUTABLE_FIELDS
            if field in request.data
        }
        serializer = UserSerializer(request.user, data=profile_data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({
            "success": True,
            "user": serializer.data
        })

class TenantWorkspaceView(views.APIView):
    permission_classes = [permissions.IsAuthenticated, RoleModulePermission]
    module_key = "dashboard"

    def get(self, request):
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active tenant business"},
                status=status.HTTP_404_NOT_FOUND
            )

        from apps.parties.models import Party
        from apps.parties.serializers import PartySerializer
        from apps.items.models import Godown, Item, ItemGodownStock
        from apps.sales.models import CreditNote, DeliveryChallan, ProformaInvoice, Quotation, SalesInvoice, SalesReturn
        from apps.purchases.models import DebitNote, PurchaseInvoice, PurchaseOrder, PurchaseReturn
        from apps.payments.models import PaymentIn, PaymentOut
        from apps.staff.models import Staff, Attendance
        from apps.accounting.models import AutomatedBill, BankAccount, BankTransaction, Expense
        from apps.business_tools.models import SMSCampaign, SMSCreditLedger, SMSTemplate, OnlineOrder
        from apps.business_settings.models import BusinessPreference, InvoiceSettings, ReminderPreference

        parties = []
        receivable = 0.0
        payable = 0.0
        for row in PartySerializer(
            Party.objects.filter(business=business, is_active=True).select_related("category"),
            many=True
        ).data:
            balance = float(row.get("net_balance") or 0)
            receivable += max(0.0, balance)
            payable += abs(min(0.0, balance))
            parties.append({
                "id": row["id"],
                "name": row["name"],
                "mobile": row.get("mobile") or "-",
                "type": "supplier" if row["party_type"] == "supplier" else "customer",
                "balance": balance,
                "opening_balance_type": row["opening_balance_type"],
                "state": row.get("state") or "",
                "category": (row.get("category_details") or {}).get("name", "-"),
                "email": row.get("email") or "",
                "gstin": row.get("gstin") or "",
                "address": row.get("address") or "",
                "city": row.get("city") or "",
                "pincode": row.get("pincode") or "",
                "creditLimit": _num(row.get("credit_limit")) if row.get("credit_limit") is not None else None,
                "creditDays": row.get("credit_days"),
                "sharedLedgerToken": row.get("shared_ledger_token") or "",
            })

        item_queryset = Item.objects.filter(business=business, is_active=True).select_related("category", "godown").prefetch_related("godown_stocks__godown")
        godowns = [
            {
                "id": str(godown.id),
                "name": godown.name,
                "location": godown.address or "",
                "isDefault": godown.is_default,
            }
            for godown in Godown.objects.filter(business=business).order_by("-is_default", "name")
        ]

        items = [
            {
                "id": str(item.id),
                "name": item.name,
                "hsn": item.hsn_code or "",
                "categoryId": str(item.category_id) if item.category_id else "",
                "godownId": str(item.godown_id) if item.godown_id else "",
                "itemCode": item.item_code or item.barcode or "",
                "mrp": _num(item.mrp),
                "price": _num(item.selling_price),
                "purchasePrice": _num(item.purchase_price),
                "stock": _num(item.current_stock),
                "lowStockQuantity": _num(item.low_stock_qty) if item.low_stock_qty is not None else None,
                "godown": item.godown.name if item.godown else "-",
                "onlineStore": item.show_online_store,
                "secondaryUnit": item.secondary_unit or "-",
                "serialisationEnabled": item.serialisation_enabled,
                "godownStocks": [
                    {
                        "godownId": str(stock.godown_id),
                        "godownName": stock.godown.name,
                        "openingStock": _num(stock.opening_stock),
                        "currentStock": _num(stock.current_stock),
                    }
                    for stock in item.godown_stocks.all()
                ],
                "category": item.category.name if item.category else "-",
                "gstRate": _num(item.gst_rate),
                "color": item.color or "",
                "cinDate": item.cin_date or "",
                "grn": item.grn_date or "",
                "grnDate": item.grn_date or "",
                "billNo": item.bill_no or "",
                "description": item.description or "",
            }
            for item in item_queryset.order_by("name")
        ]

        inventory_value = sum(item.current_stock * item.purchase_price for item in item_queryset)
        low_stock_count = sum(
            1
            for item in item_queryset
            if item.low_stock_qty is not None and item.current_stock <= item.low_stock_qty
        )

        party_by_id = {party["id"]: party for party in parties}
        item_by_id = {item["id"]: item for item in items}
        sales_queryset = SalesInvoice.objects.filter(business=business).exclude(status="cancelled")
        invoices = []
        for invoice in SalesInvoice.objects.filter(business=business).select_related("party").prefetch_related("line_items__item").order_by("-invoice_date", "-created_at"):
            invoice_party = party_by_id.get(str(invoice.party_id), {
                "id": str(invoice.party_id),
                "name": invoice.party.name,
                "mobile": invoice.party.mobile or "-",
                "type": "customer",
                "balance": 0,
            })
            invoices.append({
                "id": str(invoice.id),
                "invoiceNumber": invoice.invoice_number,
                "party": invoice_party,
                "date": _date(invoice.invoice_date),
                "items": [
                    {
                        "item": item_by_id.get(str(line.item_id), {
                            "id": str(line.item_id or line.id),
                            "name": line.item_name,
                            "hsn": line.hsn_code or "",
                            "price": _num(line.rate),
                            "purchasePrice": 0,
                            "stock": 0,
                            "godown": "-",
                            "category": "-",
                            "gstRate": _num(line.gst_rate),
                        }),
                        "quantity": _num(line.quantity),
                        "freeQuantity": _num(line.free_quantity),
                        "rate": _num(line.rate),
                        "discountPct": _num(line.discount_pct),
                    }
                    for line in invoice.line_items.all()
                ],
                "subtotal": _num(invoice.subtotal),
                "total": _num(invoice.total_amount),
                "paidAmount": _num(invoice.paid_amount),
                "paymentMode": "cash",
                "status": invoice.status if invoice.status in ["paid", "partial", "unpaid", "cancelled"] else "unpaid",
                "irn": invoice.irn or "",
                "ackNumber": invoice.ack_number or "",
                "ackDate": invoice.ack_date.isoformat() if invoice.ack_date else "",
                "qrCodeData": invoice.qr_code_data or "",
                "eInvoiceStatus": invoice.einvoice_status,
                "eInvoiceProvider": invoice.einvoice_provider,
                "eInvoiceRetryCount": invoice.einvoice_retry_count,
                "eInvoiceLastError": invoice.einvoice_last_error or "",
                "eInvoiceCancelReason": invoice.einvoice_cancel_reason or "",
                "eInvoiceCancelledAt": invoice.einvoice_cancelled_at.isoformat() if invoice.einvoice_cancelled_at else "",
            })

        attendance_rows = Attendance.objects.filter(business=business).select_related("staff")
        attendance_by_staff = {}
        for record in attendance_rows:
            attendance_by_staff.setdefault(str(record.staff_id), {})[record.date.isoformat()] = record.status

        staff = [
            {
                "id": str(member.id),
                "name": member.name,
                "designation": member.designation or "",
                "salary": _num(member.monthly_salary),
                "attendance": attendance_by_staff.get(str(member.id), {}),
            }
            for member in Staff.objects.filter(business=business, is_active=True).order_by("name")
        ]

        def _transaction_sort_key(document_date, created_at):
            return (document_date or timezone.localdate(), created_at or timezone.now())

        transactions = []
        for invoice in SalesInvoice.objects.filter(business=business).exclude(status="cancelled").select_related("party").order_by("-invoice_date", "-created_at")[:8]:
            transactions.append({
                "id": str(invoice.id),
                "sortDate": _transaction_sort_key(invoice.invoice_date, invoice.created_at),
                "date": _date(invoice.invoice_date),
                "type": "Sales Invoices",
                "txnNo": invoice.invoice_number,
                "partyName": invoice.party.name.upper(),
                "amount": _num(invoice.total_amount),
            })
        for purchase in PurchaseInvoice.objects.filter(business=business).exclude(status="cancelled").select_related("party").order_by("-invoice_date", "-created_at")[:5]:
            transactions.append({
                "id": str(purchase.id),
                "sortDate": _transaction_sort_key(purchase.invoice_date, purchase.created_at),
                "date": _date(purchase.invoice_date),
                "type": "Purchase Invoices",
                "txnNo": purchase.invoice_number,
                "partyName": purchase.party.name.upper(),
                "amount": _num(purchase.total_amount),
            })
        for payment in PaymentIn.objects.filter(business=business, status="active").select_related("party").order_by("-payment_date", "-created_at")[:5]:
            transactions.append({
                "id": str(payment.id),
                "sortDate": _transaction_sort_key(payment.payment_date, payment.created_at),
                "date": _date(payment.payment_date),
                "type": "Payment In",
                "txnNo": payment.payment_number,
                "partyName": payment.party.name.upper(),
                "amount": _num(payment.amount_received),
            })
        for payment in PaymentOut.objects.filter(business=business, status="active").select_related("party").order_by("-payment_date", "-created_at")[:5]:
            transactions.append({
                "id": str(payment.id),
                "sortDate": _transaction_sort_key(payment.payment_date, payment.created_at),
                "date": _date(payment.payment_date),
                "type": "Payment Out",
                "txnNo": payment.payment_number,
                "partyName": payment.party.name.upper(),
                "amount": _num(payment.amount_paid),
            })
        for expense in Expense.objects.filter(business=business).order_by("-expense_date", "-created_at")[:5]:
            transactions.append({
                "id": str(expense.id),
                "sortDate": _transaction_sort_key(expense.expense_date, expense.created_at),
                "date": _date(expense.expense_date),
                "type": "Expenses",
                "txnNo": expense.expense_number,
                "partyName": expense.expense_category.upper(),
                "amount": _num(expense.total_amount),
            })
        transactions.sort(key=lambda row: row["sortDate"], reverse=True)
        transactions = [{key: value for key, value in row.items() if key != "sortDate"} for row in transactions]

        users = [
            {
                "id": str(user.id),
                "name": user.name,
                "mobile": user.mobile,
                "role": user.role,
                "isActive": user.is_active,
            }
            for user in User.objects.filter(business=business).order_by("first_name", "mobile")
        ]

        purchase_queryset = PurchaseInvoice.objects.filter(business=business).exclude(status="cancelled")
        sales_total = sales_queryset.aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
        purchase_total = purchase_queryset.aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
        bank_balance = BankAccount.objects.filter(business=business, is_active=True).aggregate(total=Sum("current_balance"))["total"] or Decimal("0")
        expense_total = Expense.objects.filter(business=business).aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
        unpaid_sales = sales_queryset.filter(status__in=["unpaid", "partial"]).count()
        unpaid_purchases = purchase_queryset.filter(status__in=["unpaid", "partial"]).count()

        max_invoice_date = sales_queryset.order_by("-invoice_date").values_list("invoice_date", flat=True).first() or timezone.localdate()
        trend_start = max_invoice_date - timedelta(days=6)
        trend_rows = []
        for offset in range(7):
            day = trend_start + timedelta(days=offset)
            day_sales = sales_queryset.filter(invoice_date=day).aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
            trend_rows.append({
                "date": day.isoformat(),
                "label": day.strftime("%a"),
                "sales": _num(day_sales),
                "invoiceCount": sales_queryset.filter(invoice_date=day).count(),
            })

        checklist = [
            {
                "id": "collect",
                "label": "Collect pending receivables",
                "value": receivable,
                "count": unpaid_sales,
                "target": "parties",
                "status": "Attention" if receivable else "Clear",
            },
            {
                "id": "pay",
                "label": "Pay supplier dues",
                "value": payable,
                "count": unpaid_purchases,
                "target": "purchases",
                "status": "Attention" if payable else "Clear",
            },
            {
                "id": "stock",
                "label": "Review low stock",
                "value": 0,
                "count": low_stock_count,
                "target": "items",
                "status": "Attention" if low_stock_count else "Clear",
            },
            {
                "id": "expense",
                "label": "Review expenses",
                "value": _num(expense_total),
                "count": Expense.objects.filter(business=business).count(),
                "target": "expenses",
                "status": "Open",
            },
        ]

        sales_rows = {
            "quotation": [
                {
                    "id": str(row.id),
                    "date": _date(row.quotation_date),
                    "number": row.quotation_number,
                    "partyName": row.party.name.upper(),
                    "itemName": row.line_items.first().item_name if row.line_items.exists() else "Quoted Item",
                    "qty": _num(row.line_items.first().quantity) if row.line_items.exists() else 1,
                    "amount": _num(row.total_amount),
                    "settledAmount": 0,
                    "receivedAmount": 0,
                    "paymentMode": "-",
                    "linkedVoucher": row.converted_invoice.invoice_number if row.converted_invoice else "-",
                    "validTill": _date(row.valid_till),
                    "status": row.status.title(),
                    "notes": row.notes or "",
                }
                for row in Quotation.objects.filter(business=business)
                .select_related("party", "converted_invoice")
                .prefetch_related("line_items")
                .order_by("-quotation_date", "-created_at")
            ],
            "payment-in": [
                {
                    "id": str(row.id),
                    "date": _date(row.payment_date),
                    "number": row.payment_number,
                    "partyName": row.party.name.upper(),
                    "itemName": "Customer Payment Receipt",
                    "qty": 1,
                    "amount": _num(row.amount_received),
                    "settledAmount": 0 if row.status == "void" else _num(row.amount_received),
                    "receivedAmount": _num(row.amount_received),
                    "paymentMode": row.payment_mode.title(),
                    "linkedVoucher": row.reference_number or "-",
                    "validTill": "-",
                    "status": "Void" if row.status == "void" else "Received",
                    "notes": row.notes or "",
                }
                for row in PaymentIn.objects.filter(business=business).select_related("party").order_by("-payment_date", "-created_at")
            ],
            "sales-return": [
                {
                    "id": str(row.id),
                    "date": _date(row.return_date),
                    "number": row.return_number,
                    "partyName": row.party.name.upper(),
                    "itemName": row.line_items.first().item_name if row.line_items.exists() else "Returned Item",
                    "qty": _num(row.line_items.first().quantity) if row.line_items.exists() else 1,
                    "amount": _num(row.total_amount),
                    "settledAmount": 0,
                    "receivedAmount": 0,
                    "paymentMode": "-",
                    "linkedVoucher": row.original_invoice.invoice_number if row.original_invoice else "-",
                    "validTill": "-",
                    "status": row.status.title(),
                    "notes": row.reason or "",
                }
                for row in SalesReturn.objects.filter(business=business)
                .select_related("party", "original_invoice")
                .prefetch_related("line_items")
                .order_by("-return_date", "-created_at")
            ],
            "credit-note": [
                {
                    "id": str(row.id),
                    "date": _date(row.note_date),
                    "number": row.credit_note_number,
                    "partyName": row.party.name.upper(),
                    "itemName": "Credit Adjustment",
                    "qty": 1,
                    "amount": _num(row.total_amount),
                    "settledAmount": 0,
                    "receivedAmount": 0,
                    "paymentMode": "-",
                    "linkedVoucher": row.original_invoice.invoice_number if row.original_invoice else "-",
                    "validTill": "-",
                    "status": row.status.title(),
                    "notes": row.reason or "",
                }
                for row in CreditNote.objects.filter(business=business)
                .select_related("party", "original_invoice")
                .order_by("-note_date", "-created_at")
            ],
            "delivery-challan": [
                {
                    "id": str(row.id),
                    "date": _date(row.challan_date),
                    "number": row.challan_number,
                    "partyName": row.party.name.upper(),
                    "itemName": row.line_items.first().item_name if row.line_items.exists() else "Delivery Item",
                    "qty": _num(row.line_items.first().quantity) if row.line_items.exists() else 1,
                    "amount": _num(row.total_amount),
                    "settledAmount": 0,
                    "receivedAmount": 0,
                    "paymentMode": "-",
                    "linkedVoucher": row.converted_invoice.invoice_number if row.converted_invoice else "-",
                    "validTill": "-",
                    "status": row.status.title(),
                    "notes": row.notes or "",
                }
                for row in DeliveryChallan.objects.filter(business=business)
                .select_related("party", "converted_invoice")
                .prefetch_related("line_items")
                .order_by("-challan_date", "-created_at")
            ],
            "proforma-invoice": [
                {
                    "id": str(row.id),
                    "date": _date(row.proforma_date),
                    "number": row.proforma_number,
                    "partyName": row.party.name.upper(),
                    "itemName": row.line_items.first().item_name if row.line_items.exists() else "Proforma Item",
                    "qty": _num(row.line_items.first().quantity) if row.line_items.exists() else 1,
                    "amount": _num(row.total_amount),
                    "settledAmount": 0,
                    "receivedAmount": 0,
                    "paymentMode": "-",
                    "linkedVoucher": row.converted_invoice.invoice_number if row.converted_invoice else "-",
                    "validTill": _date(row.valid_till),
                    "status": row.status.title(),
                    "notes": "",
                }
                for row in ProformaInvoice.objects.filter(business=business)
                .select_related("party", "converted_invoice")
                .prefetch_related("line_items")
                .order_by("-proforma_date", "-created_at")
            ],
        }

        def _purchase_return_row(row):
            first_line = row.line_items.first()
            return {
                "id": str(row.id),
                "date": _date(row.return_date),
                "number": row.return_number,
                "partyName": row.party.name.upper(),
                "dueIn": "-",
                "itemName": first_line.item_name if first_line else "Purchase Return",
                "qty": _num(first_line.quantity) if first_line else 1,
                "amount": _num(row.total_amount),
                "paidAmount": 0,
                "settledAmount": 0,
                "paymentMode": "-",
                "linkedVoucher": row.original_invoice.invoice_number if row.original_invoice else row.reference_number or "-",
                "expectedDate": "-",
                "status": row.get_status_display(),
                "notes": row.reason or "",
            }

        debit_notes = list(
            DebitNote.objects.filter(business=business)
            .select_related("party", "original_invoice")
            .order_by("-note_date", "-created_at")
        )

        def _debit_row(note):
            return {
                "id": str(note.id),
                "date": _date(note.note_date),
                "number": note.debit_note_number,
                "partyName": note.party.name.upper(),
                "dueIn": "-",
                "itemName": "Debit Note",
                "qty": 1,
                "amount": _num(note.total_amount),
                "paidAmount": 0,
                "settledAmount": 0,
                "paymentMode": "-",
                "linkedVoucher": note.original_invoice.invoice_number if note.original_invoice else "-",
                "expectedDate": "-",
                "status": note.status.title(),
                "notes": note.reason or "",
            }

        debit_note_rows = [
            note for note in debit_notes if not (note.reason or "").lower().startswith("purchase return:")
        ]

        purchase_rows = {
            "purchases": [
                {
                    "id": str(invoice.id),
                    "date": _date(invoice.invoice_date),
                    "number": invoice.invoice_number,
                    "partyName": invoice.party.name.upper(),
                    "dueIn": "-",
                    "itemName": invoice.line_items.first().item_name if invoice.line_items.exists() else "Purchase Item",
                    "qty": _num(invoice.line_items.first().quantity) if invoice.line_items.exists() else 1,
                    "amount": _num(invoice.total_amount),
                    "paidAmount": _num(invoice.paid_amount),
                    "settledAmount": _num(invoice.paid_amount),
                    "paymentMode": "-",
                    "linkedVoucher": invoice.supplier_invoice_number or "-",
                    "expectedDate": "-",
                    "status": invoice.status.title(),
                    "notes": invoice.notes or "",
                }
                for invoice in PurchaseInvoice.objects.filter(business=business)
                .select_related("party")
                .prefetch_related("line_items")
                .order_by("-invoice_date", "-created_at")
            ],
            "payment-out": [
                {
                    "id": str(payment.id),
                    "date": _date(payment.payment_date),
                    "number": payment.payment_number,
                    "partyName": payment.party.name.upper(),
                    "dueIn": "-",
                    "itemName": "Supplier Payment Settlement",
                    "qty": 1,
                    "amount": _num(payment.amount_paid),
                    "paidAmount": _num(payment.amount_paid),
                    "settledAmount": 0 if payment.status == "void" else _num(payment.amount_paid),
                    "paymentMode": payment.payment_mode.title(),
                    "linkedVoucher": payment.reference_number or "-",
                    "expectedDate": "-",
                    "status": "Void" if payment.status == "void" else "Paid",
                    "notes": payment.notes or "",
                }
                for payment in PaymentOut.objects.filter(business=business).select_related("party").order_by("-payment_date", "-created_at")
            ],
            "purchase-return": [
                _purchase_return_row(row)
                for row in PurchaseReturn.objects.filter(business=business)
                .select_related("party", "original_invoice")
                .prefetch_related("line_items")
                .order_by("-return_date", "-created_at")
            ],
            "debit-note": [_debit_row(note) for note in debit_note_rows],
            "purchase-orders": [
                {
                    "id": str(order.id),
                    "date": _date(order.order_date),
                    "number": order.order_number,
                    "partyName": order.party.name.upper(),
                    "dueIn": "-",
                    "itemName": order.line_items.first().item_name if order.line_items.exists() else "Purchase Item",
                    "qty": _num(order.line_items.first().quantity) if order.line_items.exists() else 1,
                    "amount": _num(order.total_amount),
                    "paidAmount": 0,
                    "settledAmount": 0,
                    "paymentMode": "-",
                    "linkedVoucher": "-",
                    "expectedDate": "-",
                    "status": order.status.title(),
                    "notes": order.notes or "",
                }
                for order in PurchaseOrder.objects.filter(business=business)
                .select_related("party")
                .prefetch_related("line_items")
                .order_by("-order_date", "-created_at")
            ],
        }

        bank_accounts = [
            {
                "id": str(account.id),
                "name": account.account_name,
                "bankName": account.bank_name,
                "accountNumber": account.account_number,
                "balance": _num(account.current_balance),
            }
            for account in BankAccount.objects.filter(business=business, is_active=True).order_by("account_name")
        ]
        bank_transactions = [
            {
                "id": str(row.id),
                "date": _date(row.transaction_date),
                "accountId": str(row.bank_account_id),
                "accountName": row.bank_account.account_name,
                "type": row.transaction_type,
                "amount": _num(row.amount),
                "referenceNumber": row.reference_number or "-",
                "description": row.description or "",
            }
            for row in BankTransaction.objects.filter(business=business)
            .select_related("bank_account")
            .order_by("-transaction_date", "-created_at")[:50]
        ]
        expenses = [
            {
                "id": str(expense.id),
                "date": _date(expense.expense_date),
                "number": expense.expense_number,
                "category": expense.expense_category,
                "amount": _num(expense.total_amount),
                "paidAmount": _num(expense.paid_amount),
                "paymentMode": expense.payment_mode.title(),
                "notes": expense.notes or "",
            }
            for expense in Expense.objects.filter(business=business).order_by("-expense_date", "-created_at")
        ]
        automated_bills = [
            {
                "id": str(bill.id),
                "name": bill.bill_name,
                "amount": _num(bill.amount),
                "frequency": bill.frequency,
                "nextDueDate": bill.next_due_date.isoformat(),
                "isActive": bill.is_active,
            }
            for bill in AutomatedBill.objects.filter(business=business).order_by("next_due_date")
        ]
        online_orders = [
            {
                "id": str(order.id),
                "orderNumber": order.order_number,
                "orderDate": _date(order.order_date),
                "customerName": order.customer_name,
                "customerMobile": order.customer_mobile or "-",
                "partyId": str(order.party_id) if order.party_id else "",
                "itemId": str(order.item_id),
                "itemName": order.item.name,
                "itemCode": order.item.item_code or order.item.barcode or "",
                "quantity": _num(order.quantity),
                "unitPrice": _num(order.unit_price),
                "taxableAmount": _num(order.taxable_amount),
                "taxAmount": _num(order.tax_amount),
                "totalAmount": _num(order.total_amount),
                "paymentStatus": order.payment_status,
                "dispatchStatus": order.dispatch_status,
                "source": order.source,
                "stockDeducted": order.stock_deducted,
                "deliveryAddress": order.delivery_address or "",
                "notes": order.notes or "",
                "currentStock": _num(order.item.current_stock),
            }
            for order in OnlineOrder.objects.filter(business=business)
            .select_related("party", "item")
            .order_by("-order_date", "-created_at")
        ]
        sms_credit_total = SMSCreditLedger.objects.filter(business=business, entry_type="credit").aggregate(total=Sum("credits"))["total"] or 0
        sms_debit_total = SMSCreditLedger.objects.filter(business=business, entry_type="debit").aggregate(total=Sum("credits"))["total"] or 0
        sms_templates = [
            {
                "id": str(template.id),
                "name": template.name,
                "category": template.category,
                "message": template.message,
                "isActive": template.is_active,
            }
            for template in SMSTemplate.objects.filter(business=business, is_active=True).order_by("category", "name")
        ]
        sms_campaigns = [
            {
                "id": str(campaign.id),
                "campaignNumber": campaign.campaign_number,
                "name": campaign.name,
                "templateId": str(campaign.template_id) if campaign.template_id else "",
                "templateName": campaign.template.name if campaign.template else "-",
                "audience": campaign.audience,
                "message": campaign.message,
                "recipientCount": campaign.recipient_count,
                "deliveredCount": campaign.delivered_count,
                "failedCount": campaign.failed_count,
                "creditCost": campaign.credit_cost,
                "status": campaign.status,
                "queuedAt": _date(campaign.queued_at),
                "completedAt": _date(campaign.completed_at),
                "createdAt": _date(campaign.created_at),
            }
            for campaign in SMSCampaign.objects.filter(business=business)
            .select_related("template")
            .order_by("-created_at")
        ]
        invoice_settings, created = InvoiceSettings.objects.get_or_create(
            business=business,
            defaults={
                "theme": "advanced_gst",
                "theme_color": "#5B48F5",
                "paper_size": "A4",
                "thermal_paper_size": "2inch",
                "invoice_prefix": business.invoice_prefix or "INV",
            }
        )
        business_preferences, created = BusinessPreference.objects.get_or_create(
            business=business,
            defaults={
                "enable_gst_billing": bool(business.gstin),
                "show_upi_on_invoice": bool(business.upi_id),
            }
        )
        reminder_preferences, created = ReminderPreference.objects.get_or_create(business=business)
        account_user = request.user
        module_permissions = role_permissions_for(account_user.role)
        can_view = lambda module: module_permissions.get(module, {}).get("view", False)
        def _can_view_transaction(row):
            transaction_type = row["type"]
            if transaction_type.startswith("Sales"):
                return can_view("sales")
            if transaction_type.startswith("Purchase"):
                return can_view("purchases")
            if transaction_type.startswith("Payment"):
                return can_view("payments")
            if transaction_type == "Expenses":
                return can_view("accounting")
            return False

        visible_transactions = [row for row in transactions[:10] if _can_view_transaction(row)]
        dashboard_stats = {
            "totalSales": _num(sales_total) if can_view("sales") else 0,
            "totalPurchases": _num(purchase_total) if can_view("purchases") else 0,
            "receivable": receivable if can_view("parties") or can_view("payments") else 0,
            "payable": payable if can_view("parties") or can_view("purchases") else 0,
            "inventoryVal": _num(inventory_value) if can_view("items") or can_view("stock") else 0,
            "bankBalance": _num(bank_balance) if can_view("accounting") else 0,
            "expenseTotal": _num(expense_total) if can_view("accounting") else 0,
        }
        dashboard_checklist = []
        if can_view("parties") or can_view("payments"):
            dashboard_checklist.append(checklist[0])
        if can_view("parties") or can_view("purchases"):
            dashboard_checklist.append(checklist[1])
        if can_view("items") or can_view("stock"):
            dashboard_checklist.append(checklist[2])
        if can_view("accounting"):
            dashboard_checklist.append(checklist[3])
        empty_sales_rows = {key: [] for key in sales_rows}
        empty_purchase_rows = {key: [] for key in purchase_rows}
        empty_accounting = {"bankAccounts": [], "bankTransactions": [], "expenses": [], "automatedBills": []}
        empty_business_tools = {
            "onlineOrders": [],
            "smsMarketing": {"creditBalance": 0, "templates": [], "campaigns": []},
        }

        return Response({
            "success": True,
            "tenant": {"businessId": str(business.id), "userId": str(request.user.id)},
            "business": _business_payload(business),
            "providerStatus": _external_provider_payload(),
            "modulePermissions": module_permissions,
            "parties": parties if can_view("parties") else [],
            "items": items if can_view("items") else [],
            "godowns": godowns if can_view("stock") or can_view("items") else [],
            "staff": staff if can_view("staff") else [],
            "invoices": invoices if can_view("sales") else [],
            "transactions": visible_transactions,
            "dashboard": {
                "lastUpdated": timezone.localtime().strftime("%d %b %Y | %I:%M %p"),
                "stats": dashboard_stats,
                "salesTrend": trend_rows if can_view("sales") else [],
                "checklist": dashboard_checklist,
            },
            "salesRows": sales_rows if can_view("sales") else empty_sales_rows,
            "purchaseRows": purchase_rows if can_view("purchases") else empty_purchase_rows,
            "accounting": {
                "bankAccounts": bank_accounts,
                "bankTransactions": bank_transactions,
                "expenses": expenses,
                "automatedBills": automated_bills,
            } if can_view("accounting") else empty_accounting,
            "businessTools": {
                "onlineOrders": online_orders,
                "smsMarketing": {
                    "creditBalance": int(sms_credit_total) - int(sms_debit_total),
                    "templates": sms_templates,
                    "campaigns": sms_campaigns,
                },
            } if can_view("business_tools") else empty_business_tools,
            "settings": {
                "account": {
                    "id": str(account_user.id),
                    "name": account_user.name,
                    "firstName": account_user.first_name or "",
                    "lastName": account_user.last_name or "",
                    "mobile": account_user.mobile,
                    "email": account_user.email or "",
                    "role": account_user.role,
                },
                "businessProfile": {
                    "id": str(business.id),
                    "name": business.name,
                    "phone": business.phone or "",
                    "gstin": business.gstin or "",
                    "category": business_preferences.business_category,
                    "state": business.state or "",
                    "address": business.address or "",
                    "city": business.city or "",
                    "pincode": business.pincode or "",
                    "email": business.email or "",
                    "upiId": business.upi_id or "",
                    "bankAccountDetails": business.bank_account_details or {},
                    "showInOnlineStore": business_preferences.show_in_online_store,
                    "enableGstBilling": business_preferences.enable_gst_billing,
                    "showLogoOnInvoice": business_preferences.show_logo_on_invoice,
                    "branchBilling": business_preferences.branch_billing,
                    "showUpiOnInvoice": business_preferences.show_upi_on_invoice,
                    "printPreview": business_preferences.print_preview,
                    "hideZeroStockBarcodes": business_preferences.hide_zero_stock_barcodes,
                    "printOriginalDuplicate": business_preferences.print_original_duplicate,
                    "autoPrintAfterSale": business_preferences.auto_print_after_sale,
                    "caReportsEnabled": business_preferences.ca_reports_enabled,
                    "caName": business_preferences.ca_name,
                    "caEmail": business_preferences.ca_email,
                    "caMobile": business_preferences.ca_mobile,
                    "planName": business_preferences.plan_name,
                    "planValidTill": business_preferences.plan_valid_till.isoformat() if business_preferences.plan_valid_till else "",
                    "referralCode": business_preferences.referral_code,
                    "supportEmail": business_preferences.support_email,
                    "supportPhone": business_preferences.support_phone,
                },
                "invoice": {
                    "id": str(invoice_settings.id),
                    "theme": invoice_settings.theme,
                    "themeColor": invoice_settings.theme_color,
                    "themeStyle": invoice_settings.theme_style or "",
                    "showMrp": invoice_settings.show_mrp,
                    "showHsn": invoice_settings.show_hsn,
                    "showDiscount": invoice_settings.show_discount,
                    "showColor": invoice_settings.show_color,
                    "showCinDate": invoice_settings.show_cin_date,
                    "showGrnDate": invoice_settings.show_grn_date,
                    "showFreeQty": invoice_settings.show_free_qty,
                    "showPartyBalance": invoice_settings.show_party_balance,
                    "showItemDescription": invoice_settings.show_item_description,
                    "showTimeOnInvoice": invoice_settings.show_time_on_invoice,
                    "showDiscountOnMrp": invoice_settings.show_discount_on_mrp,
                    "paperSize": invoice_settings.paper_size,
                    "thermalPaperSize": invoice_settings.thermal_paper_size,
                    "thermalTheme": invoice_settings.thermal_theme,
                    "logoUrl": invoice_settings.logo_url or "",
                    "signatureUrl": invoice_settings.signature_url or "",
                    "customFields": invoice_settings.custom_fields,
                    "invoicePrefix": invoice_settings.invoice_prefix,
                    "resetEachYear": invoice_settings.reset_each_year,
                },
                "reminders": {
                    "paymentDue": reminder_preferences.payment_due,
                    "saleInvoice": reminder_preferences.sale_invoice,
                    "lowStock": reminder_preferences.low_stock,
                    "customerOccasions": reminder_preferences.customer_occasions,
                    "dailySummary": reminder_preferences.daily_summary,
                },
            },
            "users": users if can_view("users") else [],
            "counts": {
                "parties": Party.objects.filter(business=business, is_active=True).count() if can_view("parties") else 0,
                "items": Item.objects.filter(business=business, is_active=True).count() if can_view("items") else 0,
                "salesInvoices": SalesInvoice.objects.filter(business=business).count() if can_view("sales") else 0,
                "purchaseInvoices": PurchaseInvoice.objects.filter(business=business).count() if can_view("purchases") else 0,
                "paymentsIn": PaymentIn.objects.filter(business=business).count() if can_view("payments") else 0,
                "paymentsOut": PaymentOut.objects.filter(business=business).count() if can_view("payments") else 0,
            }
        })


class ActivityFeedView(views.APIView):
    permission_classes = [permissions.IsAuthenticated, RoleModulePermission]
    module_key = "audit"

    def get(self, request):
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active tenant business"},
                status=status.HTTP_404_NOT_FOUND
            )

        try:
            limit = min(max(int(request.query_params.get("limit", 40)), 1), 100)
        except (TypeError, ValueError):
            limit = 40

        logs = ActivityLog.objects.filter(business=business).select_related("user").order_by("-created_at")[:limit]
        return Response({
            "success": True,
            "activities": _activity_feed(business, limit),
            "activityLogs": ActivityLogSerializer(logs, many=True).data,
            "modulePermissions": role_permissions_for(request.user.role),
        })


class BusinessViewSet(viewsets.ModelViewSet):
    serializer_class = BusinessSerializer
    permission_classes = [permissions.IsAuthenticated, RoleModulePermission]
    module_key = "business"

    def get_queryset(self):
        # Admin can view all, but regular users only view their own
        if self.request.user.role == "admin" and self.request.business:
            return Business.objects.filter(id=self.request.business.id)
        return Business.objects.none()

    @action(detail=False, methods=["get", "put", "patch"])
    def my_business(self, request):
        """Get or update current business profile."""
        business = request.business
        if not business:
            # Let's create an empty one or return 404
            return Response(
                {"success": False, "message": "No active business associated"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        if request.method in ["PUT", "PATCH"]:
            serializer = self.get_serializer(business, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response({"success": True, "data": serializer.data})
            
        serializer = self.get_serializer(business)
        return Response({"success": True, "data": serializer.data})

    @action(detail=False, methods=["post"])
    def register(self, request):
        """Register a new business and link to creator user."""
        if request.user.business:
            return Response(
                {"success": False, "message": "User already associated with a business"},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        business = serializer.save()
        
        # Link user to new business
        user = request.user
        user.business = business
        user.save()
        
        # Log activity
        ActivityLog.objects.create(
            business=business,
            user=user,
            action="registered_business",
            entity_type="business",
            entity_id=business.id,
            details={"business_name": business.name}
        )
        
        return Response({
            "success": True,
            "message": "Business registered successfully",
            "data": serializer.data,
            "user": UserSerializer(user).data
        })

class UserManagementViewSet(viewsets.ModelViewSet):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated, RoleModulePermission]
    module_key = "users"

    def get_queryset(self):
        if not self.request.business:
            return User.objects.none()
        # Filter users inside active business tenant
        return User.objects.filter(business=self.request.business)

    def get_serializer_class(self):
        if self.action == "create":
            return AddUserSerializer
        return UserSerializer

    def create(self, request, *args, **kwargs):
        if not request.business:
            return Response(
                {"success": False, "message": "No active tenant business"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        user = serializer.save()
        ActivityLog.objects.create(
            business=self.request.business,
            user=self.request.user,
            action="added_user",
            entity_type="user",
            entity_id=user.id,
            details={"added_user_mobile": user.mobile, "role": user.role}
        )

    def perform_update(self, serializer):
        serializer.save(business=self.request.business)

    def destroy(self, request, *args, **kwargs):
        """Soft delete user as shown in Image 22."""
        if not request.business:
            return Response(
                {"success": False, "message": "No active tenant business"},
                status=status.HTTP_404_NOT_FOUND,
            )
        user = self.get_object()
        user.is_active = False
        # To show as deleted in front-end
        user.first_name = f"{user.first_name} (Deleted)"
        user.save()
        
        ActivityLog.objects.create(
            business=request.business,
            user=request.user,
            action="deleted_user",
            entity_type="user",
            entity_id=user.id,
            details={"deleted_user_mobile": user.mobile}
        )
        return Response({"success": True, "message": "User soft deleted successfully"})
