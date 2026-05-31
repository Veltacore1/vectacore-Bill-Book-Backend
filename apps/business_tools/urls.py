from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    MessagingWebhookView,
    OnlineOrderViewSet,
    SMSCampaignViewSet,
    SMSCreditLedgerViewSet,
    SMSTemplateViewSet,
)

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"online-orders", OnlineOrderViewSet, basename="online_orders")
router.register(r"sms-templates", SMSTemplateViewSet, basename="sms_templates")
router.register(r"sms-campaigns", SMSCampaignViewSet, basename="sms_campaigns")
router.register(r"sms-credits", SMSCreditLedgerViewSet, basename="sms_credits")

urlpatterns = [
    path("webhooks/messaging/<slug:provider>/", MessagingWebhookView.as_view(), name="messaging_webhook"),
    path("", include(router.urls)),
]
