import re

from django.db import transaction

from .models import Business, DocumentSequence


_TRAILING_NUMBER_RE = re.compile(r"(\d+)$")


def trailing_document_number(value):
    match = _TRAILING_NUMBER_RE.search(value or "")
    if not match:
        return 0
    return int(match.group(1))


def highest_existing_number(queryset, field_name):
    highest = 0
    for value in queryset.values_list(field_name, flat=True).iterator():
        highest = max(highest, trailing_document_number(value))
    return highest


def _increment(sequence, number_prefix, initial_value, width):
    if sequence.last_number < initial_value:
        sequence.last_number = initial_value

    update_fields = ["last_number", "updated_at"]
    if sequence.prefix != number_prefix:
        sequence.prefix = number_prefix
        update_fields.append("prefix")

    sequence.last_number += 1
    sequence.save(update_fields=update_fields)
    return f"{number_prefix}{sequence.last_number:0{width}d}"


def allocate_document_number(*, business, sequence_key, number_prefix, initial_value=0, width=4):
    initial_value = max(0, int(initial_value or 0))

    try:
        with transaction.atomic():
            sequence = DocumentSequence.objects.select_for_update().get(
                business=business,
                sequence_key=sequence_key,
            )
            return _increment(sequence, number_prefix, initial_value, width)
    except DocumentSequence.DoesNotExist:
        pass

    with transaction.atomic():
        Business.objects.select_for_update().filter(id=business.id).exists()
        sequence, _created = DocumentSequence.objects.select_for_update().get_or_create(
            business=business,
            sequence_key=sequence_key,
            defaults={
                "prefix": number_prefix,
                "last_number": initial_value,
            },
        )
        return _increment(sequence, number_prefix, initial_value, width)


def next_model_document_number(*, business, sequence_key, model, field_name, number_prefix, width=4):
    initial_value = highest_existing_number(
        model.objects.filter(
            business=business,
            **{f"{field_name}__startswith": number_prefix},
        ),
        field_name,
    )
    return allocate_document_number(
        business=business,
        sequence_key=sequence_key,
        number_prefix=number_prefix,
        initial_value=initial_value,
        width=width,
    )
