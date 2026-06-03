import re

from rest_framework import serializers
from .models import Business, User, ActivityLog


def normalize_mobile(value):
    cleaned = (value or "").strip().replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\+?\d{10,15}", cleaned):
        raise serializers.ValidationError("Enter a valid mobile number.")
    return cleaned

class BusinessSerializer(serializers.ModelSerializer):
    class Meta:
        model = Business
        fields = "__all__"

class UserSerializer(serializers.ModelSerializer):
    business_details = BusinessSerializer(source="business", read_only=True)

    class Meta:
        model = User
        fields = [
            "id", "business", "business_details", "username", "mobile", 
            "first_name", "last_name", "email", "role", "is_active", "last_login_at",
            "date_joined"
        ]
        read_only_fields = ["id", "username", "date_joined", "last_login_at"]

class SendOTPSerializer(serializers.Serializer):
    mobile = serializers.CharField(max_length=20)

    def validate_mobile(self, value):
        return normalize_mobile(value)

class VerifyOTPSerializer(serializers.Serializer):
    mobile = serializers.CharField(max_length=20)
    otp = serializers.CharField(max_length=6)

    def validate_mobile(self, value):
        return normalize_mobile(value)

    def validate_otp(self, value):
        cleaned = (value or "").strip()
        if not re.fullmatch(r"\d{6}", cleaned):
            raise serializers.ValidationError("Enter the 6 digit OTP.")
        return cleaned

class AddUserSerializer(serializers.ModelSerializer):
    mobile = serializers.CharField(max_length=20)

    class Meta:
        model = User
        fields = ["first_name", "mobile", "role"]

    def validate_first_name(self, value):
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError("User name is required.")
        return cleaned

    def validate_mobile(self, value):
        mobile = normalize_mobile(value)
        business = self.context["request"].business
        existing = User.objects.filter(mobile=mobile).select_related("business").first()
        if existing:
            if existing.business_id == getattr(business, "id", None) and not existing.is_active:
                return mobile
            raise serializers.ValidationError("This mobile number is already linked to a user.")
        return mobile

    def create(self, validated_data):
        business = self.context["request"].business
        mobile = validated_data["mobile"]
        role = validated_data["role"]
        first_name = validated_data["first_name"]

        existing = User.objects.filter(business=business, mobile=mobile, is_active=False).first()
        if existing:
            existing.first_name = first_name
            existing.role = role
            existing.is_active = True
            existing.save(update_fields=["first_name", "role", "is_active"])
            return existing
        
        # Create user linked to active business
        user = User.objects.create_user(
            mobile=mobile,
            first_name=first_name,
            role=role,
            business=business
        )
        return user


class TextileTenantRegistrationSerializer(serializers.Serializer):
    business_name = serializers.CharField(max_length=255)
    owner_name = serializers.CharField(max_length=150)
    mobile = serializers.CharField(max_length=20)
    email = serializers.EmailField(required=False, allow_blank=True)
    gstin = serializers.CharField(max_length=20, required=False, allow_blank=True)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True, default="Tamil Nadu")
    city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    pincode = serializers.CharField(max_length=10, required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    invoice_prefix = serializers.CharField(max_length=20, required=False, allow_blank=True)
    password = serializers.CharField(required=False, allow_blank=True, write_only=True)

    def validate_business_name(self, value):
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Business name is required.")
        return cleaned

    def validate_owner_name(self, value):
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Owner name is required.")
        return cleaned

    def validate_mobile(self, value):
        value = normalize_mobile(value)
        existing = User.objects.filter(mobile=value).select_related("business").first()
        if existing and existing.business_id:
            raise serializers.ValidationError("This mobile number is already linked to a tenant.")
        return value

    def validate_gstin(self, value):
        gstin = (value or "").strip().upper()
        if gstin and not re.fullmatch(r"[0-9A-Z]{15}", gstin):
            raise serializers.ValidationError("GSTIN must be a 15 character alphanumeric value.")
        return gstin

    def validate_invoice_prefix(self, value):
        prefix = (value or "INV").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,8}", prefix):
            raise serializers.ValidationError("Invoice prefix must be 2-8 letters or numbers.")
        return prefix


class ActivityLogSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.name", read_only=True)
    business_name = serializers.CharField(source="business.name", read_only=True)

    class Meta:
        model = ActivityLog
        fields = [
            "id", "business", "business_name", "user", "user_name", "action",
            "entity_type", "entity_id", "details", "created_at",
        ]
        read_only_fields = fields
