"""Diagnose why a character's ships do (or don't) show up in My Ships.

Read-only. Prints exactly what each layer of the inventory pipeline returns for
one character, so a "0 ships" report can be pinned to a specific layer without
changing settings or live-debugging. Makes NO ESI calls unless --esi is given.

    python manage.py fitcheck_inventory_doctor 95112684
    python manage.py fitcheck_inventory_doctor "Some Character" --esi
"""

from django.core.management.base import BaseCommand, CommandError

from ...services import corptools_source, esi_assets


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
        from allianceauth.eveonline.models import EveCharacter

        ident = options["character"]
        if ident.isdigit():
            character = EveCharacter.objects.filter(character_id=int(ident)).first()
        else:
            character = EveCharacter.objects.filter(character_name=ident).first()
        if character is None:
            raise CommandError(f"No EveCharacter matching {ident!r}.")
        cid = character.character_id

        def line(label, value):
            self.stdout.write(f"  {label:<34} {value}")

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Inventory doctor: {character.character_name} ({cid})"
            )
        )

        # --- SDE ship whitelist ---
        ship_set = esi_assets._ship_type_id_set()
        line("SDE ship types (category 6)", len(ship_set))
        self.stdout.write("")

        # --- corptools cache path ---
        self.stdout.write(self.style.HTTP_INFO("corptools cache"))
        installed = corptools_source.corptools_installed()
        line("corptools_installed()", installed)
        if installed:
            audit = corptools_source._audit_for(cid)
            line("CharacterAudit found?", audit is not None)
            line("assets_synced_at()", corptools_source.assets_synced_at(cid))
            all_singletons = corptools_source.ship_assets_for_character(cid, None)
            if all_singletons is None:
                line("ship_assets (no type filter)", "None (not servable)")
            else:
                line("ship_assets (no type filter)", f"{len(all_singletons)} singleton rows")
                in_set = [s for s in all_singletons if s["type_id"] in ship_set]
                line("  of those, type in SDE set", len(in_set))
                sample = sorted({s["type_id"] for s in all_singletons})[:15]
                line("  sample type_ids", sample)
            filtered = corptools_source.ship_assets_for_character(cid, ship_set)
            line(
                "ship_assets (SDE-filtered)",
                "None" if filtered is None else f"{len(filtered)} rows",
            )
        self.stdout.write("")

        # --- token / live ESI path ---
        self.stdout.write(self.style.HTTP_INFO("live ESI path"))
        token = _any_asset_token(cid)
        line("valid asset-scope token?", token is not None)
        if options["esi"]:
            if token is None:
                line("live _fetch_assets", "skipped (no token)")
            else:
                try:
                    assets = esi_assets._fetch_assets(token, cid)
                    ships = [
                        a for a in assets
                        if a.get("type_id") in ship_set and a.get("is_singleton")
                    ]
                    line("live assets rows", len(assets))
                    line("  singleton ships in SDE set", len(ships))
                except Exception as exc:  # noqa: BLE001 - diagnostic
                    line("live _fetch_assets", f"ERROR: {type(exc).__name__}: {exc}")
        else:
            line("live _fetch_assets", "skipped (pass --esi to run)")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Done. Paste this output for diagnosis."))


def _any_asset_token(character_id: int):
    """A valid asset-scope token for this character under any user (read-only)."""
    from esi.models import Token

    return (
        Token.objects.filter(character_id=character_id)
        .require_scopes(esi_assets.ASSET_SCOPES)
        .require_valid()
        .first()
    )
