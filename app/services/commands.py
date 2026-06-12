from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class CreateBookingCmd(BaseModel):
    room_id: str
    owner_id: str
    owner_name: str
    team_id: Optional[str] = None
    title: str
    start_time: datetime
    end_time: datetime
    attendees: List[str] = Field(default_factory=list)
    description: Optional[str] = None
    expected_version: int = 0


class ApproveBookingCmd(BaseModel):
    booking_id: str
    approver_id: str
    approver_name: str
    reason: Optional[str] = None
    expected_version: int


class RejectBookingCmd(BaseModel):
    booking_id: str
    approver_id: str
    approver_name: str
    reason: str
    expected_version: int


class RescheduleBookingCmd(BaseModel):
    booking_id: str
    rescheduler_id: str
    rescheduler_name: str
    new_start_time: datetime
    new_end_time: datetime
    new_room_id: Optional[str] = None
    reason: Optional[str] = None
    expected_version: int


class CancelBookingCmd(BaseModel):
    booking_id: str
    canceller_id: str
    canceller_name: str
    reason: Optional[str] = None
    expected_version: int


class CheckInCmd(BaseModel):
    booking_id: str
    check_in_user_id: str
    check_in_user_name: str
    check_in_time: Optional[datetime] = None
    expected_version: int


class ReleaseBookingCmd(BaseModel):
    booking_id: str
    released_by_id: str
    released_by_name: str
    reason: str
    release_time: Optional[datetime] = None
    expected_version: int


class ArbitrateCmd(BaseModel):
    booking_id: str
    arbitrator_id: str
    arbitrator_name: str
    decision: str
    reason: str
    affected_booking_ids: List[str] = Field(default_factory=list)
    arbitration_time: Optional[datetime] = None
    expected_version: int


class CompleteBookingCmd(BaseModel):
    booking_id: str
    completed_at: Optional[datetime] = None


class SubmitRescheduleRequestCmd(BaseModel):
    booking_id: str
    requester_id: str
    requester_name: str
    new_start_time: datetime
    new_end_time: datetime
    new_room_id: Optional[str] = None
    reason: Optional[str] = None
    expected_version: int


class ApproveRescheduleRequestCmd(BaseModel):
    request_id: str
    approver_id: str
    approver_name: str
    reason: Optional[str] = None
    expected_version: int


class RejectRescheduleRequestCmd(BaseModel):
    request_id: str
    approver_id: str
    approver_name: str
    reason: str
    expected_version: int
