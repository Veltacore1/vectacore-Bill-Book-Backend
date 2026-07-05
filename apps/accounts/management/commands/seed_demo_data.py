from django.core.management.base import BaseCommand

from seed_data import seed_database


class Command(BaseCommand):
    help = "Seed or refresh the CSM SILKS demo tenant (idempotent update_or_create)."

    def handle(self, *args, **options):
        seed_database()
        self.stdout.write(self.style.SUCCESS("Demo tenant seed completed."))
