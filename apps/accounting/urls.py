from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    BankAccountViewSet,
    BankTransactionViewSet,
    ExpenseViewSet,
    AutomatedBillViewSet,
    ReportExportView,
    ReportShareView,
    ReportsView,
    SharedReportView,
)

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"accounts", BankAccountViewSet, basename="bank_accounts")
router.register(r"transactions", BankTransactionViewSet, basename="bank_transactions")
router.register(r"expenses", ExpenseViewSet, basename="expenses")
router.register(r"recurring-bills", AutomatedBillViewSet, basename="automated_bills")

urlpatterns = [
    path("reports/shared/<str:share_token>/", SharedReportView.as_view(), name="tenant_shared_report"),
    path("reports/export/", ReportExportView.as_view(), name="tenant_report_export"),
    path("reports/share/", ReportShareView.as_view(), name="tenant_report_share"),
    path("reports/", ReportsView.as_view(), name="tenant_reports"),
    path("", include(router.urls)),
]
