from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/parties/", include("apps.parties.urls")),
    path("api/v1/items/", include("apps.items.urls")),
    path("api/v1/sales/", include("apps.sales.urls")),
    path("api/v1/purchases/", include("apps.purchases.urls")),
    path("api/v1/payments/", include("apps.payments.urls")),
    path("api/v1/accounting/", include("apps.accounting.urls")),
    path("api/v1/staff/", include("apps.staff.urls")),
    path("api/v1/business-tools/", include("apps.business_tools.urls")),
    path("api/v1/settings/", include("apps.business_settings.urls")),
]
