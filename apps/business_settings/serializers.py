from rest_framework import serializers
from .models import (
    BusinessNotification,
    BusinessPreference,
    InvoiceSettings,
    ReferralInvite,
    Reminder,
    ReminderPreference,
    SupportTicket,
)

class InvoiceSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceSettings
        exclude = ["business"]

class BusinessPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessPreference
        exclude = ["business"]

class ReminderPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReminderPreference
        exclude = ["business"]

class ReminderSerializer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True)

    class Meta:
        model = Reminder
        fields = "__all__"
        read_only_fields = [
            "id", "business", "sent_at", "attempt_count", "last_attempt_at",
            "delivery_provider", "provider_message_id", "provider_response",
            "delivery_message", "created_at",
        ]

    def validate_party(self, value):
        business = self.context["request"].business
        if value and business and value.business_id != business.id:
            raise serializers.ValidationError("Choose a party from the active tenant.")
        return value


class BusinessNotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessNotification
        exclude = ["business"]
        read_only_fields = [
            "id", "source_type", "source_id", "title", "message", "priority",
            "target", "status", "metadata", "created_at", "updated_at", "read_at",
        ]


class ReferralInviteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReferralInvite
        exclude = ["business"]
        read_only_fields = [
            "id", "referral_code", "status", "reward_label", "created_by",
            "activated_at", "created_at", "updated_at",
        ]

    def validate_mobile(self, value):
        normalized = "".join(char for char in (value or "") if char.isdigit())
        if len(normalized) < 10:
            raise serializers.ValidationError("Enter a valid mobile number.")
        return normalized[-10:]

    def validate_business_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Business name is required.")
        return value


class SupportTicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportTicket
        exclude = ["business"]
        read_only_fields = [
            "id", "ticket_number", "status", "created_by", "resolved_at",
            "created_at", "updated_at",
        ]

    def validate_subject(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Subject is required.")
        return value

    def validate_message(self, value):
        value = (value or "").strip()
        if len(value) < 10:
            raise serializers.ValidationError("Describe the issue in at least 10 characters.")
        return value
