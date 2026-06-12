from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
import uuid
import json
import logging

from .event_store import EventStoreService, ConcurrencyError
from .command_handler import DomainError, now_utc
from .commands import (
    SubmitWaitlistCmd, ConfirmWaitlistCmd, CancelWaitlistCmd, RejectWaitlistCmd,
    CreateBookingCmd,
)
from ..config import settings, get_room_config
from ..domain.permissions import (
    UserRole, Permission, BookingStatus, EventType, WaitlistStatus,
    WaitlistMatchStatus, has_permission, can_modify_booking,
)
from ..domain.aggregate import BookingAggregate, overlaps
from ..db import WaitlistEntry, WaitlistMatchLog, WaitlistActionLog

logger = logging.getLogger(__name__)

CONFIRMATION_WINDOW_MINUTES = 30


def _generate_waitlist_id() -> str:
    return f"wl-{uuid.uuid4().hex[:12]}"


def _generate_log_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class WaitlistService:
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

    def _entry_to_dict(self, entry: WaitlistEntry) -> Dict[str, Any]:
        attendees = json.loads(entry.attendees) if entry.attendees else []
        return {
            "waitlist_id": entry.waitlist_id,
            "room_id": entry.room_id,
            "requester_id": entry.requester_id,
            "requester_name": entry.requester_name,
            "requester_role": entry.requester_role,
            "team_id": entry.team_id,
            "desired_start_time": entry.desired_start_time.isoformat() if entry.desired_start_time else None,
            "desired_end_time": entry.desired_end_time.isoformat() if entry.desired_end_time else None,
            "flex_before_minutes": entry.flex_before_minutes,
            "flex_after_minutes": entry.flex_after_minutes,
            "title": entry.title,
            "attendees": attendees,
            "priority_note": entry.priority_note,
            "contact_info": entry.contact_info,
            "description": entry.description,
            "status": entry.status,
            "matched_booking_id": entry.matched_booking_id,
            "matched_start_time": entry.matched_start_time.isoformat() if entry.matched_start_time else None,
            "matched_end_time": entry.matched_end_time.isoformat() if entry.matched_end_time else None,
            "match_reason": entry.match_reason,
            "matched_at": entry.matched_at.isoformat() if entry.matched_at else None,
            "confirmed_by_id": entry.confirmed_by_id,
            "confirmed_by_name": entry.confirmed_by_name,
            "confirmed_at": entry.confirmed_at.isoformat() if entry.confirmed_at else None,
            "result_booking_id": entry.result_booking_id,
            "expire_at": entry.expire_at.isoformat() if entry.expire_at else None,
            "expired_at": entry.expired_at.isoformat() if entry.expired_at else None,
            "expire_reason": entry.expire_reason,
            "rule_version": entry.rule_version,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        }

    def _ensure_naive(self, dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.replace(tzinfo=None)
        return dt

    def _write_action_log(
        self,
        waitlist_id: str,
        action: str,
        old_status: Optional[str],
        new_status: Optional[str],
        reason: Optional[str],
        actor_id: Optional[str],
        actor_name: Optional[str],
        actor_role: Optional[str],
    ) -> None:
        log = WaitlistActionLog(
            log_id=_generate_log_id("wal"),
            waitlist_id=waitlist_id,
            action=action,
            old_status=old_status,
            new_status=new_status,
            reason=reason,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            rule_version=settings.rule_version,
        )
        self.db.add(log)

    def _write_match_log(
        self,
        waitlist_id: str,
        trigger_event: str,
        trigger_booking_id: Optional[str],
        freed_room_id: Optional[str],
        freed_start: Optional[datetime],
        freed_end: Optional[datetime],
        match_status: str,
        match_score: Optional[int],
        match_details: Optional[str],
        operator_id: Optional[str] = None,
        operator_name: Optional[str] = None,
    ) -> None:
        log = WaitlistMatchLog(
            log_id=_generate_log_id("wml"),
            waitlist_id=waitlist_id,
            trigger_event=trigger_event,
            trigger_booking_id=trigger_booking_id,
            freed_room_id=freed_room_id,
            freed_start_time=freed_start,
            freed_end_time=freed_end,
            match_status=match_status,
            match_score=match_score,
            match_details=match_details,
            operator_id=operator_id,
            operator_name=operator_name,
            rule_version=settings.rule_version,
        )
        self.db.add(log)

    def _validate_waitlist_params(
        self,
        room_id: str,
        desired_start: datetime,
        desired_end: datetime,
    ) -> None:
        room_cfg = get_room_config(room_id)
        if not room_cfg:
            raise DomainError("ROOM_NOT_FOUND", f"房间 {room_id} 不存在")

        if desired_start >= desired_end:
            raise DomainError("INVALID_TIME_RANGE", "开始时间必须早于结束时间")

        duration_min = (desired_end - desired_start).total_seconds() / 60
        if duration_min < room_cfg.min_duration_minutes:
            raise DomainError("DURATION_TOO_SHORT",
                              f"时长 {duration_min} 分钟小于最小值 {room_cfg.min_duration_minutes} 分钟")
        if duration_min > room_cfg.max_duration_minutes:
            raise DomainError("DURATION_TOO_LONG",
                              f"时长 {duration_min} 分钟超过最大值 {room_cfg.max_duration_minutes} 分钟")

        step = room_cfg.time_slot_step_minutes
        if (desired_start.minute % step) != 0 or (desired_end.minute % step) != 0:
            raise DomainError("INVALID_TIME_SLOT", f"时间必须按 {step} 分钟步长对齐")

        start_time_only = desired_start.time()
        end_time_only = desired_end.time()
        if start_time_only < room_cfg.available_from or end_time_only > room_cfg.available_to:
            raise DomainError("OUTSIDE_AVAILABLE_HOURS",
                              f"房间可用时间 {room_cfg.available_from} - {room_cfg.available_to}")

        booking_window_start = now_utc().date()
        max_booking_date = booking_window_start.fromordinal(
            booking_window_start.toordinal() + room_cfg.booking_window_days
        )
        if desired_start.date() > max_booking_date:
            raise DomainError("BEYOND_BOOKING_WINDOW",
                              f"只能在 {room_cfg.booking_window_days} 天内预订")

    def _find_duplicate_waiting(
        self,
        requester_id: str,
        room_id: str,
        desired_start: datetime,
        desired_end: datetime,
        flex_before: int,
        flex_after: int,
    ) -> Optional[WaitlistEntry]:
        naive_start = self._ensure_naive(desired_start)
        naive_end = self._ensure_naive(desired_end)

        window_start = naive_start - timedelta(minutes=max(flex_before, 30))
        window_end = naive_end + timedelta(minutes=max(flex_after, 30))

        active_entries = self.db.query(WaitlistEntry).filter(
            and_(
                WaitlistEntry.requester_id == requester_id,
                WaitlistEntry.room_id == room_id,
                WaitlistEntry.status.in_([
                    WaitlistStatus.WAITING.value,
                    WaitlistStatus.MATCHED.value,
                ]),
            )
        ).all()

        for entry in active_entries:
            e_start = self._ensure_naive(entry.desired_start_time)
            e_end = self._ensure_naive(entry.desired_end_time)
            e_flex_before = timedelta(minutes=entry.flex_before_minutes)
            e_flex_after = timedelta(minutes=entry.flex_after_minutes)

            entry_window_start = min(e_start - e_flex_before, naive_start - timedelta(minutes=flex_before))
            entry_window_end = max(e_end + e_flex_after, naive_end + timedelta(minutes=flex_after))

            overlap_start = max(e_start - e_flex_before, naive_start - timedelta(minutes=flex_before))
            overlap_end = min(e_end + e_flex_after, naive_end + timedelta(minutes=flex_after))

            if overlap_start < overlap_end:
                total_window = (entry_window_end - entry_window_start).total_seconds()
                overlap_sec = (overlap_end - overlap_start).total_seconds()
                if total_window > 0 and overlap_sec / total_window >= 0.5:
                    return entry
        return None

    def submit_waitlist(
        self,
        cmd: SubmitWaitlistCmd,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.SUBMIT_WAITLIST):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无候补申请权限")

        if cmd.requester_id != actor_id and not has_permission(actor_role, Permission.MANAGE_WAITLIST):
            raise DomainError("PERMISSION_DENIED", "无权为他人提交候补申请")

        self._validate_waitlist_params(cmd.room_id, cmd.desired_start_time, cmd.desired_end_time)

        conflicts = self.store.find_conflicting_bookings(
            cmd.room_id, cmd.desired_start_time, cmd.desired_end_time
        )
        if not conflicts:
            raise DomainError(
                "NO_CONFLICT",
                "目标时间段当前空闲，可直接提交预订申请，无需候补",
            )

        duplicate = self._find_duplicate_waiting(
            cmd.requester_id, cmd.room_id,
            cmd.desired_start_time, cmd.desired_end_time,
            cmd.flex_before_minutes, cmd.flex_after_minutes,
        )
        if duplicate:
            raise DomainError(
                "DUPLICATE_WAITLIST",
                f"同一时间窗存在候候补申请，请确认后重新提交或等待现有候补匹配",
                {"existing_waitlist_id": duplicate.waitlist_id},
            )

        waitlist_id = _generate_waitlist_id()

        entry = WaitlistEntry(
            waitlist_id=waitlist_id,
            room_id=cmd.room_id,
            requester_id=cmd.requester_id,
            requester_name=cmd.requester_name,
            requester_role=actor_role.value,
            team_id=cmd.team_id,
            desired_start_time=cmd.desired_start_time,
            desired_end_time=cmd.desired_end_time,
            flex_before_minutes=cmd.flex_before_minutes,
            flex_after_minutes=cmd.flex_after_minutes,
            title=cmd.title,
            attendees=json.dumps(cmd.attendees, ensure_ascii=False) if cmd.attendees else None,
            priority_note=cmd.priority_note,
            contact_info=cmd.contact_info,
            description=cmd.description,
            status=WaitlistStatus.WAITING.value,
            rule_version=settings.rule_version,
        )
        self.db.add(entry)
        self.db.flush()

        self._write_action_log(
            waitlist_id=waitlist_id,
            action="SUBMIT",
            old_status=None,
            new_status=WaitlistStatus.WAITING.value,
            reason=cmd.description or "候补申请提交",
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role.value,
        )
        self.db.commit()
        self.db.refresh(entry)

        logger.info(
            f"候补申请已提交: waitlist_id={waitlist_id}, "
            f"requester={cmd.requester_name}, room={cmd.room_id}, "
            f"时段={cmd.desired_start_time}~{cmd.desired_end_time}, "
            f"浮动=[{cmd.flex_before_minutes}, {cmd.flex_after_minutes}]分钟"
        )

        return {"waitlist": self._entry_to_dict(entry)}

    def _get_entry(self, waitlist_id: str) -> WaitlistEntry:
        entry = self.db.query(WaitlistEntry).filter(
            WaitlistEntry.waitlist_id == waitlist_id
        ).first()
        if not entry:
            raise DomainError("WAITLIST_NOT_FOUND", f"候补申请 {waitlist_id} 不存在")
        return entry

    def get_waitlist(
        self,
        waitlist_id: str,
        actor_id: str,
        actor_role: UserRole,
    ) -> Dict[str, Any]:
        entry = self._get_entry(waitlist_id)
        if entry.requester_id != actor_id and not has_permission(actor_role, Permission.VIEW_ALL_WAITLIST):
            raise DomainError("PERMISSION_DENIED", "无权查看他人候补申请")
        return {"waitlist": self._entry_to_dict(entry)}

    def list_waitlists(
        self,
        actor_id: str,
        actor_role: UserRole,
        room_id: Optional[str] = None,
        status: Optional[str] = None,
        requester_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        q = self.db.query(WaitlistEntry)
        filters = []

        if not has_permission(actor_role, Permission.VIEW_ALL_WAITLIST):
            filters.append(WaitlistEntry.requester_id == actor_id)
        elif requester_id:
            filters.append(WaitlistEntry.requester_id == requester_id)

        if room_id:
            filters.append(WaitlistEntry.room_id == room_id)
        if status:
            filters.append(WaitlistEntry.status == status)

        if filters:
            q = q.filter(and_(*filters))

        total = q.count()
        q = q.order_by(WaitlistEntry.created_at.desc())
        q = q.offset(offset).limit(limit)
        entries = q.all()

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [self._entry_to_dict(e) for e in entries],
            "rule_version": settings.rule_version,
        }

    def _calculate_match_score(
        self,
        entry: WaitlistEntry,
        freed_start: datetime,
        freed_end: datetime,
    ) -> int:
        naive_entry_start = self._ensure_naive(entry.desired_start_time)
        naive_entry_end = self._ensure_naive(entry.desired_end_time)
        naive_freed_start = self._ensure_naive(freed_start)
        naive_freed_end = self._ensure_naive(freed_end)

        flex_before = timedelta(minutes=entry.flex_before_minutes)
        flex_after = timedelta(minutes=entry.flex_after_minutes)

        acceptable_start = naive_entry_start - flex_before
        acceptable_end = naive_entry_end + flex_after

        if naive_freed_start < acceptable_start or naive_freed_end > acceptable_end:
            return -1

        entry_duration = (naive_entry_end - naive_entry_start).total_seconds()
        freed_duration = (naive_freed_end - naive_freed_start).total_seconds()
        if freed_duration < entry_duration:
            return -1

        score = 100
        start_diff = abs((naive_freed_start - naive_entry_start).total_seconds() / 60)
        end_diff = abs((naive_freed_end - naive_entry_end).total_seconds() / 60)
        score -= int(start_diff + end_diff)

        if entry.requester_role in (UserRole.SYSTEM_ADMIN.value, UserRole.RECEPTIONIST.value):
            score += 30
        elif entry.requester_role == UserRole.TEAM_ADMIN.value:
            score += 15

        return max(score, 0)

    def match_waitlists_for_slot(
        self,
        freed_room_id: str,
        freed_start: datetime,
        freed_end: datetime,
        trigger_event: str,
        trigger_booking_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        operator_name: Optional[str] = None,
    ) -> List[WaitlistEntry]:
        naive_freed_start = self._ensure_naive(freed_start)
        naive_freed_end = self._ensure_naive(freed_end)

        waiting_entries = self.db.query(WaitlistEntry).filter(
            and_(
                WaitlistEntry.room_id == freed_room_id,
                WaitlistEntry.status == WaitlistStatus.WAITING.value,
                WaitlistEntry.desired_start_time <= naive_freed_end,
                WaitlistEntry.desired_end_time >= naive_freed_start,
            )
        ).order_by(WaitlistEntry.created_at.asc()).all()

        matched: List[Tuple[int, WaitlistEntry]] = []

        for entry in waiting_entries:
            score = self._calculate_match_score(entry, freed_start, freed_end)
            if score >= 0:
                matched.append((score, entry))
                self._write_match_log(
                    waitlist_id=entry.waitlist_id,
                    trigger_event=trigger_event,
                    trigger_booking_id=trigger_booking_id,
                    freed_room_id=freed_room_id,
                    freed_start=freed_start,
                    freed_end=freed_end,
                    match_status=WaitlistMatchStatus.MATCHED.value,
                    match_score=score,
                    match_details=f"匹配成功，得分={score}",
                    operator_id=operator_id,
                    operator_name=operator_name,
                )
            else:
                self._write_match_log(
                    waitlist_id=entry.waitlist_id,
                    trigger_event=trigger_event,
                    trigger_booking_id=trigger_booking_id,
                    freed_room_id=freed_room_id,
                    freed_start=freed_start,
                    freed_end=freed_end,
                    match_status=WaitlistMatchStatus.TIME_MISMATCH.value,
                    match_score=None,
                    match_details="时间范围或浮动不匹配",
                    operator_id=operator_id,
                    operator_name=operator_name,
                )

        matched.sort(key=lambda x: (-x[0], x[1].created_at))

        if matched:
            _, best_entry = matched[0]
            best_entry.status = WaitlistStatus.MATCHED.value
            best_entry.matched_booking_id = trigger_booking_id
            best_entry.matched_start_time = freed_start
            best_entry.matched_end_time = freed_end
            best_entry.match_reason = f"由 {trigger_event} 触发匹配"
            best_entry.matched_at = now_utc()
            best_entry.expire_at = now_utc() + timedelta(minutes=CONFIRMATION_WINDOW_MINUTES)

            self._write_action_log(
                waitlist_id=best_entry.waitlist_id,
                action="MATCH",
                old_status=WaitlistStatus.WAITING.value,
                new_status=WaitlistStatus.MATCHED.value,
                reason=f"匹配到空闲时段 {freed_start}~{freed_end}",
                actor_id=operator_id,
                actor_name=operator_name,
                actor_role=None,
            )
            self.db.commit()

            logger.info(
                f"候补匹配成功: waitlist_id={best_entry.waitlist_id}, "
                f"room={freed_room_id}, freed_slot={freed_start}~{freed_end}, "
                f"确认窗口至 {best_entry.expire_at}"
            )
            return [best_entry]

        logger.info(
            f"无匹配候补: room={freed_room_id}, freed_slot={freed_start}~{freed_end}"
        )
        return []

    def confirm_waitlist(
        self,
        cmd: ConfirmWaitlistCmd,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        from .command_handler import CommandHandler
        from .commands import CreateBookingCmd

        entry = self._get_entry(cmd.waitlist_id)

        if entry.requester_id != actor_id and not has_permission(actor_role, Permission.MANAGE_WAITLIST):
            raise DomainError("PERMISSION_DENIED", "无权确认他人候补申请")

        now = now_utc()
        expire = entry.expire_at
        if now.tzinfo is None and expire and expire.tzinfo is not None:
            now = now.replace(tzinfo=expire.tzinfo)
        elif expire and expire.tzinfo is None and now.tzinfo is not None:
            expire = expire.replace(tzinfo=now.tzinfo)

        is_expired = (expire is not None and now > expire) or entry.status == WaitlistStatus.EXPIRED.value

        if entry.status == WaitlistStatus.MATCHED.value and is_expired and entry.status != WaitlistStatus.EXPIRED.value:
            entry.status = WaitlistStatus.EXPIRED.value
            entry.expired_at = now_utc()
            entry.expire_reason = "确认超时"
            self._write_action_log(
                waitlist_id=entry.waitlist_id,
                action="EXPIRE",
                old_status=WaitlistStatus.MATCHED.value,
                new_status=WaitlistStatus.EXPIRED.value,
                reason="确认超时",
                actor_id=actor_id,
                actor_name=actor_name,
                actor_role=actor_role.value,
            )
            self.db.commit()
            raise DomainError("WAITLIST_EXPIRED", "候补匹配已过期，请重新提交")

        if is_expired:
            raise DomainError("WAITLIST_EXPIRED", "候补匹配已过期，请重新提交")

        if entry.status not in (WaitlistStatus.WAITING.value, WaitlistStatus.MATCHED.value):
            raise DomainError(
                "INVALID_STATUS",
                f"当前状态 {entry.status} 不允许确认",
            )

        use_start = entry.matched_start_time or entry.desired_start_time
        use_end = entry.matched_end_time or entry.desired_end_time

        conflicts = self.store.find_conflicting_bookings(
            entry.room_id, use_start, use_end
        )
        if conflicts:
            raise DomainError(
                "BOOKING_CONFLICT",
                f"确认时检测到与 {len(conflicts)} 个预订冲突",
                {"conflicts": [
                    {"booking_id": c.booking_id, "title": c.title,
                     "start_time": c.start_time.isoformat() if c.start_time else None,
                     "end_time": c.end_time.isoformat() if c.end_time else None,
                     "owner_name": c.owner_name, "status": c.status.value}
                    for c in conflicts
                ]},
            )

        attendees = json.loads(entry.attendees) if entry.attendees else []
        booking_cmd = CreateBookingCmd(
            room_id=entry.room_id,
            owner_id=entry.requester_id,
            owner_name=entry.requester_name,
            team_id=entry.team_id,
            title=entry.title,
            start_time=use_start,
            end_time=use_end,
            attendees=attendees,
            description=entry.description,
        )
        handler = CommandHandler(self.db)
        result = handler.create_booking(booking_cmd, actor_id, actor_role, actor_name)
        booking = result["booking"]
        events = result["events"]

        old_status = entry.status
        entry.status = WaitlistStatus.CONFIRMED.value
        entry.confirmed_by_id = cmd.confirmer_id
        entry.confirmed_by_name = cmd.confirmer_name
        entry.confirmed_at = now_utc()
        entry.result_booking_id = booking["booking_id"]
        entry.expire_at = None

        self._write_action_log(
            waitlist_id=entry.waitlist_id,
            action="CONFIRM",
            old_status=old_status,
            new_status=WaitlistStatus.CONFIRMED.value,
            reason=cmd.reason or "候补确认并生成预订",
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role.value,
        )
        self.db.commit()
        self.db.refresh(entry)

        logger.info(
            f"候补已确认: waitlist_id={entry.waitlist_id}, "
            f"生成预订 booking_id={booking['booking_id']}, "
            f"时段={use_start}~{use_end}"
        )

        return {
            "waitlist": self._entry_to_dict(entry),
            "booking": booking,
            "events": events,
        }

    def cancel_waitlist(
        self,
        cmd: CancelWaitlistCmd,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        entry = self._get_entry(cmd.waitlist_id)

        if entry.requester_id != actor_id and not has_permission(actor_role, Permission.MANAGE_WAITLIST):
            raise DomainError("PERMISSION_DENIED", "无权取消他人候补申请")

        if entry.status in (WaitlistStatus.CONFIRMED.value, WaitlistStatus.CANCELLED.value,
                            WaitlistStatus.EXPIRED.value, WaitlistStatus.REJECTED.value):
            raise DomainError(
                "INVALID_STATUS",
                f"当前状态 {entry.status} 不允许取消",
            )

        old_status = entry.status
        entry.status = WaitlistStatus.CANCELLED.value
        entry.expire_at = None

        self._write_action_log(
            waitlist_id=entry.waitlist_id,
            action="CANCEL",
            old_status=old_status,
            new_status=WaitlistStatus.CANCELLED.value,
            reason=cmd.reason or "用户取消候补",
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role.value,
        )
        self.db.commit()
        self.db.refresh(entry)

        logger.info(
            f"候补已取消: waitlist_id={entry.waitlist_id}, "
            f"canceller={cmd.canceller_name}, reason={cmd.reason}"
        )

        return {"waitlist": self._entry_to_dict(entry)}

    def reject_waitlist(
        self,
        cmd: RejectWaitlistCmd,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.MANAGE_WAITLIST):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无驳回候补权限")

        entry = self._get_entry(cmd.waitlist_id)

        if entry.status in (WaitlistStatus.CONFIRMED.value, WaitlistStatus.CANCELLED.value,
                            WaitlistStatus.EXPIRED.value, WaitlistStatus.REJECTED.value):
            raise DomainError(
                "INVALID_STATUS",
                f"当前状态 {entry.status} 不允许驳回",
            )

        old_status = entry.status
        entry.status = WaitlistStatus.REJECTED.value
        entry.expire_at = None

        self._write_action_log(
            waitlist_id=entry.waitlist_id,
            action="REJECT",
            old_status=old_status,
            new_status=WaitlistStatus.REJECTED.value,
            reason=cmd.reason,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role.value,
        )
        self.db.commit()
        self.db.refresh(entry)

        logger.info(
            f"候补已驳回: waitlist_id={entry.waitlist_id}, "
            f"rejecter={cmd.rejecter_name}, reason={cmd.reason}"
        )

        return {"waitlist": self._entry_to_dict(entry)}

    def expire_stale_waitlists(self) -> int:
        now = now_utc()
        q = self.db.query(WaitlistEntry).filter(
            and_(
                WaitlistEntry.status == WaitlistStatus.MATCHED.value,
                WaitlistEntry.expire_at.isnot(None),
            )
        )
        all_candidates = q.all()
        stale = []
        for entry in all_candidates:
            expire = entry.expire_at
            cmp_now = now
            if cmp_now.tzinfo is None and expire and expire.tzinfo is not None:
                cmp_now = cmp_now.replace(tzinfo=expire.tzinfo)
            elif expire and expire.tzinfo is None and cmp_now.tzinfo is not None:
                expire = expire.replace(tzinfo=cmp_now.tzinfo)
            if expire and cmp_now >= expire:
                stale.append(entry)

        count = 0
        for entry in stale:
            old_status = entry.status
            entry.status = WaitlistStatus.EXPIRED.value
            entry.expired_at = now
            entry.expire_reason = "确认超时自动失效"
            self._write_action_log(
                waitlist_id=entry.waitlist_id,
                action="AUTO_EXPIRE",
                old_status=old_status,
                new_status=WaitlistStatus.EXPIRED.value,
                reason="确认超时自动失效",
                actor_id=None,
                actor_name="SYSTEM",
                actor_role=None,
            )
            count += 1
            logger.info(f"候补自动过期: waitlist_id={entry.waitlist_id}")

        if count > 0:
            self.db.commit()

        return count
