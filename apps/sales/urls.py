from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    SalesInvoiceViewSet, QuotationViewSet, DeliveryChallanViewSet,
    CreditNoteViewSet, ProformaInvoiceViewSet, SalesReturnViewSet
)

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"invoices", SalesInvoiceViewSet, basename="sales_invoices")
router.register(r"quotations", QuotationViewSet, basename="quotations")
router.register(r"challans", DeliveryChallanViewSet, basename="delivery_challans")
router.register(r"credit-notes", CreditNoteViewSet, basename="credit_notes")
router.register(r"sales-returns", SalesReturnViewSet, basename="sales_returns")
router.register(r"proforma-invoices", ProformaInvoiceViewSet, basename="proforma_invoices")

urlpatterns = [
    path("", include(router.urls)),
]
