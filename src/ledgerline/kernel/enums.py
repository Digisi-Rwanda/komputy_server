from enum import StrEnum


class RunStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    PD_RUNNING = "pd_running"
    LGD_RUNNING = "lgd_running"
    EAD_RUNNING = "ead_running"
    COMPLETE = "complete"
    FAILED = "failed"
    DELETED = "deleted"


class UploadKind(StrEnum):
    PD = "PD"
    LGD = "LGD"
    EAD = "EAD"


class ValidationStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


class OutputArtifactKind(StrEnum):
    PD_CALCS = "PD_CALCS"
    LGD = "LGD"
    RUNDOWN = "RUNDOWN"
    ECL_SUMMARY = "ECL_SUMMARY"
    # Spec v1.0 Step 11 workbooks; the three above stay downloadable on older runs.
    LOAN_DETAIL = "LOAN_DETAIL"
    PORTFOLIO_SUMMARY = "PORTFOLIO_SUMMARY"


class EngineStageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    ERROR = "error"


class LoanStage(StrEnum):
    STAGE_1 = "Stage 1"
    STAGE_2 = "Stage 2"
    STAGE_3 = "Stage 3"


from enum import StrEnum


class UserRole(StrEnum):
    ADMINISTRATOR = "administrator"
    ANALYST = "analyst"
    REVIEWER = "reviewer"


class MemberStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class TenantPlan(StrEnum):
    TRIAL = "trial"
    STARTER = "starter"
    GROWTH = "growth"
    ENTERPRISE = "enterprise"


class TenantStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSING = "closing"


class DeviceType(StrEnum):
    LAPTOP = "laptop"
    PHONE = "phone"
    UNKNOWN = "unknown"


class InvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ReportingCadence(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


SUPPORTED_CURRENCIES = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "CHF",
        "JPY",
        "CNY",
        "INR",
        "AED",
        "SAR",
        "ZAR",
        "NGN",
        "GHS",
        "KES",
        "UGX",
        "TZS",
        "RWF",
        "BIF",
        "ETB",
        "CDF",
        "XAF",
        "XOF",
        "MAD",
        "EGP",
        "ZMW",
        "MWK",
        "MZN",
        "BWP",
        "NAD",
    }
)
REPORTING_CADENCES = frozenset({"monthly", "quarterly"})
