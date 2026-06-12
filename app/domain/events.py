from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Dict, Any, List
from datetime import datetime
from enum import Enum


class BookingCreatedData(BaseModel):
    booking_id: str
    room_id: str
    owner_id: str
    owner_name: str
    team_id: Optional[str] = None
    title: str
    start_time: datetime
    end_time: datetime
    attendees: List[str] = Field(default_factory=list)
    description: Optional[str] = None
    require_approval: bool = False
    auto_approved: bool = False


class BookingApprovedData(BaseModel):
    booking_id: str
    approver_id: str
    approver_name: str
    reason: Optional[str] = None


class BookingRejectedData(BaseModel):
    booking_id: str
    approver_id: str
    approver_name: str
    reason: str


class BookingRescheduledData(BaseModel):
    booking_id: str
    rescheduler_id: str
    rescheduler_name: str
    old_start_time: datetime
    old_end_time: datetime
    new_start_time: datetime
    new_end_time: datetime
    old_room_id: str
    new_room_id: str
    reason: Optional[str] = None


class BookingCancelledData(BaseModel):
    booking_id: str
    canceller_id: str
    canceller_name: str
    reason: Optional[str] = None


class BookingCheckedInData(BaseModel):
    booking_id: str
    check_in_user_id: str
    check_in_user_name: str
    check_in_time: datetime


class BookingReleasedData(BaseModel):
    booking_id: str
    released_by_id: str
    released_by_name: str
    reason: str
    release_time: datetime


class BookingArbitratedData(BaseModel):
    booking_id: str
    arbitrator_id: str
    arbitrator_name: str
    decision: str
    reason: str
    affected_booking_ids: List[str] = Field(default_factory=list)
    arbitration_time: datetime


class BookingCompletedData(BaseModel):
    booking_id: str
    completed_at: datetime


class RescheduleRequestedData(BaseModel):
    request_id: str
    booking_id: str
    requester_id: str
    requester_name: str
    old_start_time: datetime
    old_end_time: datetime
    old_room_id: str
    new_start_time: datetime
    new_end_time: datetime
    new_room_id: str
    reason: Optional[str] = None


class RescheduleApprovedData(BaseModel):
    request_id: str
    booking_id: str
    approver_id: str
    approver_name: str
    old_start_time: datetime
    old_end_time: datetime
    old_room_id: str
    new_start_time: datetime
    new_end_time: datetime
    new_room_id: str
    reason: Optional[str] = None


class RescheduleRejectedData(BaseModel):
    request_id: str
    booking_id: str
    approver_id: str
    approver_name: str
    reason: str
