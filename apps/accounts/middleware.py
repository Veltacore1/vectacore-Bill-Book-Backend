from django.utils.deprecation import MiddlewareMixin

class MultiTenantMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if request.user.is_authenticated:
            request.business = getattr(request.user, "business", None)
        else:
            request.business = None
        return None
