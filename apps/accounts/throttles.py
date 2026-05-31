from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework.throttling import ScopedRateThrottle


class TenantScopedRateThrottle(ScopedRateThrottle):
    """Scoped throttle that reads rates from current Django settings."""

    def get_rate(self):
        if not getattr(self, "scope", None):
            raise ImproperlyConfigured(f"You must set `.throttle_scope` for {self.__class__.__name__}.")

        rates = getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES", {})
        try:
            return rates[self.scope]
        except KeyError as error:
            raise ImproperlyConfigured(f"No throttle rate set for scope '{self.scope}'.") from error
