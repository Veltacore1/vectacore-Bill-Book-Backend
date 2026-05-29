from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Staff, Attendance, Payroll
from .serializers import StaffSerializer, AttendanceSerializer, PayrollSerializer


MONEY_PLACES = Decimal("0.01")


def _money(value):
    return Decimal(value or 0).quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def _num(value):
    if value is None:
        return 0.0
    return float(value)


def _parse_month_year(data):
    try:
        month = int(data.get("month"))
        year = int(data.get("year"))
    except (TypeError, ValueError):
        return None, None, "Month and Year are required"

    if month < 1 or month > 12:
        return None, None, "Month must be between 1 and 12"
    if year < 2000 or year > 2100:
        return None, None, "Year must be between 2000 and 2100"

    return month, year, None


def _month_bounds(month, year):
    days_in_month = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, days_in_month), days_in_month


def _attendance_stats(business, staff, month, year):
    start_date, end_date, days_in_month = _month_bounds(month, year)
    records = Attendance.objects.filter(
        business=business,
        staff=staff,
        date__gte=start_date,
        date__lte=end_date,
    )
    counts = {"present": 0, "absent": 0, "half_day": 0, "holiday": 0}
    for record in records:
        if record.status in counts:
            counts[record.status] += 1

    marked_days = sum(counts.values())
    deduction_units = Decimal(counts["absent"]) + (Decimal(counts["half_day"]) * Decimal("0.5"))
    paid_days = Decimal(days_in_month) - deduction_units
    daily_rate = Decimal(staff.monthly_salary) / Decimal(days_in_month)
    deductions = _money(daily_rate * deduction_units)

    return {
        "daysInMonth": days_in_month,
        "markedDays": marked_days,
        "presentDays": counts["present"],
        "absentDays": counts["absent"],
        "halfDays": counts["half_day"],
        "holidayDays": counts["holiday"],
        "unmarkedDays": days_in_month - marked_days,
        "paidDays": float(paid_days),
        "deductions": deductions,
    }


def _payroll_row(staff, payroll, stats, month, year):
    basic_salary = _money(payroll.basic_salary if payroll else staff.monthly_salary)
    allowances = _money(payroll.allowances if payroll else 0)
    deductions = _money(payroll.deductions if payroll else stats["deductions"])
    net_salary = _money(payroll.net_salary if payroll else basic_salary - deductions + allowances)
    status_value = payroll.status if payroll else "not_generated"

    return {
        "id": str(payroll.id) if payroll else f"{staff.id}-{year}-{month}",
        "payrollId": str(payroll.id) if payroll else "",
        "staffId": str(staff.id),
        "staffName": staff.name,
        "designation": staff.designation or "",
        "month": month,
        "year": year,
        "basicSalary": _num(basic_salary),
        "allowances": _num(allowances),
        "deductions": _num(deductions),
        "netSalary": _num(net_salary),
        "status": status_value,
        "paymentDate": payroll.payment_date.isoformat() if payroll and payroll.payment_date else "",
        "attendance": {
            key: value
            for key, value in stats.items()
            if key not in {"deductions"}
        },
    }


def _monthly_report_payload(business, month, year):
    rows = []
    payroll_by_staff = {
        row.staff_id: row
        for row in Payroll.objects.filter(business=business, month=month, year=year)
        .select_related("staff")
    }

    for staff in Staff.objects.filter(business=business, is_active=True).order_by("name"):
        stats = _attendance_stats(business, staff, month, year)
        rows.append(_payroll_row(staff, payroll_by_staff.get(staff.id), stats, month, year))

    paid_rows = [row for row in rows if row["status"] == "paid"]
    unpaid_rows = [row for row in rows if row["status"] == "unpaid"]
    generated_rows = [row for row in rows if row["status"] in {"paid", "unpaid"}]
    month_label = date(year, month, 1).strftime("%B %Y")

    return {
        "month": month,
        "year": year,
        "monthLabel": month_label,
        "rows": rows,
        "summary": {
            "totalStaff": len(rows),
            "generatedCount": len(generated_rows),
            "paidCount": len(paid_rows),
            "unpaidCount": len(unpaid_rows),
            "totalNetSalary": _num(_money(sum(Decimal(str(row["netSalary"])) for row in rows))),
            "paidAmount": _num(_money(sum(Decimal(str(row["netSalary"])) for row in paid_rows))),
            "unpaidAmount": _num(_money(sum(Decimal(str(row["netSalary"])) for row in unpaid_rows))),
            "attendanceMarkedDays": sum(row["attendance"]["markedDays"] for row in rows),
        },
    }

class StaffViewSet(viewsets.ModelViewSet):
    serializer_class = StaffSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return Staff.objects.none()
        return Staff.objects.filter(business=self.request.business, is_active=True)

class AttendanceViewSet(viewsets.ModelViewSet):
    serializer_class = AttendanceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return Attendance.objects.none()
        
        queryset = Attendance.objects.filter(business=self.request.business)
        staff_id = self.request.query_params.get("staff")
        if staff_id:
            queryset = queryset.filter(staff_id=staff_id)
            
        date = self.request.query_params.get("date")
        if date:
            queryset = queryset.filter(date=date)
            
        return queryset

    @action(detail=False, methods=["post"])
    def bulk_mark(self, request):
        """Mark attendance in bulk for a date."""
        business = request.business
        attendance_date = request.data.get("date")
        records = request.data.get("records", [])  # list of {staff_id, status, check_in_time, check_out_time}
        
        if not attendance_date:
            return Response({"success": False, "message": "Date is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            parsed_attendance_date = date.fromisoformat(attendance_date)
        except (TypeError, ValueError):
            return Response(
                {"success": False, "message": "Date must be YYYY-MM-DD"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(records, list) or not records:
            return Response(
                {"success": False, "message": "At least one attendance record is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valid_statuses = {choice[0] for choice in Attendance.STATUS_CHOICES}
        invalid_status = next(
            (rec.get("status") for rec in records if rec.get("status", "present") not in valid_statuses),
            None,
        )
        if invalid_status:
            return Response(
                {"success": False, "message": f"Invalid attendance status: {invalid_status}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        staff_by_id = {}
        invalid_staff_ids = []
        for rec in records:
            staff_id = rec.get("staff_id") or rec.get("staffId")
            if not staff_id:
                invalid_staff_ids.append("")
                continue
            try:
                staff = Staff.objects.filter(id=staff_id, business=business, is_active=True).first()
            except (ValueError, TypeError, DjangoValidationError):
                staff = None
            if not staff:
                invalid_staff_ids.append(str(staff_id))
                continue
            staff_by_id[str(staff_id)] = staff

        if invalid_staff_ids:
            return Response(
                {
                    "success": False,
                    "message": "Attendance contains staff outside this tenant or inactive staff.",
                    "invalidStaffIds": invalid_staff_ids,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
            
        created_records = []
        with transaction.atomic():
            for rec in records:
                staff_id = rec.get("staff_id") or rec.get("staffId")
                status_val = rec.get("status", "present")
                notes = rec.get("notes")
                staff = staff_by_id[str(staff_id)]
                    
                attendance, created = Attendance.objects.update_or_create(
                    staff=staff,
                    date=parsed_attendance_date,
                    defaults={
                        "business": business,
                        "status": status_val,
                        "check_in_time": rec.get("check_in_time"),
                        "check_out_time": rec.get("check_out_time"),
                        "notes": notes
                    }
                )
                created_records.append(AttendanceSerializer(attendance).data)
                
        return Response({
            "success": True,
            "message": "Bulk attendance updated successfully",
            "data": created_records
        })

class PayrollViewSet(viewsets.ModelViewSet):
    serializer_class = PayrollSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if not self.request.business:
            return Payroll.objects.none()
        queryset = Payroll.objects.filter(business=self.request.business).select_related("staff")
        month = self.request.query_params.get("month")
        year = self.request.query_params.get("year")
        status_filter = self.request.query_params.get("status")

        if month:
            queryset = queryset.filter(month=month)
        if year:
            queryset = queryset.filter(year=year)
        if status_filter in {"paid", "unpaid"}:
            queryset = queryset.filter(status=status_filter)

        return queryset.order_by("-year", "-month", "staff__name")

    @action(detail=False, methods=["get"])
    def monthly_report(self, request):
        """Return projected/generated payroll rows for the selected month."""
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active tenant business"},
                status=status.HTTP_404_NOT_FOUND,
            )

        month, year, error = _parse_month_year(request.query_params)
        if error:
            return Response({"success": False, "message": error}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "success": True,
            "data": _monthly_report_payload(business, month, year),
        })

    @action(detail=False, methods=["post"])
    def generate_monthly(self, request):
        """Generates salary vouchers in bulk for a month."""
        business = request.business
        if not business:
            return Response(
                {"success": False, "message": "No active tenant business"},
                status=status.HTTP_404_NOT_FOUND,
            )

        month, year, error = _parse_month_year(request.data)
        if error:
            return Response({"success": False, "message": error}, status=status.HTTP_400_BAD_REQUEST)

        staff_members = Staff.objects.filter(business=business, is_active=True).order_by("name")
        created_count = 0
        updated_count = 0
        skipped_paid_count = 0
        
        with transaction.atomic():
            for staff in staff_members:
                stats = _attendance_stats(business, staff, month, year)
                existing = Payroll.objects.filter(
                    business=business,
                    staff=staff,
                    month=month,
                    year=year,
                ).first()

                if existing and existing.status == "paid":
                    skipped_paid_count += 1
                    continue

                allowances = _money(existing.allowances if existing else 0)
                basic_salary = _money(staff.monthly_salary)
                deductions = stats["deductions"]
                net_salary = _money(basic_salary - deductions + allowances)

                payroll, created = Payroll.objects.update_or_create(
                    business=business,
                    staff=staff,
                    month=month,
                    year=year,
                    defaults={
                        "basic_salary": basic_salary,
                        "deductions": deductions,
                        "allowances": allowances,
                        "net_salary": net_salary,
                        "status": "unpaid",
                    },
                )
                if created:
                    created_count += 1
                else:
                    updated_count += 1

        report = _monthly_report_payload(business, month, year)
        return Response({
            "success": True,
            "message": (
                f"Payroll generated for {report['monthLabel']}: "
                f"{created_count} created, {updated_count} updated"
                + (f", {skipped_paid_count} paid records kept unchanged" if skipped_paid_count else "")
            ),
            "data": report,
        })

    @action(detail=True, methods=["post"])
    def mark_paid(self, request, pk=None):
        payroll = self.get_object()
        payment_date = request.data.get("payment_date") or timezone.localdate().isoformat()

        try:
            parsed_payment_date = date.fromisoformat(payment_date)
        except ValueError:
            return Response(
                {"success": False, "message": "Payment date must be YYYY-MM-DD"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payroll.status = "paid"
        payroll.payment_date = parsed_payment_date
        payroll.notes = request.data.get("notes", payroll.notes)
        payroll.save(update_fields=["status", "payment_date", "notes"])
        try:
            from apps.accounts.models import ActivityLog

            ActivityLog.objects.create(
                business=payroll.business,
                user=request.user if request.user and request.user.is_authenticated else None,
                action="payroll_paid",
                entity_type="payroll",
                entity_id=payroll.id,
                details={
                    "staff": payroll.staff.name,
                    "month": payroll.month,
                    "year": payroll.year,
                    "netSalary": _num(payroll.net_salary),
                    "paymentDate": payroll.payment_date.isoformat(),
                },
            )
        except Exception:
            pass

        return Response({
            "success": True,
            "message": f"Salary marked paid for {payroll.staff.name}",
            "data": PayrollSerializer(payroll).data,
        })
