from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from .models import ActivityLog


def _json_safe(value):
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def write_activity(*, business, user=None, action, entity_type=None, entity_id=None, details=None):
    return ActivityLog.objects.create(
        business=business,
        user=user if user and getattr(user, "is_authenticated", False) else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=_json_safe(details or {}),
    )
