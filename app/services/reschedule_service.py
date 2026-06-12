from __future__ import annotations
from typing import List, Dict, Any, Optional
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
import uuid
import logging

from .event_store import EventStoreService, ConcurrencyError
from .command_handler import DomainError, now_utc
from .commands import (
    SubmitRescheduleRequestCmd,
    ApproveRescheduleRequestCmd,
    RejectRescheduleRequestCmd,
)
from ..config import settings, get_room_config
from ..domain.permissions import (
    UserRole, Permission, BookingStatus, EventType,
    RescheduleRequestStatus, has_permission, can_modify_booking,
)
from ..domain.aggregate import BookingAggregate, overlaps
from ..db import RescheduleRequest

logger = logging.getLogger(__name__)


def _generate_request_id() -> str:
    return f"rs-{uuid.uuid4().hex[:12]}"


class RescheduleApprovalService:
    def __init__(self, db: Session):
        self.db = db
        self.store = EventStoreService(db)

    def _actor_metadata(self, actor_id: str, actor_role: str, actor_name: str) -> Dict[str, Any]:
        return {
            "actor_id": actor_id,
            "actor_role": actor_role,
            "actor_name": actor_name,
            "command_ts": now_utc().isoformat(),
            "rule_version": settings.rule_version,
        }

    def _request_to_dict(self, req: RescheduleRequest) -> Dict[str, Any]:
        return {
            "request_id": req.request_id,
            "booking_id": req.booking_id,
            "requester_id": req.requester_id,
            "requester_name": req.requester_name,
            "requester_role": req.requester_role,
            "old_start_time": req.old_start_time.isoformat() if req.old_start_time else None,
            "old_end_time": req.old_end_time.isoformat() if req.old_end_time else None,
            "old_room_id": req.old_room_id,
            "new_start_time": req.new_start_time.isoformat() if req.new_start_time else None,
            "new_end_time": req.new_end_time.isoformat() if req.new_end_time else None,
            "new_room_id": req.new_room_id,
            "reason": req.reason,
            "status": req.status,
            "approver_id": req.approver_id,
            "approver_name": req.approver_name,
            "approve_reason": req.approve_reason,
            "approved_at": req.approved_at.isoformat() if req.approved_at else None,
            "booking_version": req.booking_version,
            "rule_version": req.rule_version,
            "created_at": req.created_at.isoformat() if req.created_at else None,
            "updated_at": req.updated_at.isoformat() if req.updated_at else None,
        }

    def _validate_reschedule_params(
        self,
        agg: BookingAggregate,
        new_room_id: str,
        new_start: datetime,
        new_end: datetime,
    ) -> None:
        room_cfg = get_room_config(new_room_id)
        if not room_cfg:
            raise DomainError("ROOM_NOT_FOUND", f"房间 {new_room_id} 不存在")

        if new_start >= new_end:
            raise DomainError("INVALID_TIME_RANGE", "开始时间必须早于结束时间")

        duration_min = (new_end - new_start).total_seconds() / 60
        if duration_min < room_cfg.min_duration_minutes:
            raise DomainError("DURATION_TOO_SHORT",
                              f"时长 {duration_min} 分钟小于最小值 {room_cfg.min_duration_minutes} 分钟")
        if duration_min > room_cfg.max_duration_minutes:
            raise DomainError("DURATION_TOO_LONG",
                              f"时长 {duration_min} 分钟超过最大值 {room_cfg.max_duration_minutes} 分钟")

        step = room_cfg.time_slot_step_minutes
        if (new_start.minute % step) != 0 or (new_end.minute % step) != 0:
            raise DomainError("INVALID_TIME_SLOT", f"时间必须按 {step} 分钟步长对齐")

        start_time_only = new_start.time()
        end_time_only = new_end.time()
        if start_time_only < room_cfg.available_from or end_time_only > room_cfg.available_to:
            raise DomainError("OUTSIDE_AVAILABLE_HOURS",
                              f"房间可用时间 {room_cfg.available_from} - {room_cfg.available_to}")

        booking_window_start = now_utc().date()
        max_booking_date = booking_window_start.fromordinal(
            booking_window_start.toordinal() + room_cfg.booking_window_days
        )
        if new_start.date() > max_booking_date:
            raise DomainError("BEYOND_BOOKING_WINDOW",
                              f"只能在 {room_cfg.booking_window_days} 天内预订")

    def _ensure_naive(self, dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.replace(tzinfo=None)
        return dt

    def _check_conflicts_with_pending_requests(
        self,
        booking_id: str,
        new_room_id: str,
        new_start: datetime,
        new_end: datetime,
    ) -> List[Dict[str, Any]]:
        conflicts = []
        new_start = self._ensure_naive(new_start)
        new_end = self._ensure_naive(new_end)

        pending_requests = self.db.query(RescheduleRequest).filter(
            and_(
                RescheduleRequest.status == RescheduleRequestStatus.PENDING.value,
                RescheduleRequest.booking_id != booking_id,
                RescheduleRequest.new_room_id == new_room_id,
            )
        ).all()

        for req in pending_requests:
            req_start = self._ensure_naive(req.new_start_time)
            req_end = self._ensure_naive(req.new_end_time)
            if overlaps(new_start, new_end, req_start, req_end):
                conflicts.append({
                    "type": "pending_request",
                    "request_id": req.request_id,
                    "booking_id": req.booking_id,
                    "requester_name": req.requester_name,
                    "start_time": req.new_start_time.isoformat(),
                    "end_time": req.new_end_time.isoformat(),
                })

        return conflicts

    def _detect_internal_conflicts(self, booking_id: str) -> List[Dict[str, Any]]:
        pending = self.db.query(RescheduleRequest).filter(
            and_(
                RescheduleRequest.booking_id == booking_id,
                RescheduleRequest.status == RescheduleRequestStatus.PENDING.value,
            )
        ).order_by(RescheduleRequest.created_at.asc()).all()

        conflicts = []
        for i, req1 in enumerate(pending):
            for req2 in pending[i + 1:]:
                if req1.new_room_id != req2.new_room_id:
                    continue
                req1_start = self._ensure_naive(req1.new_start_time)
                req1_end = self._ensure_naive(req1.new_end_time)
                req2_start = self._ensure_naive(req2.new_start_time)
                req2_end = self._ensure_naive(req2.new_end_time)
                if overlaps(req1_start, req1_end, req2_start, req2_end):
                    conflicts.append({
                        "request_id_1": req1.request_id,
                        "request_id_2": req2.request_id,
                        "new_room_id": req1.new_room_id,
                        "start_1": req1.new_start_time.isoformat(),
                        "end_1": req1.new_end_time.isoformat(),
                        "start_2": req2.new_start_time.isoformat(),
                        "end_2": req2.new_end_time.isoformat(),
                    })

        return conflicts

    def submit_request(
        self,
        cmd: SubmitRescheduleRequestCmd,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.RESCHEDULE_BOOKING):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无改期权限")

        agg = self.store.load_aggregate(cmd.booking_id)
        if agg.version == 0:
            raise DomainError("BOOKING_NOT_FOUND", f"预订 {cmd.booking_id} 不存在")

        if not can_modify_booking(actor_role, agg.owner_id, actor_id, agg.team_id):
            raise DomainError("PERMISSION_DENIED", "无权修改他人预订")

        if agg.status not in (BookingStatus.APPROVED, BookingStatus.PENDING_APPROVAL):
            raise DomainError("INVALID_STATUS",
                              f"当前状态 {agg.status.value} 不允许改期")

        if not agg.start_time or not agg.end_time:
            raise DomainError("AGGREGATE_CORRUPT", "原预订时间缺失")

        old_room_id = agg.room_id
        new_room_id = cmd.new_room_id or old_room_id
        new_start = cmd.new_start_time
        new_end = cmd.new_end_time

        self._validate_reschedule_params(agg, new_room_id, new_start, new_end)

        conflicts = self.store.find_conflicting_bookings(
            new_room_id, new_start, new_end, exclude_booking_id=cmd.booking_id
        )
        if conflicts:
            raise DomainError(
                "BOOKING_CONFLICT",
                f"与 {len(conflicts)} 个预订冲突",
                {"conflicts": [
                    {"booking_id": c.booking_id, "title": c.title,
                     "start_time": c.start_time.isoformat() if c.start_time else None,
                     "end_time": c.end_time.isoformat() if c.end_time else None,
                     "owner_name": c.owner_name, "status": c.status.value}
                    for c in conflicts
                ]},
            )

        pending_conflicts = self._check_conflicts_with_pending_requests(
            cmd.booking_id, new_room_id, new_start, new_end
        )
        if pending_conflicts:
            raise DomainError(
                "PENDING_REQUEST_CONFLICT",
                f"与 {len(pending_conflicts)} 个待审批改期请求冲突",
                {"conflicts": pending_conflicts},
            )

        request_id = _generate_request_id()

        event_data = {
            "request_id": request_id,
            "booking_id": cmd.booking_id,
            "requester_id": cmd.requester_id,
            "requester_name": cmd.requester_name,
            "old_start_time": agg.start_time.isoformat(),
            "old_end_time": agg.end_time.isoformat(),
            "old_room_id": old_room_id,
            "new_start_time": new_start.isoformat(),
            "new_end_time": new_end.isoformat(),
            "new_room_id": new_room_id,
            "reason": cmd.reason,
        }

        agg.enqueue_event(EventType.RESCHEDULE_REQUESTED.value, event_data)
        pending_events = agg.drain_pending_events()

        try:
            events = self.store.append_events(
                stream_id=cmd.booking_id,
                stream_type="booking",
                expected_version=cmd.expected_version,
                events=pending_events,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        db_request = RescheduleRequest(
            request_id=request_id,
            booking_id=cmd.booking_id,
            requester_id=cmd.requester_id,
            requester_name=cmd.requester_name,
            requester_role=actor_role.value,
            old_start_time=agg.start_time,
            old_end_time=agg.end_time,
            old_room_id=old_room_id,
            new_start_time=new_start,
            new_end_time=new_end,
            new_room_id=new_room_id,
            reason=cmd.reason,
            status=RescheduleRequestStatus.PENDING.value,
            booking_version=cmd.expected_version + 1,
            rule_version=settings.rule_version,
        )
        self.db.add(db_request)
        self.db.commit()
        self.db.refresh(db_request)

        internal_conflicts = self._detect_internal_conflicts(cmd.booking_id)
        if internal_conflicts:
            logger.warning(
                f"预订 {cmd.booking_id} 存在内部待审批改期冲突: {internal_conflicts}"
            )

        new_agg = self.store.load_aggregate(cmd.booking_id)
        logger.info(
            f"改期请求已提交: request_id={request_id}, booking_id={cmd.booking_id}, "
            f"requester={cmd.requester_name}, "
            f"原时段={agg.start_time}~{agg.end_time}, "
            f"新时段={new_start}~{new_end}, "
            f"房间={old_room_id}→{new_room_id}"
        )

        return {
            "request": self._request_to_dict(db_request),
            "booking": new_agg.to_dict(),
            "events": [e.to_dict() for e in events],
            "has_internal_conflicts": len(internal_conflicts) > 0,
            "internal_conflicts": internal_conflicts,
        }

    def _get_request(self, request_id: str) -> RescheduleRequest:
        req = self.db.query(RescheduleRequest).filter(
            RescheduleRequest.request_id == request_id
        ).first()
        if not req:
            raise DomainError("REQUEST_NOT_FOUND", f"改期请求 {request_id} 不存在")
        return req

    def approve_request(
        self,
        cmd: ApproveRescheduleRequestCmd,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.APPROVE_RESCHEDULE):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无改期审批权限")

        req = self._get_request(cmd.request_id)

        if req.status != RescheduleRequestStatus.PENDING.value:
            raise DomainError("INVALID_STATUS",
                              f"当前状态 {req.status} 不允许审批，仅待审批可操作")

        agg = self.store.load_aggregate(req.booking_id)
        if agg.version == 0:
            raise DomainError("BOOKING_NOT_FOUND", f"预订 {req.booking_id} 不存在")

        if agg.status not in (BookingStatus.APPROVED, BookingStatus.PENDING_APPROVAL):
            raise DomainError("INVALID_STATUS",
                              f"预订状态 {agg.status.value} 不允许改期")

        if req.booking_version != cmd.expected_version:
            raise DomainError("CONCURRENCY_CONFLICT",
                              f"预订版本不匹配，预期 {cmd.expected_version}，实际 {req.booking_version}",
                              {"expected": cmd.expected_version, "actual": req.booking_version})

        conflicts = self.store.find_conflicting_bookings(
            req.new_room_id, req.new_start_time, req.new_end_time, exclude_booking_id=req.booking_id
        )
        if conflicts:
            req.status = RescheduleRequestStatus.CONFLICT.value
            req.updated_at = now_utc()
            self.db.commit()
            raise DomainError(
                "BOOKING_CONFLICT",
                f"审批时检测到与 {len(conflicts)} 个预订冲突",
                {"conflicts": [
                    {"booking_id": c.booking_id, "title": c.title,
                     "start_time": c.start_time.isoformat() if c.start_time else None,
                     "end_time": c.end_time.isoformat() if c.end_time else None,
                     "owner_name": c.owner_name, "status": c.status.value}
                    for c in conflicts
                ]},
            )

        pending_conflicts = self._check_conflicts_with_pending_requests(
            req.booking_id, req.new_room_id, req.new_start_time, req.new_end_time
        )
        if pending_conflicts:
            req.status = RescheduleRequestStatus.CONFLICT.value
            req.updated_at = now_utc()
            self.db.commit()
            raise DomainError(
                "PENDING_REQUEST_CONFLICT",
                f"审批时检测到与 {len(pending_conflicts)} 个待审批改期请求冲突",
                {"conflicts": pending_conflicts},
            )

        event_data = {
            "request_id": req.request_id,
            "booking_id": req.booking_id,
            "approver_id": cmd.approver_id,
            "approver_name": cmd.approver_name,
            "old_start_time": req.old_start_time.isoformat(),
            "old_end_time": req.old_end_time.isoformat(),
            "old_room_id": req.old_room_id,
            "new_start_time": req.new_start_time.isoformat(),
            "new_end_time": req.new_end_time.isoformat(),
            "new_room_id": req.new_room_id,
            "reason": cmd.reason,
        }

        agg.enqueue_event(EventType.RESCHEDULE_APPROVED.value, event_data)
        pending_events = agg.drain_pending_events()

        try:
            events = self.store.append_events(
                stream_id=req.booking_id,
                stream_type="booking",
                expected_version=cmd.expected_version,
                events=pending_events,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        req.status = RescheduleRequestStatus.APPROVED.value
        req.approver_id = cmd.approver_id
        req.approver_name = cmd.approver_name
        req.approve_reason = cmd.reason
        req.approved_at = now_utc()
        req.updated_at = now_utc()
        self.db.commit()

        other_pending = self.db.query(RescheduleRequest).filter(
            and_(
                RescheduleRequest.booking_id == req.booking_id,
                RescheduleRequest.status == RescheduleRequestStatus.PENDING.value,
                RescheduleRequest.request_id != req.request_id,
            )
        ).all()
        for other in other_pending:
            other.status = RescheduleRequestStatus.SUPERSEDED.value
            other.approver_id = cmd.approver_id
            other.approver_name = cmd.approver_name
            other.approve_reason = f"被请求 {req.request_id} 覆盖"
            other.updated_at = now_utc()
        self.db.commit()

        new_agg = self.store.load_aggregate(req.booking_id)
        logger.info(
            f"改期请求已批准: request_id={req.request_id}, booking_id={req.booking_id}, "
            f"approver={cmd.approver_name}, "
            f"新时段={req.new_start_time}~{req.new_end_time}"
        )

        return {
            "request": self._request_to_dict(req),
            "booking": new_agg.to_dict(),
            "events": [e.to_dict() for e in events],
            "superseded_requests": [r.request_id for r in other_pending],
        }

    def reject_request(
        self,
        cmd: RejectRescheduleRequestCmd,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.REJECT_RESCHEDULE):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无改期驳回权限")

        req = self._get_request(cmd.request_id)

        if req.status != RescheduleRequestStatus.PENDING.value:
            raise DomainError("INVALID_STATUS",
                              f"当前状态 {req.status} 不允许驳回，仅待审批可操作")

        agg = self.store.load_aggregate(req.booking_id)
        if agg.version == 0:
            raise DomainError("BOOKING_NOT_FOUND", f"预订 {req.booking_id} 不存在")

        if req.booking_version != cmd.expected_version:
            raise DomainError("CONCURRENCY_CONFLICT",
                              f"预订版本不匹配，预期 {cmd.expected_version}，实际 {req.booking_version}",
                              {"expected": cmd.expected_version, "actual": req.booking_version})

        event_data = {
            "request_id": req.request_id,
            "booking_id": req.booking_id,
            "approver_id": cmd.approver_id,
            "approver_name": cmd.approver_name,
            "reason": cmd.reason,
        }

        agg.enqueue_event(EventType.RESCHEDULE_REJECTED.value, event_data)
        pending_events = agg.drain_pending_events()

        try:
            events = self.store.append_events(
                stream_id=req.booking_id,
                stream_type="booking",
                expected_version=cmd.expected_version,
                events=pending_events,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        req.status = RescheduleRequestStatus.REJECTED.value
        req.approver_id = cmd.approver_id
        req.approver_name = cmd.approver_name
        req.approve_reason = cmd.reason
        req.updated_at = now_utc()
        self.db.commit()

        new_agg = self.store.load_aggregate(req.booking_id)
        logger.info(
            f"改期请求已驳回: request_id={req.request_id}, booking_id={req.booking_id}, "
            f"approver={cmd.approver_name}, reason={cmd.reason}"
        )

        return {
            "request": self._request_to_dict(req),
            "booking": new_agg.to_dict(),
            "events": [e.to_dict() for e in events],
        }

    def get_request(self, request_id: str) -> Dict[str, Any]:
        req = self._get_request(request_id)
        return self._request_to_dict(req)

    def list_requests(
        self,
        booking_id: Optional[str] = None,
        status: Optional[str] = None,
        requester_id: Optional[str] = None,
        room_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        q = self.db.query(RescheduleRequest)
        filters = []
        if booking_id:
            filters.append(RescheduleRequest.booking_id == booking_id)
        if status:
            filters.append(RescheduleRequest.status == status)
        if requester_id:
            filters.append(RescheduleRequest.requester_id == requester_id)
        if room_id:
            filters.append(or_(
                RescheduleRequest.old_room_id == room_id,
                RescheduleRequest.new_room_id == room_id,
            ))
        if filters:
            q = q.filter(and_(*filters))

        total = q.count()
        q = q.order_by(RescheduleRequest.created_at.desc())
        q = q.offset(offset).limit(limit)
        requests = q.all()

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [self._request_to_dict(r) for r in requests],
            "rule_version": settings.rule_version,
        }

    def get_booking_pending_requests(self, booking_id: str) -> List[Dict[str, Any]]:
        requests = self.db.query(RescheduleRequest).filter(
            and_(
                RescheduleRequest.booking_id == booking_id,
                RescheduleRequest.status == RescheduleRequestStatus.PENDING.value,
            )
        ).order_by(RescheduleRequest.created_at.asc()).all()
        return [self._request_to_dict(r) for r in requests]
