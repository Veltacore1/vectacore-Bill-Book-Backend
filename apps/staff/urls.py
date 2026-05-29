from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import StaffViewSet, AttendanceViewSet, PayrollViewSet

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"directory", StaffViewSet, basename="staff_directory")
router.register(r"attendance", AttendanceViewSet, basename="attendance")
router.register(r"payroll", PayrollViewSet, basename="payroll")

urlpatterns = [
    path("", include(router.urls)),
]
