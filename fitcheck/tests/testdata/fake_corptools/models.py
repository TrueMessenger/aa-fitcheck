"""Test-only stand-in for the slice of aa-corptools 3.1.0 that
``fitcheck.services.corptools_source`` reads.

Mirrors corptools 3.1.0 ``corptools/models/assets.py`` + ``audits.py`` (only the
fields fitcheck touches). Registered as app_label ``corptools`` in the test
settings so corptools_source runs REAL ORM filtering against real tables - the
previous duck-typed fakes in ``test_corptools_source.py`` ignored ``filter()``
kwargs, so ``singleton`` / ``type_id__in`` / ``character`` filtering was never
actually exercised.
"""

import datetime

from allianceauth.eveonline.models import EveCharacter
from django.db import models
from django.utils import timezone


class CharacterAudit(models.Model):
    character = models.OneToOneField(EveCharacter, on_delete=models.CASCADE)
    update_timestamps = models.JSONField(default=dict)

    def get_update_time(self, key: str):
        val = self.update_timestamps.get(key)
        if val is None:
            return None
        return datetime.datetime.fromisoformat(val)

    def set_update_time(self, key: str) -> None:
        self.update_timestamps[key] = timezone.now().isoformat()


class CharacterAsset(models.Model):
    # corptools' real model uses an abstract ``Asset`` base, so this is a single
    # concrete table - exactly what fitcheck's filter/.values() reads.
    character = models.ForeignKey(CharacterAudit, on_delete=models.CASCADE)
    blueprint_copy = models.BooleanField(null=True, default=None)
    singleton = models.BooleanField()
    item_id = models.BigIntegerField()
    location_flag = models.CharField(max_length=50)
    location_id = models.BigIntegerField()
    location_type = models.CharField(max_length=25)
    quantity = models.IntegerField()
    type_id = models.IntegerField()
    name = models.CharField(max_length=255, null=True, default=None)
