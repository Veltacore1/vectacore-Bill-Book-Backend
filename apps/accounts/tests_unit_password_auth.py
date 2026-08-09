"""Unit tests for password-based auth serializers."""

from django.test import TestCase

from apps.accounts.serializers import PasswordLoginSerializer, TextileTenantRegistrationSerializer


class PasswordAuthSerializerTests(TestCase):
    def test_registration_requires_password_min_length(self):
        serializer = TextileTenantRegistrationSerializer(data={
            "business_name": "Unit Textile",
            "owner_name": "Unit Owner",
            "mobile": "9000000301",
            "password": "12345",
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("password", serializer.errors)

    def test_registration_accepts_valid_payload(self):
        serializer = TextileTenantRegistrationSerializer(data={
            "business_name": "Unit Textile",
            "owner_name": "Unit Owner",
            "mobile": "9000000302",
            "password": "secret12",
            "invoice_prefix": "UNT",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_password_login_requires_mobile_and_password(self):
        serializer = PasswordLoginSerializer(data={"mobile": "9000000303", "password": ""})
        self.assertFalse(serializer.is_valid())
        self.assertIn("password", serializer.errors)

    def test_password_login_normalizes_mobile(self):
        serializer = PasswordLoginSerializer(data={
            "mobile": " 90000 00304 ",
            "password": "secret12",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["mobile"], "9000000304")
