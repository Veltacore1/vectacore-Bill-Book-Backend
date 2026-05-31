from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PaymentGatewayOrderViewSet, PaymentInViewSet, PaymentOutViewSet, RazorpayWebhookView

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"payment-in", PaymentInViewSet, basename="payment_in")
router.register(r"payment-out", PaymentOutViewSet, basename="payment_out")
router.register(r"gateway/orders", PaymentGatewayOrderViewSet, basename="payment_gateway_orders")

urlpatterns = [
    path("webhooks/razorpay/", RazorpayWebhookView.as_view(), name="razorpay_webhook"),
    path("", include(router.urls)),
]
