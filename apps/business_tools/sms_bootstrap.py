"""Ensure each tenant has SMS templates and an opening credit balance."""

DEFAULT_SMS_TEMPLATES = (
    (
        "Festival Sale",
        "offer",
        "CSM SILKS festival offer is live. Visit our store for fresh silk saree collections and special customer pricing.",
    ),
    (
        "New Saree Arrivals",
        "new_arrival",
        "New silk saree arrivals are available at CSM SILKS today. Reply or visit the store to see the latest designs.",
    ),
    (
        "Payment Reminder",
        "payment",
        "Dear customer, your CSM SILKS account has a pending balance. Please clear the payment at your convenience.",
    ),
    (
        "Store Update",
        "store",
        "CSM SILKS online catalog is updated. Contact us to reserve sarees or visit the showroom for assistance.",
    ),
)

OPENING_SMS_CREDITS = 500
OPENING_SMS_REFERENCE = "OPENING-SMS-CREDITS"


def ensure_sms_marketing_workspace(business, *, opening_credits=OPENING_SMS_CREDITS):
    """Idempotently seed default templates and opening SMS credits for a tenant."""
    from apps.business_tools.models import SMSCreditLedger, SMSTemplate

    for name, category, message in DEFAULT_SMS_TEMPLATES:
        SMSTemplate.objects.get_or_create(
            business=business,
            name=name,
            defaults={"category": category, "message": message, "is_active": True},
        )

    SMSCreditLedger.objects.get_or_create(
        business=business,
        reference=OPENING_SMS_REFERENCE,
        defaults={
            "entry_type": "credit",
            "credits": opening_credits,
            "notes": "Opening SMS credit balance for tenant marketing workspace.",
        },
    )
