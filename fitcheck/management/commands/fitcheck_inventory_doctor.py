"""Diagnose why a character's ships do (or don't) show up in My Ships.

Read-only. Prints exactly what each layer of the inventory pipeline returns for
one character, so a "0 ships" report can be pinned to a specific layer without
changing settings or live-debugging. Makes NO ESI calls unless --esi is given.

    python manage.py fitcheck_inventory_doctor 95112684
    python manage.py fitcheck_inventory_doctor "Some Character" --esi

The same read-only report powers the admin Settings -> Diagnostics page.
"""

from django.core.management.base import BaseCommand, CommandError

from ...services import diagnostics


class Command(BaseCommand):
    help = "Report, read-only, why a character's ships do/don't surface in My Ships."

    def add_arguments(self, parser):
        parser.add_argument(
            "character",
            help="EVE character_id (int) or exact character name.",
        )
        parser.add_argument(
            "--esi",
            action="store_true",
            help="Also do a live ESI asset fetch (needs a valid token). Off by default.",
        )

    def handle(self, *args, **options):
        character = diagnostics.resolve_character(options["character"])
        if character is None:
            raise CommandError(f"No EveCharacter matching {options['character']!r}.")

        report = diagnostics.inventory_report(
            character.character_id, with_esi=options["esi"]
        )
        ct = report["corptools"]

        def line(label, value):
            self.stdout.write(f"  {label:<34} {value}")

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Inventory doctor: {character.character_name} ({character.character_id})"
            )
        )
        line("SDE ship types (category 6)", report["sde_ship_types"])
        line("FITCHECK_ASSET_SOURCE", report["asset_source"])
        self.stdout.write("")

        self.stdout.write(self.style.HTTP_INFO("corptools cache"))
        line("corptools_installed()", ct["installed"])
        line("corptools version", ct["version"])
        if ct["installed"]:
            line("CharacterAudit found?", ct["audit_found"])
            line("assets_synced_at()", ct["assets_synced_at"])
            if ct["ship_rows_all"] is None:
                line("ship_assets (no type filter)", "None (not servable)")
            else:
                line("ship_assets (no type filter)", f"{ct['ship_rows_all']} singleton rows")
                line("  of those, type in SDE set", ct["ship_rows_in_sde"])
                line("  sample type_ids", ct["sample_type_ids"])
            line("ship_assets (SDE-filtered)", ct["ship_rows_sde_filtered"])
        self.stdout.write("")

        self.stdout.write(self.style.HTTP_INFO("live ESI path"))
        line("valid asset-scope token?", report["token_present"])
        if options["esi"]:
            esi = report["esi"]
            if esi["error"]:
                line("live _fetch_assets", f"ERROR/skip: {esi['error']}")
            else:
                line("live assets rows", esi["assets"])
                line("  singleton ships in SDE set", esi["ships"])
        else:
            line("live _fetch_assets", "skipped (pass --esi to run)")

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"Verdict: {report['verdict']}"))
