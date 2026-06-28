"""Local mirror of the slice of the EVE static data export the engine needs.

django-eveuniverse does not expose ``variationParentTypeID`` (the data behind
Pyfa's "Variations" menu), so we keep our own compact tables, refreshed
whenever CCP publishes a new static-data build.
"""

from django.db import models
from django.db.models.functions import Upper

from ..constants import EveCategoryId, EveMetaGroupId, SlotKind


class SdeType(models.Model):
    type_id = models.PositiveIntegerField(primary_key=True)
    name = models.CharField(max_length=200, db_index=True)
    group_id = models.PositiveIntegerField()
    category_id = models.PositiveIntegerField(db_index=True)
    # EVE market group; used to separate boosters/clone mappers from implants
    # (they share category 20 but live under different market subtrees).
    market_group_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    # Normalized: points to itself when the type IS the family parent.
    variation_parent_type_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    meta_group_id = models.PositiveIntegerField(null=True, blank=True)
    meta_level = models.PositiveSmallIntegerField(null=True, blank=True)
    slot_kind = models.CharField(max_length=8, choices=SlotKind.choices, default=SlotKind.OTHER)
    published = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(Upper("name"), name="fitcheck_sdetype_name_upper"),
            models.Index(fields=["category_id", "published"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.type_id})"

    @property
    def is_abyssal(self) -> bool:
        return self.meta_group_id == EveMetaGroupId.ABYSSAL

    @property
    def is_ship(self) -> bool:
        return self.category_id == EveCategoryId.SHIP


class SdeAttribute(models.Model):
    attribute_id = models.PositiveIntegerField(primary_key=True)
    name = models.CharField(max_length=100, db_index=True)
    display_name = models.CharField(max_length=150, blank=True, db_index=True)
    high_is_good = models.BooleanField(default=True)
    unit_name = models.CharField(max_length=50, blank=True)
    published = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.display_name or self.name


class SdeTypeAttribute(models.Model):
    eve_type = models.ForeignKey(SdeType, on_delete=models.CASCADE, related_name="attributes")
    attribute = models.ForeignKey(SdeAttribute, on_delete=models.CASCADE, related_name="+")
    value = models.FloatField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["eve_type", "attribute"], name="fitcheck_unique_type_attribute"
            )
        ]

    def __str__(self) -> str:
        return f"{self.eve_type_id}.{self.attribute_id}={self.value}"


class SdeMutaplasmidMapping(models.Model):
    """One row per (abyssal result type, base source type, mutaplasmid)."""

    abyssal_type = models.ForeignKey(
        SdeType, on_delete=models.CASCADE, related_name="mutation_sources"
    )
    source_type = models.ForeignKey(
        SdeType, on_delete=models.CASCADE, related_name="mutation_results"
    )
    mutator_type_id = models.PositiveIntegerField()
    # [{"attr_id": int, "min": float, "max": float, "high_is_good": bool}, ...]
    mutable_attributes = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["abyssal_type", "source_type", "mutator_type_id"],
                name="fitcheck_unique_mutaplasmid_mapping",
            )
        ]

    def __str__(self) -> str:
        return f"{self.source_type_id} -> {self.abyssal_type_id} via {self.mutator_type_id}"


class SdeLoadRecord(models.Model):
    sde_build = models.CharField(max_length=128)
    loaded_at = models.DateTimeField(auto_now_add=True)
    type_count = models.PositiveIntegerField(default=0)

    class Meta:
        get_latest_by = "loaded_at"

    def __str__(self) -> str:
        return f"{self.sde_build} @ {self.loaded_at:%Y-%m-%d %H:%M}"


class StructureNameCache(models.Model):
    """Locally-cached names for private upwell structures (Citadels, id >= 1e12).

    django-eveuniverse has no model for player structures and aa-corptools caches
    only the raw ``location_id``, so we persist resolved names ourselves. The bulk
    member-inventory scan reads ONLY this table (never ESI); the periodic
    ``refresh_structure_names`` task fills/refreshes rows via ESI with bounded,
    paced, negatively-cached lookups - so one inaccessible Citadel costs a couple
    of attempts per run instead of a 403 per structure-scoped token (which was
    what tripped ESI's error limit on an alliance-wide scan).

    Row states:
    - ``resolved_at`` NULL, ``accessible`` True  -> pending (seen by a scan, not yet resolved)
    - ``resolved_at`` set                        -> resolved (refreshed when older than the TTL)
    - ``accessible`` False, ``name`` NULL        -> negative-cached (retried only past backoff)
    """

    structure_id = models.BigIntegerField(primary_key=True)
    name = models.CharField(max_length=255, null=True, blank=True)
    solar_system_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    # NULL = never resolved (pending); set on each successful resolution.
    resolved_at = models.DateTimeField(null=True, blank=True)
    # Last attempt (success OR failure); drives the negative-cache backoff.
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    fail_count = models.PositiveIntegerField(default=0)
    # False once no available token could reach the structure; retried after backoff.
    accessible = models.BooleanField(default=True)

    class Meta:
        verbose_name = "structure name cache entry"
        verbose_name_plural = "structure name cache entries"
        indexes = [
            models.Index(fields=["resolved_at"]),
            models.Index(fields=["accessible", "last_attempt_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.name or 'pending'} ({self.structure_id})"
