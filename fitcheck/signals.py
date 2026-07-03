"""Signals other Alliance Auth apps can subscribe to.

``compliance_changed`` fires whenever a submission's compliance state moves:
on first grading (submit), on a re-check, and on a reviewer decision.

Receivers get these keyword arguments:

- ``sender`` -- the ``FitSubmission`` class
- ``submission`` -- the ``FitSubmission`` instance (already saved)
- ``user`` -- the pilot the submission belongs to
- ``fit`` -- the ``DoctrineFit`` graded against
- ``doctrine`` -- the ``Doctrine`` whose policy snapshot graded it, or ``None``
  (source defaults)
- ``old_verdict`` / ``new_verdict`` -- ``FitSubmission.Verdict`` values;
  ``old_verdict`` is ``None`` on first grading
- ``old_status`` / ``new_status`` -- ``FitSubmission.Status`` values;
  ``old_status`` is ``None`` on first grading
- ``actor`` -- who caused the change (submitter, re-check actor, or reviewer);
  may be ``None`` for automated re-checks
"""

from django.dispatch import Signal

compliance_changed = Signal()
