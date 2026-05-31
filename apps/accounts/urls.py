from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    SendOTPView,
    VerifyOTPView,
    TextileTenantRegistrationView,
    DemoSessionView,
    CookieTokenRefreshView,
    LogoutView,
    UserProfileView,
    TenantWorkspaceView,
    ActivityFeedView,
    BusinessViewSet,
    UserManagementViewSet,
)

router = DefaultRouter()
router.include_format_suffixes = False
router.register(r"business", BusinessViewSet, basename="business")
router.register(r"users", UserManagementViewSet, basename="users")

urlpatterns = [
    path("register", TextileTenantRegistrationView.as_view(), name="tenant_register"),
    path("send-otp", SendOTPView.as_view(), name="send_otp"),
    path("verify-otp", VerifyOTPView.as_view(), name="verify_otp"),
    path("token/refresh", CookieTokenRefreshView.as_view(), name="token_refresh"),
    path("logout", LogoutView.as_view(), name="logout"),
    path("demo-session", DemoSessionView.as_view(), name="demo_session"),
    path("profile", UserProfileView.as_view(), name="user_profile"),
    path("workspace", TenantWorkspaceView.as_view(), name="tenant_workspace"),
    path("activity", ActivityFeedView.as_view(), name="activity_feed"),
    path("", include(router.urls)),
]
