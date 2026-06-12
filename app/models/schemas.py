from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class ActorContext(BaseModel):
    actor_id: str
    actor_role: str
    actor_name: str


class CreateBookingRequest(BaseModel):
    room_id: str
    owner_id: str
    owner_name: str
    team_id: Optional[str] = None
    title: str
    start_time: datetime
    end_time: datetime
    attendees: List[str] = Field(default_factory=list)
    description: Optional[str] = None


class ApproveBookingRequest(BaseModel):
    booking_id: str
    approver_id: str
    approver_name: str
    reason: Optional[str] = None
    expected_version: int


class RejectBookingRequest(BaseModel):
    booking_id: str
    approver_id: str
    approver_name: str
    reason: str
    expected_version: int


class RescheduleBookingRequest(BaseModel):
    booking_id: str
    rescheduler_id: str
    rescheduler_name: str
    new_start_time: datetime
    new_end_time: datetime
    new_room_id: Optional[str] = None
    reason: Optional[str] = None
    expected_version: int


class CancelBookingRequest(BaseModel):
    booking_id: str
    canceller_id: str
    canceller_name: str
    reason: Optional[str] = None
    expected_version: int


class CheckInRequest(BaseModel):
    booking_id: str
    check_in_user_id: str
    check_in_user_name: str
    check_in_time: Optional[datetime] = None
    expected_version: int


class ReleaseBookingRequest(BaseModel):
    booking_id: str
    released_by_id: str
    released_by_name: str
    reason: str
    release_time: Optional[datetime] = None
    expected_version: int


class ArbitrateRequest(BaseModel):
    booking_id: str
    arbitrator_id: str
    arbitrator_name: str
    decision: str
    reason: str
    affected_booking_ids: List[str] = Field(default_factory=list)
    arbitration_time: Optional[datetime] = None
    expected_version: int


class CommandResponse(BaseModel):
    success: bool = True
    booking: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = Field(default_factory=list)
    rule_version: str


class ErrorResponse(BaseModel):
    success: bool = False
    error: Dict[str, Any]
    rule_version: str


class EventQueryResponse(BaseModel):
    total: int
    limit: int
    offset: int
    rule_version: str
    items: List[Dict[str, Any]] = Field(default_factory=list)


class ScheduleResponse(BaseModel):
    window: Dict[str, Any]
    rule_version: str
    total: int
    items: List[Dict[str, Any]] = Field(default_factory=list)


class RoomsResponse(BaseModel):
    rule_version: str
    items: List[Dict[str, Any]] = Field(default_factory=list)


class ExportResponse(BaseModel):
    rule_version: str
    format: str
    row_count: int
    window: Dict[str, Any]
    content: Optional[str] = None
    items: Optional[List[Dict[str, Any]]] = None
    header: Optional[List[str]] = None


class ConflictAnalysisResponse(BaseModel):
    rule_version: str
    room_id: str
    window: Dict[str, Any]
    has_conflict: bool
    conflict_count: int
    recommendation: str
    reason: str
    incumbent: Optional[Dict[str, Any]] = None
    affected: List[Dict[str, Any]] = Field(default_factory=list)


class SubmitRescheduleRequest(BaseModel):
    booking_id: str
    requester_id: str
    requester_name: str
    new_start_time: datetime
    new_end_time: datetime
    new_room_id: Optional[str] = None
    reason: Optional[str] = None
    expected_version: int


class ApproveRescheduleRequest(BaseModel):
    request_id: str
    approver_id: str
    approver_name: str
    reason: Optional[str] = None
    expected_version: int


class RejectRescheduleRequest(BaseModel):
    request_id: str
    approver_id: str
    approver_name: str
    reason: str
    expected_version: int


class RescheduleRequestResponse(BaseModel):
    request_id: str
    booking_id: str
    requester_id: str
    requester_name: str
    requester_role: str
    old_start_time: Optional[datetime]
    old_end_time: Optional[datetime]
    old_room_id: str
    new_start_time: Optional[datetime]
    new_end_time: Optional[datetime]
    new_room_id: str
    reason: Optional[str]
    status: str
    approver_id: Optional[str]
    approver_name: Optional[str]
    approve_reason: Optional[str]
    approved_at: Optional[datetime]
    booking_version: int
    rule_version: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


class RescheduleRequestListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    rule_version: str
    items: List[Dict[str, Any]] = Field(default_factory=list)


class RescheduleApprovalResponse(BaseModel):
    success: bool = True
    request: Optional[Dict[str, Any]] = None
    booking: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = Field(default_factory=list)
    rule_version: str
    requires_approval: Optional[bool] = None
    has_internal_conflicts: Optional[bool] = None
    internal_conflicts: List[Dict[str, Any]] = Field(default_factory=list)
    superseded_requests: List[str] = Field(default_factory=list)


class SuggestionResponse(BaseModel):
    rule_version: str
    suggestions: List[Dict[str, Any]] = Field(default_factory=list)


class SubmitWaitlistRequest(BaseModel):
    room_id: str
    requester_id: str
    requester_name: str
    team_id: Optional[str] = None
    title: str
    desired_start_time: datetime
    desired_end_time: datetime
    flex_before_minutes: int = 0
    flex_after_minutes: int = 0
    attendees: List[str] = Field(default_factory=list)
    priority_note: Optional[str] = None
    contact_info: Optional[str] = None
    description: Optional[str] = None


class ConfirmWaitlistRequest(BaseModel):
    waitlist_id: str
    confirmer_id: str
    confirmer_name: str
    reason: Optional[str] = None


class CancelWaitlistRequest(BaseModel):
    waitlist_id: str
    canceller_id: str
    canceller_name: str
    reason: Optional[str] = None


class RejectWaitlistRequest(BaseModel):
    waitlist_id: str
    rejecter_id: str
    rejecter_name: str
    reason: str


class WaitlistEntryResponse(BaseModel):
    waitlist_id: str
    room_id: str
    requester_id: str
    requester_name: str
    requester_role: str
    team_id: Optional[str]
    desired_start_time: Optional[datetime]
    desired_end_time: Optional[datetime]
    flex_before_minutes: int
    flex_after_minutes: int
    title: str
    attendees: Optional[List[str]]
    priority_note: Optional[str]
    contact_info: Optional[str]
    description: Optional[str]
    status: str
    matched_booking_id: Optional[str]
    matched_start_time: Optional[datetime]
    matched_end_time: Optional[datetime]
    match_reason: Optional[str]
    matched_at: Optional[datetime]
    confirmed_by_id: Optional[str]
    confirmed_by_name: Optional[str]
    confirmed_at: Optional[datetime]
    result_booking_id: Optional[str]
    expire_at: Optional[datetime]
    expired_at: Optional[datetime]
    expire_reason: Optional[str]
    rule_version: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


class WaitlistListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    rule_version: str
    items: List[Dict[str, Any]] = Field(default_factory=list)


class WaitlistActionResponse(BaseModel):
    success: bool = True
    waitlist: Optional[Dict[str, Any]] = None
    booking: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = Field(default_factory=list)
    rule_version: str


# ---------- 批量导入 ----------

class BulkImportDraftRow(BaseModel):
    room_id: str
    owner_id: str
    owner_name: str
    team_id: Optional[str] = None
    title: str
    start_time: datetime
    end_time: datetime
    attendees: List[str] = Field(default_factory=list)
    description: Optional[str] = None


class BulkImportUploadRequest(BaseModel):
    format: str = Field(..., pattern="^(csv|json)$")
    filename: Optional[str] = None
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    csv_content: Optional[str] = None


class BulkImportDraftInfo(BaseModel):
    draft_index: int
    row_number: int
    room_id: Optional[str] = None
    owner_id: Optional[str] = None
    owner_name: Optional[str] = None
    team_id: Optional[str] = None
    title: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    attendees: Optional[List[str]] = None
    description: Optional[str] = None
    precheck_status: str
    precheck_errors: List[Dict[str, Any]] = Field(default_factory=list)
    precheck_warnings: List[Dict[str, Any]] = Field(default_factory=list)
    result_status: str
    result_booking_id: Optional[str] = None
    result_error: Optional[Dict[str, Any]] = None
    retryable: bool = False


class BulkImportBatchResponse(BaseModel):
    batch_id: str
    submitter_id: str
    submitter_name: str
    submitter_role: str
    source_format: str
    source_filename: Optional[str] = None
    total_count: int
    status: str
    precheck_passed: bool
    precheck_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    confirmed_by_id: Optional[str] = None
    confirmed_by_name: Optional[str] = None
    cancelled_at: Optional[datetime] = None
    cancelled_by_id: Optional[str] = None
    cancelled_by_name: Optional[str] = None
    success_count: int
    failed_count: int
    precheck_summary: Dict[str, Any] = Field(default_factory=dict)
    drafts: List[Dict[str, Any]] = Field(default_factory=list)
    rule_version: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BulkImportBatchListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    rule_version: str
    items: List[Dict[str, Any]] = Field(default_factory=list)


class BulkImportPrecheckResponse(BaseModel):
    success: bool = True
    batch_id: str
    precheck_passed: bool
    error_count: int
    warning_count: int
    passed_count: int
    summary: Dict[str, Any] = Field(default_factory=dict)
    drafts: List[Dict[str, Any]] = Field(default_factory=list)
    rule_version: str


class BulkImportConfirmRequest(BaseModel):
    expected_version: Optional[str] = None
    note: Optional[str] = None


class BulkImportConfirmResponse(BaseModel):
    success: bool = True
    batch_id: str
    total_count: int
    success_count: int
    failed_count: int
    retryable_count: int
    status: str
    results: List[Dict[str, Any]] = Field(default_factory=list)
    events: List[Dict[str, Any]] = Field(default_factory=list)
    rule_version: str


class BulkImportCancelRequest(BaseModel):
    reason: Optional[str] = None


class BulkImportOperationLogResponse(BaseModel):
    log_id: str
    batch_id: str
    operation: str
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    actor_id: Optional[str] = None
    actor_name: Optional[str] = None
    actor_role: Optional[str] = None
    created_at: Optional[datetime] = None
    rule_version: str
