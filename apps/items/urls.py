from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ItemCategoryViewSet, GodownViewSet, ItemGodownStockViewSet,
    ItemViewSet, StockMovementViewSet, GodownTransferViewSet,
    ItemPartyPriceViewSet, BarcodeLabelViewSet
)

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"categories", ItemCategoryViewSet, basename="item_categories")
router.register(r"godowns", GodownViewSet, basename="godowns")
router.register(r"godown-stocks", ItemGodownStockViewSet, basename="godown_stocks")
router.register(r"items", ItemViewSet, basename="items")
router.register(r"party-prices", ItemPartyPriceViewSet, basename="item_party_prices")
router.register(r"movements", StockMovementViewSet, basename="stock_movements")
router.register(r"transfers", GodownTransferViewSet, basename="godown_transfers")
router.register(r"barcode-labels", BarcodeLabelViewSet, basename="barcode_labels")

urlpatterns = [
    path("", include(router.urls)),
]
