from enum import Enum
from typing import Optional, List


class UserRole(str, Enum):
    MEMBER = "member"
    TEAM_ADMIN = "team_admin"
    RECEPTIONIST = "receptionist"
    SYSTEM_ADMIN = "system_admin"


class BookingStatus(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHECKED_IN = "checked_in"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RELEASED = "released"
    ARBITRATED = "arbitrated"


class RescheduleRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    CONFLICT = "conflict"


class WaitlistStatus(str, Enum):
    WAITING = "waiting"
    MATCHED = "matched"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


class WaitlistMatchStatus(str, Enum):
    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    TIME_MISMATCH = "time_mismatch"
    DUPLICATE_MATCH = "duplicate_match"


class BulkImportBatchStatus(str, Enum):
    DRAFT = "draft"
    PRECHECKING = "prechecking"
    PRECHECKED = "prechecked"
    PRECHECK_FAILED = "precheck_failed"
    CONFIRMING = "confirming"
    CONFIRMED = "confirmed"
    PARTIALLY_FAILED = "partially_failed"
    CANCELLED = "cancelled"


class BulkImportDraftStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    CREATED = "created"
    CREATE_FAILED = "create_failed"
    RETRYABLE = "retryable"


class BulkImportPrecheckStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    ERROR = "error"
    WARNING = "warning"


class DeactivationPlanStatus(str, Enum):
    DRAFT = "draft"
    PRECHECKED = "prechecked"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    PROCESSED = "processed"
    REVOKED = "revoked"
    CANCELLED = "cancelled"


class DeactivationRecurrenceType(str, Enum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class ConflictResolutionAction(str, Enum):
    CANCEL = "cancel"
    RESCHEDULE = "reschedule"
    SKIP = "skip"
    PENDING = "pending"


class EventType(str, Enum):
    BOOKING_CREATED = "booking_created"
    BOOKING_APPROVED = "booking_approved"
    BOOKING_REJECTED = "booking_rejected"
    BOOKING_RESCHEDULED = "booking_rescheduled"
    BOOKING_CANCELLED = "booking_cancelled"
    BOOKING_CHECKED_IN = "booking_checked_in"
    BOOKING_RELEASED = "booking_released"
    BOOKING_ARBITRATED = "booking_arbitrated"
    BOOKING_COMPLETED = "booking_completed"
    RESCHEDULE_REQUESTED = "reschedule_requested"
    RESCHEDULE_APPROVED = "reschedule_approved"
    RESCHEDULE_REJECTED = "reschedule_rejected"
    WAITLIST_SUBMITTED = "waitlist_submitted"
    WAITLIST_MATCHED = "waitlist_matched"
    WAITLIST_CONFIRMED = "waitlist_confirmed"
    WAITLIST_CANCELLED = "waitlist_cancelled"
    WAITLIST_EXPIRED = "waitlist_expired"


class Permission(str, Enum):
    CREATE_BOOKING = "create_booking"
    APPROVE_BOOKING = "approve_booking"
    REJECT_BOOKING = "reject_booking"
    RESCHEDULE_BOOKING = "reschedule_booking"
    CANCEL_BOOKING = "cancel_booking"
    CHECK_IN = "check_in"
    RELEASE_UNUSED = "release_unused"
    ARBITRATE_CONFLICT = "arbitrate_conflict"
    QUERY_EVENTS = "query_events"
    EXPORT_SCHEDULE = "export_schedule"
    MANAGE_ROOMS = "manage_rooms"
    APPROVE_RESCHEDULE = "approve_reschedule"
    REJECT_RESCHEDULE = "reject_reschedule"
    SUBMIT_WAITLIST = "submit_waitlist"
    MANAGE_WAITLIST = "manage_waitlist"
    VIEW_ALL_WAITLIST = "view_all_waitlist"
    BULK_IMPORT_DRAFT = "bulk_import_draft"
    BULK_IMPORT_CONFIRM = "bulk_import_confirm"
    BULK_IMPORT_CANCEL = "bulk_import_cancel"
    BULK_IMPORT_VIEW_ALL = "bulk_import_view_all"
    MANAGE_DEACTIVATION = "manage_deactivation"
    VIEW_DEACTIVATION = "view_deactivation"


ROLE_PERMISSIONS = {
    UserRole.MEMBER: {
        Permission.CREATE_BOOKING,
        Permission.CANCEL_BOOKING,
        Permission.CHECK_IN,
        Permission.QUERY_EVENTS,
        Permission.EXPORT_SCHEDULE,
        Permission.RESCHEDULE_BOOKING,
        Permission.SUBMIT_WAITLIST,
        Permission.BULK_IMPORT_DRAFT,
        Permission.VIEW_DEACTIVATION,
    },
    UserRole.TEAM_ADMIN: {
        Permission.CREATE_BOOKING,
        Permission.APPROVE_BOOKING,
        Permission.REJECT_BOOKING,
        Permission.RESCHEDULE_BOOKING,
        Permission.CANCEL_BOOKING,
        Permission.CHECK_IN,
        Permission.RELEASE_UNUSED,
        Permission.QUERY_EVENTS,
        Permission.EXPORT_SCHEDULE,
        Permission.APPROVE_RESCHEDULE,
        Permission.REJECT_RESCHEDULE,
        Permission.SUBMIT_WAITLIST,
        Permission.MANAGE_WAITLIST,
        Permission.VIEW_ALL_WAITLIST,
        Permission.BULK_IMPORT_DRAFT,
        Permission.BULK_IMPORT_CONFIRM,
        Permission.BULK_IMPORT_CANCEL,
        Permission.VIEW_DEACTIVATION,
    },
    UserRole.RECEPTIONIST: {
        Permission.CREATE_BOOKING,
        Permission.APPROVE_BOOKING,
        Permission.REJECT_BOOKING,
        Permission.RESCHEDULE_BOOKING,
        Permission.CANCEL_BOOKING,
        Permission.CHECK_IN,
        Permission.RELEASE_UNUSED,
        Permission.QUERY_EVENTS,
        Permission.EXPORT_SCHEDULE,
        Permission.APPROVE_RESCHEDULE,
        Permission.REJECT_RESCHEDULE,
        Permission.SUBMIT_WAITLIST,
        Permission.MANAGE_WAITLIST,
        Permission.VIEW_ALL_WAITLIST,
        Permission.BULK_IMPORT_DRAFT,
        Permission.BULK_IMPORT_CONFIRM,
        Permission.BULK_IMPORT_CANCEL,
        Permission.BULK_IMPORT_VIEW_ALL,
        Permission.MANAGE_DEACTIVATION,
        Permission.VIEW_DEACTIVATION,
    },
    UserRole.SYSTEM_ADMIN: {
        Permission.CREATE_BOOKING,
        Permission.APPROVE_BOOKING,
        Permission.REJECT_BOOKING,
        Permission.RESCHEDULE_BOOKING,
        Permission.CANCEL_BOOKING,
        Permission.CHECK_IN,
        Permission.RELEASE_UNUSED,
        Permission.ARBITRATE_CONFLICT,
        Permission.QUERY_EVENTS,
        Permission.EXPORT_SCHEDULE,
        Permission.MANAGE_ROOMS,
        Permission.APPROVE_RESCHEDULE,
        Permission.REJECT_RESCHEDULE,
        Permission.SUBMIT_WAITLIST,
        Permission.MANAGE_WAITLIST,
        Permission.VIEW_ALL_WAITLIST,
        Permission.BULK_IMPORT_DRAFT,
        Permission.BULK_IMPORT_CONFIRM,
        Permission.BULK_IMPORT_CANCEL,
        Permission.BULK_IMPORT_VIEW_ALL,
        Permission.MANAGE_DEACTIVATION,
        Permission.VIEW_DEACTIVATION,
    },
}


def has_permission(role: UserRole, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, set())


def can_modify_booking(role: UserRole, owner_id: str, actor_id: str, team_id: Optional[str] = None) -> bool:
    if role == UserRole.SYSTEM_ADMIN or role == UserRole.RECEPTIONIST:
        return True
    if role == UserRole.TEAM_ADMIN:
        return True
    if owner_id == actor_id:
        return True
    return False
