from .doctrine import (  # noqa: F401
    ArchivedFitVersion,
    AssignmentItemOverride,
    AssignmentItemPolicy,
    CompliancePolicy,
    Doctrine,
    DoctrineCategory,
    DoctrineFit,
    DoctrineFitItem,
    FitAssignment,
    FitItemOverride,
    PolicySlotRule,
)
from .general import General  # noqa: F401
from .reporting import ComplianceSnapshot  # noqa: F401
from .settings import EnforcementSettings, VerificationMode  # noqa: F401
from .sde import (  # noqa: F401
    SdeAttribute,
    SdeLoadRecord,
    SdeMutaplasmidMapping,
    SdeType,
    SdeTypeAttribute,
    StructureNameCache,
)
from .submission import (  # noqa: F401
    ComplianceFinding,
    FitSubmission,
    SubmissionActionLog,
    SubmissionItem,
)

# Optional Secure Groups smart filter. Defines FitComplianceFilter only when
# allianceauth-securegroups is installed; otherwise SECUREGROUPS_INSTALLED is
# False and nothing else is exported. Imported last so the doctrine/submission
# models it references are already bound.
from .securegroups import SECUREGROUPS_INSTALLED  # noqa: F401

if SECUREGROUPS_INSTALLED:
    from .securegroups import FitComplianceFilter  # noqa: F401
