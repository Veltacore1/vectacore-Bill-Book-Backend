from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import ActivityLog, Business, User
from apps.staff.models import Attendance, Payroll, Staff


class StaffPayrollLifecycleTests(APITestCase):
    def auth_as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def setUp(self):
        self.business = Business.objects.create(name="CSM SILKS", phone="8608633066")
        self.other_business = Business.objects.create(name="Other Textile", phone="9000000000")
        self.user = User.objects.create_user(
            mobile="8608633066",
            business=self.business,
            role="admin",
            first_name="CSM",
            is_active=True,
        )
        self.staff = Staff.objects.create(
            business=self.business,
            name="Sales Counter",
            designation="Salesman",
            monthly_salary=Decimal("31000.00"),
        )
        self.other_staff = Staff.objects.create(
            business=self.other_business,
            name="Other Staff",
            designation="Salesman",
            monthly_salary=Decimal("50000.00"),
        )
        Attendance.objects.create(business=self.business, staff=self.staff, date="2026-05-01", status="present")
        Attendance.objects.create(business=self.business, staff=self.staff, date="2026-05-02", status="absent")
        Attendance.objects.create(business=self.business, staff=self.staff, date="2026-05-03", status="half_day")

    def test_monthly_report_projects_payroll_from_attendance_before_generation(self):
        self.auth_as(self.user)
        response = self.client.get("/api/v1/staff/payroll/monthly_report/?month=5&year=2026")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        report = response.data["data"]
        self.assertEqual(report["summary"]["totalStaff"], 1)
        self.assertEqual(report["summary"]["generatedCount"], 0)
        row = report["rows"][0]
        self.assertEqual(row["staffName"], "Sales Counter")
        self.assertEqual(row["status"], "not_generated")
        self.assertEqual(row["attendance"]["presentDays"], 1)
        self.assertEqual(row["attendance"]["absentDays"], 1)
        self.assertEqual(row["attendance"]["halfDays"], 1)
        self.assertEqual(row["deductions"], 1500.0)
        self.assertEqual(row["netSalary"], 29500.0)

    def test_generate_and_mark_paid_keep_payroll_tenant_scoped(self):
        self.auth_as(self.user)
        generate_response = self.client.post("/api/v1/staff/payroll/generate_monthly/", {
            "month": 5,
            "year": 2026,
        }, format="json")
        self.assertEqual(generate_response.status_code, status.HTTP_200_OK)
        self.assertEqual(generate_response.data["data"]["summary"]["generatedCount"], 1)
        payroll = Payroll.objects.get(business=self.business, staff=self.staff, month=5, year=2026)
        self.assertEqual(payroll.net_salary, Decimal("29500.00"))
        self.assertEqual(Payroll.objects.filter(business=self.other_business).count(), 0)

        paid_response = self.client.post(f"/api/v1/staff/payroll/{payroll.id}/mark_paid/", {
            "payment_date": "2026-05-25",
            "notes": "Paid by cash",
        }, format="json")
        self.assertEqual(paid_response.status_code, status.HTTP_200_OK)
        payroll.refresh_from_db()
        self.assertEqual(payroll.status, "paid")
        self.assertEqual(payroll.payment_date.isoformat(), "2026-05-25")
        self.assertTrue(
            ActivityLog.objects.filter(
                business=self.business,
                action="payroll_paid",
                entity_type="payroll",
                entity_id=payroll.id,
            ).exists()
        )

        report_response = self.client.get("/api/v1/staff/payroll/monthly_report/?month=5&year=2026")
        self.assertEqual(report_response.data["data"]["summary"]["paidCount"], 1)
        self.assertEqual(report_response.data["data"]["summary"]["paidAmount"], 29500.0)

    def test_attendance_bulk_mark_rejects_cross_tenant_staff(self):
        self.auth_as(self.user)
        response = self.client.post("/api/v1/staff/attendance/bulk_mark/", {
            "date": "2026-05-04",
            "records": [{"staff_id": str(self.other_staff.id), "status": "present"}],
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("invalidStaffIds", response.data)
        self.assertFalse(
            Attendance.objects.filter(business=self.business, staff=self.other_staff, date="2026-05-04").exists()
        )

    def test_staff_salary_must_be_positive(self):
        self.auth_as(self.user)
        response = self.client.post("/api/v1/staff/directory/", {
            "name": "Zero Salary",
            "designation": "Salesman",
            "monthly_salary": "0.00",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Staff.objects.filter(business=self.business, name="Zero Salary").exists())
