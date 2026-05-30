from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PurchaseInvoiceViewSet, PurchaseOrderViewSet, PurchaseReturnViewSet, DebitNoteViewSet

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"invoices", PurchaseInvoiceViewSet, basename="purchase_invoices")
router.register(r"orders", PurchaseOrderViewSet, basename="purchase_orders")
router.register(r"returns", PurchaseReturnViewSet, basename="purchase_returns")
router.register(r"debit-notes", DebitNoteViewSet, basename="debit_notes")

urlpatterns = [
    path("", include(router.urls)),
]
