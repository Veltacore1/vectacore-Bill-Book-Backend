from rest_framework import serializers
from .models import Staff, Attendance, Payroll

class StaffSerializer(serializers.ModelSerializer):
    class Meta:
        model = Staff
        fields = [
            "id", "name", "phone", "email", "designation", "monthly_salary",
            "joining_date", "bank_details", "is_active", "created_at"
        ]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["business"] = self.context["request"].business
        return Staff.objects.create(**validated_data)

    def validate_monthly_salary(self, value):
        if value <= 0:
            raise serializers.ValidationError("Monthly salary must be greater than zero.")
        return value

class AttendanceSerializer(serializers.ModelSerializer):
    staff_name = serializers.CharField(source="staff.name", read_only=True)

    class Meta:
        model = Attendance
        fields = "__all__"
        read_only_fields = ["id", "business", "created_at"]

    def create(self, validated_data):
        validated_data["business"] = self.context["request"].business
        return Attendance.objects.create(**validated_data)

    def validate_staff(self, value):
        business = self.context["request"].business
        if not business or value.business_id != business.id:
            raise serializers.ValidationError("Staff member is not available for this tenant.")
        return value

class PayrollSerializer(serializers.ModelSerializer):
    staff_name = serializers.CharField(source="staff.name", read_only=True)

    class Meta:
        model = Payroll
        fields = "__all__"
        read_only_fields = ["id", "business", "created_at"]

    def create(self, validated_data):
        validated_data["business"] = self.context["request"].business
        # Default calculation: net = basic - deductions + allowances
        basic = validated_data.get("basic_salary", 0.0)
        deductions = validated_data.get("deductions", 0.0)
        allowances = validated_data.get("allowances", 0.0)
        validated_data["net_salary"] = basic - deductions + allowances
        return Payroll.objects.create(**validated_data)

    def validate_staff(self, value):
        business = self.context["request"].business
        if not business or value.business_id != business.id:
            raise serializers.ValidationError("Staff member is not available for this tenant.")
        return value
