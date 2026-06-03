from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    BusinessNotificationViewSet,
    BusinessPreferenceViewSet,
    InvoiceSettingsViewSet,
    ReferralInviteViewSet,
    ReminderPreferenceViewSet,
    ReminderViewSet,
    SupportTicketViewSet,
)

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"invoice-layout", InvoiceSettingsViewSet, basename="invoice_layout")
router.register(r"business-preferences", BusinessPreferenceViewSet, basename="business_preferences")
router.register(r"reminder-preferences", ReminderPreferenceViewSet, basename="reminder_preferences")
router.register(r"reminders", ReminderViewSet, basename="reminders")
router.register(r"notifications", BusinessNotificationViewSet, basename="notifications")
router.register(r"referral-invites", ReferralInviteViewSet, basename="referral_invites")
router.register(r"support-tickets", SupportTicketViewSet, basename="support_tickets")

urlpatterns = [
    path("", include(router.urls)),
]
