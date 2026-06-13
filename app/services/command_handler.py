from __future__ import annotations
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
from sqlalchemy.orm import Session
import uuid
import logging

from .event_store import EventStoreService, ConcurrencyError

logger = logging.getLogger(__name__)
from .commands import (
    CreateBookingCmd, ApproveBookingCmd, RejectBookingCmd,
    RescheduleBookingCmd, CancelBookingCmd, CheckInCmd,
    ReleaseBookingCmd, ArbitrateCmd, CompleteBookingCmd,
)
from ..config import settings, get_room_config
from ..domain.permissions import (
    UserRole, Permission, BookingStatus, EventType,
    has_permission, can_modify_booking,
)
from ..domain.aggregate import EventRecord, BookingAggregate


class DomainError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _generate_id() -> str:
    return f"bk-{uuid.uuid4().hex[:12]}"


class CommandHandler:
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

    def create_booking(self, cmd: CreateBookingCmd, actor_id: str, actor_role: UserRole, actor_name: str) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.CREATE_BOOKING):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无创建预订权限")

        room_cfg = get_room_config(cmd.room_id)
        if not room_cfg:
            raise DomainError("ROOM_NOT_FOUND", f"房间 {cmd.room_id} 不存在")

        if cmd.start_time >= cmd.end_time:
            raise DomainError("INVALID_TIME_RANGE", "开始时间必须早于结束时间")

        duration_min = (cmd.end_time - cmd.start_time).total_seconds() / 60
        if duration_min < room_cfg.min_duration_minutes:
            raise DomainError("DURATION_TOO_SHORT",
                              f"时长 {duration_min} 分钟小于最小值 {room_cfg.min_duration_minutes} 分钟")
        if duration_min > room_cfg.max_duration_minutes:
            raise DomainError("DURATION_TOO_LONG",
                              f"时长 {duration_min} 分钟超过最大值 {room_cfg.max_duration_minutes} 分钟")

        step = room_cfg.time_slot_step_minutes
        if (cmd.start_time.minute % step) != 0 or (cmd.end_time.minute % step) != 0:
            raise DomainError("INVALID_TIME_SLOT",
                              f"时间必须按 {step} 分钟步长对齐")

        start_time_only = cmd.start_time.time()
        end_time_only = cmd.end_time.time()
        if start_time_only < room_cfg.available_from or end_time_only > room_cfg.available_to:
            raise DomainError("OUTSIDE_AVAILABLE_HOURS",
                              f"房间可用时间 {room_cfg.available_from} - {room_cfg.available_to}")

        booking_window_start = now_utc().date()
        max_booking_date = booking_window_start.fromordinal(booking_window_start.toordinal() + room_cfg.booking_window_days)
        if cmd.start_time.date() > max_booking_date:
            raise DomainError("BEYOND_BOOKING_WINDOW",
                              f"只能在 {room_cfg.booking_window_days} 天内预订")

        conflicts = self.store.find_conflicting_bookings(cmd.room_id, cmd.start_time, cmd.end_time)
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

        booking_id = _generate_id()
        auto_approved = not room_cfg.require_approval

        event_data = {
            "booking_id": booking_id,
            "room_id": cmd.room_id,
            "owner_id": cmd.owner_id,
            "owner_name": cmd.owner_name,
            "team_id": cmd.team_id,
            "title": cmd.title,
            "start_time": cmd.start_time.isoformat(),
            "end_time": cmd.end_time.isoformat(),
            "attendees": cmd.attendees,
            "description": cmd.description,
            "require_approval": room_cfg.require_approval,
            "auto_approved": auto_approved,
        }

        agg = BookingAggregate(booking_id=booking_id, version=0)
        agg.enqueue_event(EventType.BOOKING_CREATED.value, event_data)
        pending = agg.drain_pending_events()
        try:
            events = self.store.append_events(
                stream_id=booking_id,
                stream_type="booking",
                expected_version=0,
                events=pending,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        new_agg = self.store.load_aggregate(booking_id)
        return {
            "booking": new_agg.to_dict(),
            "events": [e.to_dict() for e in events],
        }

    def approve_booking(self, cmd: ApproveBookingCmd, actor_id: str, actor_role: UserRole, actor_name: str) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.APPROVE_BOOKING):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无审批权限")

        agg = self.store.load_aggregate(cmd.booking_id)
        if agg.version == 0:
            raise DomainError("BOOKING_NOT_FOUND", f"预订 {cmd.booking_id} 不存在")

        if agg.status != BookingStatus.PENDING_APPROVAL:
            raise DomainError("INVALID_STATUS",
                              f"当前状态 {agg.status.value} 不允许审批，仅待审批可操作")

        event_data = {
            "booking_id": cmd.booking_id,
            "approver_id": cmd.approver_id,
            "approver_name": cmd.approver_name,
            "reason": cmd.reason,
        }
        agg.enqueue_event(EventType.BOOKING_APPROVED.value, event_data)
        pending = agg.drain_pending_events()
        try:
            events = self.store.append_events(
                stream_id=cmd.booking_id,
                stream_type="booking",
                expected_version=cmd.expected_version,
                events=pending,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        new_agg = self.store.load_aggregate(cmd.booking_id)
        return {"booking": new_agg.to_dict(), "events": [e.to_dict() for e in events]}

    def reject_booking(self, cmd: RejectBookingCmd, actor_id: str, actor_role: UserRole, actor_name: str) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.REJECT_BOOKING):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无驳回权限")

        agg = self.store.load_aggregate(cmd.booking_id)
        if agg.version == 0:
            raise DomainError("BOOKING_NOT_FOUND", f"预订 {cmd.booking_id} 不存在")

        if agg.status != BookingStatus.PENDING_APPROVAL:
            raise DomainError("INVALID_STATUS",
                              f"当前状态 {agg.status.value} 不允许驳回")

        event_data = {
            "booking_id": cmd.booking_id,
            "approver_id": cmd.approver_id,
            "approver_name": cmd.approver_name,
            "reason": cmd.reason,
        }
        agg.enqueue_event(EventType.BOOKING_REJECTED.value, event_data)
        pending = agg.drain_pending_events()
        try:
            events = self.store.append_events(
                stream_id=cmd.booking_id,
                stream_type="booking",
                expected_version=cmd.expected_version,
                events=pending,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        new_agg = self.store.load_aggregate(cmd.booking_id)
        return {"booking": new_agg.to_dict(), "events": [e.to_dict() for e in events]}

    def reschedule_booking(self, cmd: RescheduleBookingCmd, actor_id: str, actor_role: UserRole, actor_name: str) -> Dict[str, Any]:
        from .reschedule_service import RescheduleApprovalService
        from .commands import SubmitRescheduleRequestCmd

        can_direct_approve = has_permission(actor_role, Permission.APPROVE_RESCHEDULE)

        if not can_direct_approve:
            svc = RescheduleApprovalService(self.db)
            submit_cmd = SubmitRescheduleRequestCmd(
                booking_id=cmd.booking_id,
                requester_id=cmd.rescheduler_id,
                requester_name=cmd.rescheduler_name,
                new_start_time=cmd.new_start_time,
                new_end_time=cmd.new_end_time,
                new_room_id=cmd.new_room_id,
                reason=cmd.reason,
                expected_version=cmd.expected_version,
            )
            result = svc.submit_request(submit_cmd, actor_id, actor_role, actor_name)
            return {
                "booking": result["booking"],
                "events": result["events"],
                "reschedule_request": result["request"],
                "requires_approval": True,
                "has_internal_conflicts": result.get("has_internal_conflicts", False),
                "internal_conflicts": result.get("internal_conflicts", []),
            }

        agg = self.store.load_aggregate(cmd.booking_id)
        if agg.version == 0:
            raise DomainError("BOOKING_NOT_FOUND", f"预订 {cmd.booking_id} 不存在")

        if not has_permission(actor_role, Permission.RESCHEDULE_BOOKING):
            if not (actor_id == agg.owner_id and has_permission(actor_role, Permission.CANCEL_BOOKING)):
                raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无改期权限")

        if not can_modify_booking(actor_role, agg.owner_id, actor_id, agg.team_id):
            raise DomainError("PERMISSION_DENIED", "无权修改他人预订")

        if agg.status not in (BookingStatus.APPROVED, BookingStatus.PENDING_APPROVAL):
            raise DomainError("INVALID_STATUS",
                              f"当前状态 {agg.status.value} 不允许改期")

        old_room_id = agg.room_id
        new_room_id = cmd.new_room_id or old_room_id
        new_start = cmd.new_start_time
        new_end = cmd.new_end_time

        if old_room_id != new_room_id:
            room_cfg = get_room_config(new_room_id)
            if not room_cfg:
                raise DomainError("ROOM_NOT_FOUND", f"房间 {new_room_id} 不存在")
        else:
            room_cfg = get_room_config(old_room_id)

        if new_start >= new_end:
            raise DomainError("INVALID_TIME_RANGE", "开始时间必须早于结束时间")

        duration_min = (new_end - new_start).total_seconds() / 60
        if duration_min < room_cfg.min_duration_minutes:
            raise DomainError("DURATION_TOO_SHORT",
                              f"时长 {duration_min} 分钟小于最小值 {room_cfg.min_duration_minutes}")
        if duration_min > room_cfg.max_duration_minutes:
            raise DomainError("DURATION_TOO_LONG",
                              f"时长 {duration_min} 分钟超过最大值 {room_cfg.max_duration_minutes}")

        step = room_cfg.time_slot_step_minutes
        if (new_start.minute % step) != 0 or (new_end.minute % step) != 0:
            raise DomainError("INVALID_TIME_SLOT", f"时间必须按 {step} 分钟步长对齐")

        start_time_only = new_start.time()
        end_time_only = new_end.time()
        if start_time_only < room_cfg.available_from or end_time_only > room_cfg.available_to:
            raise DomainError("OUTSIDE_AVAILABLE_HOURS",
                              f"房间可用时间 {room_cfg.available_from} - {room_cfg.available_to}")

        booking_window_start = now_utc().date()
        max_booking_date = booking_window_start.fromordinal(booking_window_start.toordinal() + room_cfg.booking_window_days)
        if new_start.date() > max_booking_date:
            raise DomainError("BEYOND_BOOKING_WINDOW",
                              f"只能在 {room_cfg.booking_window_days} 天内预订")

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

        if not agg.start_time or not agg.end_time:
            raise DomainError("AGGREGATE_CORRUPT", "原预订时间缺失")

        event_data = {
            "booking_id": cmd.booking_id,
            "rescheduler_id": cmd.rescheduler_id,
            "rescheduler_name": cmd.rescheduler_name,
            "old_start_time": agg.start_time.isoformat(),
            "old_end_time": agg.end_time.isoformat(),
            "new_start_time": new_start.isoformat(),
            "new_end_time": new_end.isoformat(),
            "old_room_id": old_room_id,
            "new_room_id": new_room_id,
            "reason": cmd.reason,
        }
        agg.enqueue_event(EventType.BOOKING_RESCHEDULED.value, event_data)
        pending = agg.drain_pending_events()
        try:
            events = self.store.append_events(
                stream_id=cmd.booking_id,
                stream_type="booking",
                expected_version=cmd.expected_version,
                events=pending,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        new_agg = self.store.load_aggregate(cmd.booking_id)

        from .waitlist_service import WaitlistService
        if old_room_id and agg.start_time and agg.end_time:
            try:
                wl_svc = WaitlistService(self.db)
                wl_svc.match_waitlists_for_slot(
                    freed_room_id=old_room_id,
                    freed_start=agg.start_time,
                    freed_end=agg.end_time,
                    trigger_event="BOOKING_RESCHEDULED",
                    trigger_booking_id=cmd.booking_id,
                    operator_id=actor_id,
                    operator_name=actor_name,
                )
            except Exception as e:
                logger.warning(f"改期后触发候补匹配失败: {e}")

        return {
            "booking": new_agg.to_dict(),
            "events": [e.to_dict() for e in events],
            "requires_approval": False,
        }

    def cancel_booking(self, cmd: CancelBookingCmd, actor_id: str, actor_role: UserRole, actor_name: str) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.CANCEL_BOOKING):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无取消权限")

        agg = self.store.load_aggregate(cmd.booking_id)
        if agg.version == 0:
            raise DomainError("BOOKING_NOT_FOUND", f"预订 {cmd.booking_id} 不存在")

        if not can_modify_booking(actor_role, agg.owner_id, actor_id, agg.team_id):
            raise DomainError("PERMISSION_DENIED", "无权取消他人预订")

        if agg.status in (BookingStatus.CANCELLED, BookingStatus.REJECTED, BookingStatus.COMPLETED):
            raise DomainError("INVALID_STATUS",
                              f"当前状态 {agg.status.value} 不允许取消")

        if agg.start_time:
            minutes_to_start = (_ensure_naive(agg.start_time) - _ensure_naive(now_utc())).total_seconds() / 60
            if minutes_to_start < settings.default_cancel_before_minutes and actor_role == UserRole.MEMBER:
                if agg.owner_id != actor_id:
                    pass
                # 成员只能在开始前 N 分钟取消，管理员无限制
                if actor_role == UserRole.MEMBER and minutes_to_start < 0:
                    raise DomainError("CANCEL_WINDOW_EXPIRED",
                                      f"已超过取消窗口（需提前 {settings.default_cancel_before_minutes} 分钟）")

        event_data = {
            "booking_id": cmd.booking_id,
            "canceller_id": cmd.canceller_id,
            "canceller_name": cmd.canceller_name,
            "reason": cmd.reason,
        }
        agg.enqueue_event(EventType.BOOKING_CANCELLED.value, event_data)
        pending = agg.drain_pending_events()
        try:
            events = self.store.append_events(
                stream_id=cmd.booking_id,
                stream_type="booking",
                expected_version=cmd.expected_version,
                events=pending,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        new_agg = self.store.load_aggregate(cmd.booking_id)

        from .waitlist_service import WaitlistService
        if new_agg.room_id and new_agg.start_time and new_agg.end_time:
            try:
                wl_svc = WaitlistService(self.db)
                wl_svc.match_waitlists_for_slot(
                    freed_room_id=new_agg.room_id,
                    freed_start=new_agg.start_time,
                    freed_end=new_agg.end_time,
                    trigger_event="BOOKING_CANCELLED",
                    trigger_booking_id=cmd.booking_id,
                    operator_id=actor_id,
                    operator_name=actor_name,
                )
            except Exception as e:
                logger.warning(f"取消后触发候补匹配失败: {e}")

        return {"booking": new_agg.to_dict(), "events": [e.to_dict() for e in events]}

    def check_in(self, cmd: CheckInCmd, actor_id: str, actor_role: UserRole, actor_name: str) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.CHECK_IN):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无签到权限")

        agg = self.store.load_aggregate(cmd.booking_id)
        if agg.version == 0:
            raise DomainError("BOOKING_NOT_FOUND", f"预订 {cmd.booking_id} 不存在")

        if agg.status not in (BookingStatus.APPROVED,):
            raise DomainError("INVALID_STATUS",
                              f"当前状态 {agg.status.value} 不允许签到，仅已批准预订可签到")

        room_cfg = get_room_config(agg.room_id)
        if not room_cfg:
            raise DomainError("ROOM_NOT_FOUND", f"房间 {agg.room_id} 不存在")

        check_time = cmd.check_in_time or now_utc()

        if agg.start_time:
            diff_seconds = (check_time - agg.start_time).total_seconds()
            diff_minutes = diff_seconds / 60
            grace = room_cfg.check_in_grace_minutes
            if diff_minutes < -30:
                raise DomainError("CHECK_IN_TOO_EARLY",
                                  f"签到过早（可在开始前 30 分钟内签到）")
            if diff_minutes > grace:
                raise DomainError("CHECK_IN_GRACE_EXPIRED",
                                  f"已超过签到宽限期 {grace} 分钟（开始后 {diff_minutes:.1f} 分钟）")

        event_data = {
            "booking_id": cmd.booking_id,
            "check_in_user_id": cmd.check_in_user_id,
            "check_in_user_name": cmd.check_in_user_name,
            "check_in_time": check_time.isoformat(),
        }
        agg.enqueue_event(EventType.BOOKING_CHECKED_IN.value, event_data)
        pending = agg.drain_pending_events()
        try:
            events = self.store.append_events(
                stream_id=cmd.booking_id,
                stream_type="booking",
                expected_version=cmd.expected_version,
                events=pending,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        new_agg = self.store.load_aggregate(cmd.booking_id)
        return {"booking": new_agg.to_dict(), "events": [e.to_dict() for e in events]}

    def release_booking(self, cmd: ReleaseBookingCmd, actor_id: str, actor_role: UserRole, actor_name: str) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.RELEASE_UNUSED):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无释放权限")

        agg = self.store.load_aggregate(cmd.booking_id)
        if agg.version == 0:
            raise DomainError("BOOKING_NOT_FOUND", f"预订 {cmd.booking_id} 不存在")

        room_cfg = get_room_config(agg.room_id)
        grace = room_cfg.check_in_grace_minutes if room_cfg else 15

        release_time = cmd.release_time or now_utc()

        if agg.status == BookingStatus.APPROVED:
            if agg.start_time:
                diff_minutes = (release_time - agg.start_time).total_seconds() / 60
                if diff_minutes < grace and actor_role == UserRole.MEMBER:
                    raise DomainError("RELEASE_TOO_EARLY",
                                      f"宽限期内不可释放（需开始后 {grace} 分钟）")
        elif agg.status in (BookingStatus.CHECKED_IN,):
            pass
        else:
            raise DomainError("INVALID_STATUS",
                              f"当前状态 {agg.status.value} 不允许释放")

        event_data = {
            "booking_id": cmd.booking_id,
            "released_by_id": cmd.released_by_id,
            "released_by_name": cmd.released_by_name,
            "reason": cmd.reason,
            "release_time": release_time.isoformat(),
        }
        agg.enqueue_event(EventType.BOOKING_RELEASED.value, event_data)
        pending = agg.drain_pending_events()
        try:
            events = self.store.append_events(
                stream_id=cmd.booking_id,
                stream_type="booking",
                expected_version=cmd.expected_version,
                events=pending,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        new_agg = self.store.load_aggregate(cmd.booking_id)

        from .waitlist_service import WaitlistService
        if new_agg.room_id and new_agg.start_time and new_agg.end_time:
            try:
                wl_svc = WaitlistService(self.db)
                wl_svc.match_waitlists_for_slot(
                    freed_room_id=new_agg.room_id,
                    freed_start=new_agg.start_time,
                    freed_end=new_agg.end_time,
                    trigger_event="BOOKING_RELEASED",
                    trigger_booking_id=cmd.booking_id,
                    operator_id=actor_id,
                    operator_name=actor_name,
                )
            except Exception as e:
                logger.warning(f"释放后触发候补匹配失败: {e}")

        return {"booking": new_agg.to_dict(), "events": [e.to_dict() for e in events]}

    def arbitrate(self, cmd: ArbitrateCmd, actor_id: str, actor_role: UserRole, actor_name: str) -> Dict[str, Any]:
        if actor_role != UserRole.SYSTEM_ADMIN:
            raise DomainError("PERMISSION_DENIED",
                              "仅系统管理员可执行仲裁")

        agg = self.store.load_aggregate(cmd.booking_id)
        if agg.version == 0:
            raise DomainError("BOOKING_NOT_FOUND", f"预订 {cmd.booking_id} 不存在")

        arbitration_time = cmd.arbitration_time or now_utc()

        event_data = {
            "booking_id": cmd.booking_id,
            "arbitrator_id": cmd.arbitrator_id,
            "arbitrator_name": cmd.arbitrator_name,
            "decision": cmd.decision,
            "reason": cmd.reason,
            "affected_booking_ids": cmd.affected_booking_ids,
            "arbitration_time": arbitration_time.isoformat(),
        }
        agg.enqueue_event(EventType.BOOKING_ARBITRATED.value, event_data)
        pending = agg.drain_pending_events()
        try:
            events = self.store.append_events(
                stream_id=cmd.booking_id,
                stream_type="booking",
                expected_version=cmd.expected_version,
                events=pending,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        new_agg = self.store.load_aggregate(cmd.booking_id)
        return {"booking": new_agg.to_dict(), "events": [e.to_dict() for e in events]}

    def complete_booking(self, cmd: CompleteBookingCmd, actor_id: str, actor_role: UserRole, actor_name: str) -> Dict[str, Any]:
        agg = self.store.load_aggregate(cmd.booking_id)
        if agg.version == 0:
            raise DomainError("BOOKING_NOT_FOUND", f"预订 {cmd.booking_id} 不存在")

        if agg.status in (BookingStatus.CANCELLED, BookingStatus.REJECTED, BookingStatus.RELEASED, BookingStatus.COMPLETED):
            raise DomainError("INVALID_STATUS",
                              f"当前状态 {agg.status.value} 不允许标记完成")

        completed_at = cmd.completed_at or now_utc()

        event_data = {
            "booking_id": cmd.booking_id,
            "completed_at": completed_at.isoformat(),
        }
        agg.enqueue_event(EventType.BOOKING_COMPLETED.value, event_data)
        pending = agg.drain_pending_events()
        expected_ver = self.store._get_current_version(cmd.booking_id)
        try:
            events = self.store.append_events(
                stream_id=cmd.booking_id,
                stream_type="booking",
                expected_version=expected_ver,
                events=pending,
                metadata=self._actor_metadata(actor_id, actor_role.value, actor_name),
            )
        except ConcurrencyError as e:
            raise DomainError("CONCURRENCY_CONFLICT", str(e),
                              {"stream_id": e.stream_id, "expected": e.expected_version, "actual": e.actual_version})

        new_agg = self.store.load_aggregate(cmd.booking_id)
        return {"booking": new_agg.to_dict(), "events": [e.to_dict() for e in events]}
