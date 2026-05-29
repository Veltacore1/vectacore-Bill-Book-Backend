from rest_framework import permissions


ROLE_KEYS = ("admin", "partner", "salesman", "accountant", "stock_manager")
FULL_ACCESS = set(ROLE_KEYS)
OWNER_ACCESS = {"admin", "partner"}
ADMIN_ACCESS = {"admin"}


MODULE_ROLE_ACCESS = {
    "dashboard": {
        "view": FULL_ACCESS,
        "create": set(),
        "manage": set(),
    },
    "business": {
        "view": {"admin", "partner", "accountant"},
        "create": {"admin"},
        "manage": OWNER_ACCESS,
    },
    "users": {
        "view": ADMIN_ACCESS,
        "create": ADMIN_ACCESS,
        "manage": ADMIN_ACCESS,
    },
    "parties": {
        "view": {"admin", "partner", "salesman", "accountant"},
        "create": {"admin", "partner", "salesman", "accountant"},
        "manage": {"admin", "partner", "accountant"},
    },
    "items": {
        "view": FULL_ACCESS,
        "create": {"admin", "partner", "stock_manager"},
        "manage": {"admin", "partner", "stock_manager"},
    },
    "stock": {
        "view": FULL_ACCESS,
        "create": {"admin", "partner", "stock_manager"},
        "manage": {"admin", "partner", "stock_manager"},
    },
    "sales": {
        "view": {"admin", "partner", "salesman", "accountant"},
        "create": {"admin", "partner", "salesman"},
        "manage": OWNER_ACCESS,
    },
    "purchases": {
        "view": {"admin", "partner", "accountant", "stock_manager"},
        "create": {"admin", "partner", "accountant"},
        "manage": {"admin", "partner", "accountant"},
    },
    "payments": {
        "view": {"admin", "partner", "salesman", "accountant"},
        "create": {"admin", "partner", "salesman", "accountant"},
        "manage": {"admin", "partner", "accountant"},
    },
    "accounting": {
        "view": {"admin", "partner", "accountant"},
        "create": {"admin", "partner", "accountant"},
        "manage": {"admin", "partner", "accountant"},
    },
    "staff": {
        "view": {"admin", "partner", "accountant"},
        "create": {"admin", "partner", "accountant"},
        "manage": {"admin", "partner", "accountant"},
    },
    "settings": {
        "view": {"admin", "partner", "accountant"},
        "create": OWNER_ACCESS,
        "manage": OWNER_ACCESS,
    },
    "business_tools": {
        "view": {"admin", "partner", "salesman", "accountant", "stock_manager"},
        "create": {"admin", "partner", "salesman", "stock_manager"},
        "manage": {"admin", "partner", "salesman", "stock_manager"},
    },
    "reports": {
        "view": {"admin", "partner", "accountant"},
        "create": {"admin", "partner", "accountant"},
        "manage": set(),
    },
    "audit": {
        "view": {"admin", "partner", "accountant"},
        "create": set(),
        "manage": set(),
    },
}


ROLE_MODULES = {
    role: {
        module: {
            "view": role in access["view"] or role in access["create"] or role in access["manage"],
            "create": role in access["create"],
            "manage": role in access["manage"],
        }
        for module, access in MODULE_ROLE_ACCESS.items()
    }
    for role in ROLE_KEYS
}
NO_ROLE_ACCESS = {
    module: {"view": False, "create": False, "manage": False}
    for module in MODULE_ROLE_ACCESS
}


PATH_MODULE_PREFIXES = (
    ("/api/v1/auth/workspace", "dashboard"),
    ("/api/v1/auth/business", "business"),
    ("/api/v1/auth/users", "users"),
    ("/api/v1/auth/activity", "audit"),
    ("/api/v1/parties/", "parties"),
    ("/api/v1/items/movements", "stock"),
    ("/api/v1/items/godown-stocks", "stock"),
    ("/api/v1/items/transfers", "stock"),
    ("/api/v1/items/godown-transfers", "stock"),
    ("/api/v1/items/godowns", "stock"),
    ("/api/v1/items/barcode-labels", "items"),
    ("/api/v1/items/", "items"),
    ("/api/v1/sales/", "sales"),
    ("/api/v1/purchases/", "purchases"),
    ("/api/v1/payments/", "payments"),
    ("/api/v1/accounting/reports", "reports"),
    ("/api/v1/accounting/", "accounting"),
    ("/api/v1/staff/", "staff"),
    ("/api/v1/business-tools/", "business_tools"),
    ("/api/v1/settings/", "settings"),
)

MANAGE_ACTION_SUFFIXES = (
    "/cancel/",
    "/void/",
    "/trigger_einvoice/",
    "/retry_einvoice/",
    "/cancel_einvoice/",
    "/convert_to_invoice/",
    "/stock_adjustment/",
    "/transfer/",
    "/set_status/",
    "/sync_delivery/",
    "/active_settings/",
    "/active_preferences/",
    "/active_reminders/",
)


def role_permissions_for(role):
    return ROLE_MODULES.get(role, NO_ROLE_ACCESS)


def module_for_path(path):
    normalized_path = path.rstrip("/") + "/"
    for prefix, module_key in PATH_MODULE_PREFIXES:
        normalized_prefix = prefix.rstrip("/") + "/"
        if normalized_path.startswith(normalized_prefix):
            return module_key
    return None


def required_access_for_request(method, path=""):
    if method in permissions.SAFE_METHODS:
        return "view"
    normalized_path = path.rstrip("/") + "/"
    if method == "POST" and not any(suffix in normalized_path for suffix in MANAGE_ACTION_SUFFIXES):
        return "create"
    return "manage"


def has_module_access(role, module_key, method="GET", path=""):
    if not module_key:
        return True
    access = role_permissions_for(role).get(module_key)
    if not access:
        return False
    return access[required_access_for_request(method, path)]


def audit_denied_access(user, request=None, module_key=None, reason="module_denied"):
    try:
        if not user or not getattr(user, "is_authenticated", False):
            return
        business = getattr(request, "business", None) if request else None
        business = business or getattr(user, "business", None)
        if not business:
            return

        path = getattr(request, "path", "") if request else ""
        method = getattr(request, "method", "") if request else ""
        module_key = module_key or module_for_path(path)

        from .models import ActivityLog

        ActivityLog.objects.create(
            business=business,
            user=user,
            action="access_denied",
            entity_type=module_key or "request",
            details={
                "path": path,
                "method": method,
                "module": module_key or "",
                "requiredAccess": required_access_for_request(method, path) if method else "",
                "role": getattr(user, "role", ""),
                "reason": reason,
            },
        )
    except Exception:
        return


class RoleModulePermission(permissions.BasePermission):
    message = "Your role does not have access to this module."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if not getattr(request.user, "is_active", False):
            audit_denied_access(request.user, request, reason="inactive_user")
            return False

        module_key = getattr(view, "module_key", None) or module_for_path(request.path)
        role = getattr(request.user, "role", "salesman")
        allowed = has_module_access(role, module_key, request.method, request.path)
        if not allowed:
            audit_denied_access(request.user, request, module_key=module_key)
        return allowed
