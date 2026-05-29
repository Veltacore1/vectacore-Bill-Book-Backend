import uuid

from django.db.models import Count, Q
from django.utils import timezone

from .models import BusinessNotification
from .serializers import BusinessNotificationSerializer


def stable_notification_id(business, source_type, source_key):
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"{business.id}:{source_type}:{source_key}")


def upsert_notification(
    *,
    business,
    source_type,
    source_key,
    title,
    message,
    priority="medium",
    target="",
    metadata=None,
):
    source_id = source_key if isinstance(source_key, uuid.UUID) else stable_notification_id(business, source_type, source_key)
    payload = {
        "title": title[:160],
        "message": message,
        "priority": priority if priority in {"high", "medium", "low"} else "medium",
        "target": target or "",
        "metadata": metadata or {},
    }
    notification, created = BusinessNotification.objects.get_or_create(
        business=business,
        source_type=source_type,
        source_id=source_id,
        defaults=payload,
    )
    if not created:
        changed_fields = []
        for field, value in payload.items():
            if getattr(notification, field) != value:
                setattr(notification, field, value)
                changed_fields.append(field)
        if changed_fields:
            notification.save(update_fields=[*changed_fields, "updated_at"])
    return notification, created


def mark_notification_read(notification):
    if notification.status != "read":
        notification.status = "read"
        notification.read_at = timezone.now()
        notification.save(update_fields=["status", "read_at", "updated_at"])
    return notification


def notification_counts(business):
    rows = (
        BusinessNotification.objects.filter(business=business)
        .aggregate(
            total=Count("id"),
            unread=Count("id", filter=Q(status="unread")),
            read=Count("id", filter=Q(status="read")),
            dismissed=Count("id", filter=Q(status="dismissed")),
        )
    )
    return {key: rows.get(key) or 0 for key in ("total", "unread", "read", "dismissed")}


def serialize_notifications(queryset):
    return BusinessNotificationSerializer(queryset, many=True).data
