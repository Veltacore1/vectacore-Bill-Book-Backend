from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PartyCategoryViewSet, PartyViewSet, SharedLedgerPortalView

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"categories", PartyCategoryViewSet, basename="party_categories")
router.register(r"parties", PartyViewSet, basename="parties")

urlpatterns = [
    path("shared-ledger/<str:token>/", SharedLedgerPortalView.as_view(), name="shared_ledger_portal"),
    path("", include(router.urls)),
]
