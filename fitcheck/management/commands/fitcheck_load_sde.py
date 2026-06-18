from django.core.management.base import BaseCommand

from ...services.sde_loader import load_sde


class Command(BaseCommand):
    help = "Download and load the EVE static data slice Fit Check needs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Reload even when the remote build matches the last load.",
        )

    def handle(self, *args, **options):
        record = load_sde(force=options["force"])
        if record is None:
            self.stdout.write(self.style.SUCCESS("SDE already current - nothing to do."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Loaded {record.type_count} types (build {record.sde_build})."
                )
            )
