from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PaymentInViewSet, PaymentOutViewSet

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"payment-in", PaymentInViewSet, basename="payment_in")
router.register(r"payment-out", PaymentOutViewSet, basename="payment_out")

urlpatterns = [
    path("", include(router.urls)),
]
