from rest_framework import serializers
from .models import BusinessNotification, BusinessPreference, InvoiceSettings, Reminder, ReminderPreference

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
