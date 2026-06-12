from __future__ import annotations
from typing import List, Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass, field
import json

from .permissions import BookingStatus, EventType
from .events import (
    BookingCreatedData,
    BookingApprovedData,
    BookingRejectedData,
    BookingRescheduledData,
    BookingCancelledData,
    BookingCheckedInData,
    BookingReleasedData,
    BookingArbitratedData,
    BookingCompletedData,
)


@dataclass
class EventRecord:
    event_id: int
    stream_id: str
    stream_type: str
    version: int
    event_type: str
    event_data: Dict[str, Any]
    metadata: Dict[str, Any]
    rule_version: str
    created_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "stream_id": self.stream_id,
            "stream_type": self.stream_type,
            "version": self.version,
            "event_type": self.event_type,
            "event_data": self.event_data,
            "metadata": self.metadata,
            "rule_version": self.rule_version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass
class BookingAggregate:
    booking_id: str = ""
    room_id: str = ""
    owner_id: str = ""
    owner_name: str = ""
    team_id: Optional[str] = None
    title: str = ""
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    attendees: List[str] = field(default_factory=list)
    description: Optional[str] = None
    status: BookingStatus = BookingStatus.PENDING_APPROVAL
    version: int = 0
    require_approval: bool = False
    approver_id: Optional[str] = None
    approver_name: Optional[str] = None
    reject_reason: Optional[str] = None
    last_check_in_time: Optional[datetime] = None
    release_reason: Optional[str] = None
    arbitration_decision: Optional[str] = None
    arbitration_reason: Optional[str] = None
    reschedule_history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    _pending_events: List[Dict[str, Any]] = field(default_factory=list)

    def apply(self, event_type: str, event_data: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> None:
        if event_type == EventType.BOOKING_CREATED.value:
            self._apply_created(event_data)
        elif event_type == EventType.BOOKING_APPROVED.value:
            self._apply_approved(event_data)
        elif event_type == EventType.BOOKING_REJECTED.value:
            self._apply_rejected(event_data)
        elif event_type == EventType.BOOKING_RESCHEDULED.value:
            self._apply_rescheduled(event_data)
        elif event_type == EventType.BOOKING_CANCELLED.value:
            self._apply_cancelled(event_data)
        elif event_type == EventType.BOOKING_CHECKED_IN.value:
            self._apply_checked_in(event_data)
        elif event_type == EventType.BOOKING_RELEASED.value:
            self._apply_released(event_data)
        elif event_type == EventType.BOOKING_ARBITRATED.value:
            self._apply_arbitrated(event_data)
        elif event_type == EventType.BOOKING_COMPLETED.value:
            self._apply_completed(event_data)
        self.version += 1

    def _apply_created(self, data: Dict[str, Any]) -> None:
        d = BookingCreatedData(**data)
        self.booking_id = d.booking_id
        self.room_id = d.room_id
        self.owner_id = d.owner_id
        self.owner_name = d.owner_name
        self.team_id = d.team_id
        self.title = d.title
        self.start_time = d.start_time
        self.end_time = d.end_time
        self.attendees = d.attendees
        self.description = d.description
        self.require_approval = d.require_approval
        if d.auto_approved:
            self.status = BookingStatus.APPROVED
        else:
            self.status = BookingStatus.PENDING_APPROVAL
        self.created_at = datetime.now()
        self.updated_at = self.created_at

    def _apply_approved(self, data: Dict[str, Any]) -> None:
        d = BookingApprovedData(**data)
        self.status = BookingStatus.APPROVED
        self.approver_id = d.approver_id
        self.approver_name = d.approver_name
        self.updated_at = datetime.now()

    def _apply_rejected(self, data: Dict[str, Any]) -> None:
        d = BookingRejectedData(**data)
        self.status = BookingStatus.REJECTED
        self.approver_id = d.approver_id
        self.approver_name = d.approver_name
        self.reject_reason = d.reason
        self.updated_at = datetime.now()

    def _apply_rescheduled(self, data: Dict[str, Any]) -> None:
        d = BookingRescheduledData(**data)
        self.reschedule_history.append({
            "old_start_time": d.old_start_time.isoformat() if isinstance(d.old_start_time, datetime) else d.old_start_time,
            "old_end_time": d.old_end_time.isoformat() if isinstance(d.old_end_time, datetime) else d.old_end_time,
            "old_room_id": d.old_room_id,
            "new_start_time": d.new_start_time.isoformat() if isinstance(d.new_start_time, datetime) else d.new_start_time,
            "new_end_time": d.new_end_time.isoformat() if isinstance(d.new_end_time, datetime) else d.new_end_time,
            "new_room_id": d.new_room_id,
            "rescheduler_id": d.rescheduler_id,
            "rescheduler_name": d.rescheduler_name,
            "reason": d.reason,
        })
        self.start_time = d.new_start_time
        self.end_time = d.new_end_time
        self.room_id = d.new_room_id
        self.updated_at = datetime.now()

    def _apply_cancelled(self, data: Dict[str, Any]) -> None:
        self.status = BookingStatus.CANCELLED
        self.updated_at = datetime.now()

    def _apply_checked_in(self, data: Dict[str, Any]) -> None:
        d = BookingCheckedInData(**data)
        self.status = BookingStatus.CHECKED_IN
        self.last_check_in_time = d.check_in_time
        self.updated_at = d.check_in_time

    def _apply_released(self, data: Dict[str, Any]) -> None:
        d = BookingReleasedData(**data)
        self.status = BookingStatus.RELEASED
        self.release_reason = d.reason
        self.updated_at = d.release_time

    def _apply_arbitrated(self, data: Dict[str, Any]) -> None:
        d = BookingArbitratedData(**data)
        self.status = BookingStatus.ARBITRATED
        self.arbitration_decision = d.decision
        self.arbitration_reason = d.reason
        self.updated_at = d.arbitration_time

    def _apply_completed(self, data: Dict[str, Any]) -> None:
        d = BookingCompletedData(**data)
        self.status = BookingStatus.COMPLETED
        self.updated_at = d.completed_at

    def enqueue_event(self, event_type: str, event_data: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> None:
        self._pending_events.append({
            "event_type": event_type,
            "event_data": event_data,
            "metadata": metadata or {},
        })

    def drain_pending_events(self) -> List[Dict[str, Any]]:
        events = self._pending_events
        self._pending_events = []
        return events

    def to_dict(self) -> Dict[str, Any]:
        return {
            "booking_id": self.booking_id,
            "room_id": self.room_id,
            "owner_id": self.owner_id,
            "owner_name": self.owner_name,
            "team_id": self.team_id,
            "title": self.title,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "attendees": self.attendees,
            "description": self.description,
            "status": self.status.value,
            "version": self.version,
            "require_approval": self.require_approval,
            "approver_id": self.approver_id,
            "approver_name": self.approver_name,
            "reject_reason": self.reject_reason,
            "last_check_in_time": self.last_check_in_time.isoformat() if self.last_check_in_time else None,
            "release_reason": self.release_reason,
            "arbitration_decision": self.arbitration_decision,
            "arbitration_reason": self.arbitration_reason,
            "reschedule_history": self.reschedule_history,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def rebuild_aggregate(stream_id: str, events: List[EventRecord]) -> BookingAggregate:
    agg = BookingAggregate(booking_id=stream_id)
    for evt in events:
        agg.apply(evt.event_type, evt.event_data, evt.metadata)
    return agg


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end
