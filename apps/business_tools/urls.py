from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SMSCampaignViewSet, SMSCreditLedgerViewSet, SMSTemplateViewSet, OnlineOrderViewSet

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"online-orders", OnlineOrderViewSet, basename="online_orders")
router.register(r"sms-templates", SMSTemplateViewSet, basename="sms_templates")
router.register(r"sms-campaigns", SMSCampaignViewSet, basename="sms_campaigns")
router.register(r"sms-credits", SMSCreditLedgerViewSet, basename="sms_credits")

urlpatterns = [
    path("", include(router.urls)),
]
