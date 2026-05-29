from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    BusinessNotificationViewSet,
    BusinessPreferenceViewSet,
    InvoiceSettingsViewSet,
    ReminderPreferenceViewSet,
    ReminderViewSet,
)

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"invoice-layout", InvoiceSettingsViewSet, basename="invoice_layout")
router.register(r"business-preferences", BusinessPreferenceViewSet, basename="business_preferences")
router.register(r"reminder-preferences", ReminderPreferenceViewSet, basename="reminder_preferences")
router.register(r"reminders", ReminderViewSet, basename="reminders")
router.register(r"notifications", BusinessNotificationViewSet, basename="notifications")

urlpatterns = [
    path("", include(router.urls)),
]
