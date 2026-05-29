from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PurchaseInvoiceViewSet, PurchaseOrderViewSet, DebitNoteViewSet

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"invoices", PurchaseInvoiceViewSet, basename="purchase_invoices")
router.register(r"orders", PurchaseOrderViewSet, basename="purchase_orders")
router.register(r"debit-notes", DebitNoteViewSet, basename="debit_notes")

urlpatterns = [
    path("", include(router.urls)),
]
