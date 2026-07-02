"""Aggregate reporting data collected by the snapshot beat task.

``ComplianceSnapshot`` rows are derived data: each one records, for one doctrine
on one day, how its target audience broke down by compliance state. They exist
so reports can chart trends — point-in-time submissions cannot be aggregated
retroactively. Rows are safe to purge at any time (the Diagnostics page offers
purge controls; ``FITCHECK_SNAPSHOT_RETENTION_DAYS`` auto-prunes); losing them
only shortens the available trend history.
"""

from django.db import models

from .doctrine import Doctrine


class ComplianceSnapshot(models.Model):
    """Daily per-doctrine compliance aggregate over the doctrine's target
    audience (holders of ``basic_access`` admitted by the doctrine's
    categories). Written by ``fitcheck.tasks.take_compliance_snapshots``;
    re-running on the same day updates the day's row in place."""

    doctrine = models.ForeignKey(
        Doctrine, on_delete=models.CASCADE, related_name="compliance_snapshots"
    )
    date = models.DateField()
    audience_count = models.PositiveIntegerField(default=0)
    compliant_count = models.PositiveIntegerField(default=0)
    compliant_subs_count = models.PositiveIntegerField(default=0)
    non_compliant_count = models.PositiveIntegerField(default=0)
    no_submission_count = models.PositiveIntegerField(default=0)
    taken_at = models.DateTimeField(auto_now=True)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["doctrine", "date"], name="fitcheck_snapshot_doctrine_date"
            )
        ]
        indexes = [models.Index(fields=["date"])]

    def __str__(self) -> str:
        return (
            f"{self.date} {self.doctrine}: "
            f"{self.compliant_count + self.compliant_subs_count}/{self.audience_count} compliant"
        )
