from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied

from .permissions import audit_denied_access, has_module_access, module_for_path


class TenantJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, token = result
        request.business = getattr(user, "business", None)
        if not getattr(user, "is_active", False):
            audit_denied_access(user, request, reason="inactive_user")
            raise AuthenticationFailed("User account is disabled.")

        module_key = module_for_path(request.path)
        if module_key and not has_module_access(getattr(user, "role", "salesman"), module_key, request.method, request.path):
            audit_denied_access(user, request, module_key=module_key)
            raise PermissionDenied("Your role does not have access to this module.")
        return user, token
