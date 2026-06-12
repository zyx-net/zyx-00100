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
