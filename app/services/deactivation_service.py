from __future__ import annotations
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import and_
import uuid
import json
import csv
import io
import logging

from .event_store import EventStoreService
from .command_handler import CommandHandler, DomainError, now_utc
from .commands import CancelBookingCmd
from ..config import settings, get_room_config
from ..domain.permissions import (
    UserRole, Permission, BookingStatus,
    DeactivationPlanStatus, DeactivationRecurrenceType, ConflictResolutionAction,
    has_permission,
)
from ..domain.aggregate import BookingAggregate, overlaps
from ..db import (
    DeactivationPlan, DeactivationConflictSnapshot, DeactivationActionLog,
    RescheduleRequest, WaitlistEntry, BulkImportDraft, BulkImportBatch,
)

logger = logging.getLogger(__name__)


def _generate_plan_id() -> str:
    return f"dact-{uuid.uuid4().hex[:12]}"


def _generate_snapshot_id() -> str:
    return f"dcs-{uuid.uuid4().hex[:12]}"


def _generate_log_id() -> str:
    return f"dalog-{uuid.uuid4().hex[:12]}"


class DeactivationService:
    def __init__(self, db: Session):
        self.db = db
        self.store = EventStoreService(db)

    def _write_log(
        self,
        plan_id: str,
        action: str,
        old_status: Optional[str],
        new_status: Optional[str],
        actor_id: Optional[str],
        actor_name: Optional[str],
        actor_role: Optional[str],
        details: Optional[Dict[str, Any]] = None,
        booking_id: Optional[str] = None,
    ) -> None:
        log = DeactivationActionLog(
            log_id=_generate_log_id(),
            plan_id=plan_id,
            action=action,
            old_status=old_status,
            new_status=new_status,
            details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
            booking_id=booking_id,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role.value if isinstance(actor_role, UserRole) else actor_role,
            rule_version=settings.rule_version,
        )
        self.db.add(log)
        self.db.flush()

    def _expand_windows(
        self,
        room_id: str,
        recurrence_type: str,
        window_start: datetime,
        window_end: datetime,
        until_date: Optional[datetime],
        recurrence_rule: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        windows = []
        if recurrence_type == DeactivationRecurrenceType.ONCE.value:
            windows.append({
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
            })
            return windows

        effective_until = until_date or (window_start + timedelta(days=90))
        if effective_until.tzinfo is not None:
            effective_until = effective_until.replace(tzinfo=None)

        current_start = window_start
        current_end = window_end
        duration = window_end - window_start
        max_iterations = 365
        count = 0

        while current_start <= effective_until and count < max_iterations:
            windows.append({
                "start": current_start.isoformat(),
                "end": current_end.isoformat(),
            })

            if recurrence_type == DeactivationRecurrenceType.DAILY.value:
                interval = 1
                if recurrence_rule:
                    interval = recurrence_rule.get("interval", 1)
                current_start = current_start + timedelta(days=interval)
                current_end = current_start + duration
            elif recurrence_type == DeactivationRecurrenceType.WEEKLY.value:
                interval = 1
                if recurrence_rule:
                    interval = recurrence_rule.get("interval", 1)
                current_start = current_start + timedelta(weeks=interval)
                current_end = current_start + duration
            elif recurrence_type == DeactivationRecurrenceType.MONTHLY.value:
                interval = 1
                if recurrence_rule:
                    interval = recurrence_rule.get("interval", 1)
                try:
                    current_start = current_start.replace(month=current_start.month + interval)
                except ValueError:
                    if current_start.month + interval > 12:
                        year = current_start.year + (current_start.month + interval - 1) // 12
                        month = (current_start.month + interval - 1) % 12 + 1
                        current_start = current_start.replace(year=year, month=month)
                current_end = current_start + duration
            else:
                break

            count += 1

        return windows

    def create_plan(
        self,
        room_id: str,
        reason: str,
        impact_scope: Optional[str],
        allow_auto_reschedule: bool,
        recurrence_type: str,
        recurrence_rule: Optional[Dict[str, Any]],
        window_start: datetime,
        window_end: datetime,
        until_date: Optional[datetime],
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.MANAGE_DEACTIVATION):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无停用计划管理权限")

        room_cfg = get_room_config(room_id)
        if not room_cfg:
            raise DomainError("ROOM_NOT_FOUND", f"房间 {room_id} 不存在")

        if window_start >= window_end:
            raise DomainError("INVALID_TIME_RANGE", "开始时间必须早于结束时间")

        if recurrence_type != DeactivationRecurrenceType.ONCE.value and until_date and until_date < window_start:
            raise DomainError("INVALID_UNTIL_DATE", "截止日期不能早于开始时间")

        overlapping = self.db.query(DeactivationPlan).filter(
            and_(
                DeactivationPlan.room_id == room_id,
                DeactivationPlan.status.in_([
                    DeactivationPlanStatus.DRAFT.value,
                    DeactivationPlanStatus.PRECHECKED.value,
                    DeactivationPlanStatus.CONFIRMED.value,
                    DeactivationPlanStatus.PROCESSING.value,
                    DeactivationPlanStatus.PROCESSED.value,
                ]),
            )
        ).all()
        for p in overlapping:
            if overlaps(window_start, window_end, p.window_start, p.window_end):
                raise DomainError(
                    "OVERLAPPING_PLAN",
                    f"该房间已存在重叠的停用计划 {p.plan_id}",
                    {"existing_plan_id": p.plan_id},
                )

        expanded = self._expand_windows(
            room_id, recurrence_type, window_start, window_end, until_date, recurrence_rule,
        )

        plan_id = _generate_plan_id()
        plan = DeactivationPlan(
            plan_id=plan_id,
            room_id=room_id,
            reason=reason,
            impact_scope=impact_scope,
            allow_auto_reschedule=allow_auto_reschedule,
            recurrence_type=recurrence_type,
            recurrence_rule=json.dumps(recurrence_rule, ensure_ascii=False) if recurrence_rule else None,
            window_start=window_start,
            window_end=window_end,
            until_date=until_date,
            status=DeactivationPlanStatus.DRAFT.value,
            version=1,
            creator_id=actor_id,
            creator_name=actor_name,
            creator_role=actor_role.value,
            expanded_windows=json.dumps(expanded, ensure_ascii=False),
            rule_version=settings.rule_version,
        )
        self.db.add(plan)
        self.db.flush()

        self._write_log(
            plan_id=plan_id,
            action="CREATE",
            old_status=None,
            new_status=DeactivationPlanStatus.DRAFT.value,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            details={"room_id": room_id, "recurrence_type": recurrence_type, "windows_count": len(expanded)},
        )
        self.db.commit()
        self.db.refresh(plan)
        return self._plan_to_dict(plan)

    def modify_plan(
        self,
        plan_id: str,
        reason: Optional[str],
        impact_scope: Optional[str],
        allow_auto_reschedule: Optional[bool],
        recurrence_type: Optional[str],
        recurrence_rule: Optional[Dict[str, Any]],
        window_start: Optional[datetime],
        window_end: Optional[datetime],
        until_date: Optional[datetime],
        expected_version: int,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.MANAGE_DEACTIVATION):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无停用计划管理权限")

        plan = self._get_plan(plan_id)
        if plan.status not in (DeactivationPlanStatus.DRAFT.value, DeactivationPlanStatus.PRECHECKED.value):
            raise DomainError("INVALID_STATUS", f"当前状态 {plan.status} 不允许修改")

        if plan.version != expected_version:
            raise DomainError(
                "VERSION_CONFLICT",
                f"计划版本不匹配: 期望 {expected_version}, 实际 {plan.version}",
                {"expected": expected_version, "actual": plan.version},
            )

        old_status = plan.status
        new_window_start = window_start or plan.window_start
        new_window_end = window_end or plan.window_end
        new_recurrence_type = recurrence_type or plan.recurrence_type
        new_recurrence_rule = recurrence_rule if recurrence_rule is not None else (json.loads(plan.recurrence_rule) if plan.recurrence_rule else None)
        new_until_date = until_date if until_date is not None else plan.until_date

        if new_window_start >= new_window_end:
            raise DomainError("INVALID_TIME_RANGE", "开始时间必须早于结束时间")

        if reason is not None:
            plan.reason = reason
        if impact_scope is not None:
            plan.impact_scope = impact_scope
        if allow_auto_reschedule is not None:
            plan.allow_auto_reschedule = allow_auto_reschedule
        plan.recurrence_type = new_recurrence_type
        plan.recurrence_rule = json.dumps(new_recurrence_rule, ensure_ascii=False) if new_recurrence_rule else None
        plan.window_start = new_window_start
        plan.window_end = new_window_end
        plan.until_date = new_until_date
        plan.version += 1

        expanded = self._expand_windows(
            plan.room_id, new_recurrence_type, new_window_start, new_window_end, new_until_date, new_recurrence_rule,
        )
        plan.expanded_windows = json.dumps(expanded, ensure_ascii=False)
        plan.status = DeactivationPlanStatus.DRAFT.value
        self.db.flush()

        self._write_log(
            plan_id=plan_id,
            action="MODIFY",
            old_status=old_status,
            new_status=plan.status,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            details={"new_version": plan.version, "windows_count": len(expanded)},
        )
        self.db.commit()
        return self._plan_to_dict(plan)

    def precheck_plan(
        self,
        plan_id: str,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.MANAGE_DEACTIVATION):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无停用计划管理权限")

        plan = self._get_plan(plan_id)
        if plan.status not in (DeactivationPlanStatus.DRAFT.value, DeactivationPlanStatus.PRECHECKED.value):
            raise DomainError("INVALID_STATUS", f"当前状态 {plan.status} 不允许预检")

        expanded = json.loads(plan.expanded_windows) if plan.expanded_windows else []

        booking_conflicts = self._find_booking_conflicts(plan, expanded)
        reschedule_conflicts = self._find_reschedule_conflicts(plan, expanded)
        waitlist_conflicts = self._find_waitlist_conflicts(plan, expanded)
        bulk_import_conflicts = self._find_bulk_import_conflicts(plan, expanded)

        total = len(booking_conflicts) + len(reschedule_conflicts) + len(waitlist_conflicts) + len(bulk_import_conflicts)

        old_status = plan.status
        plan.status = DeactivationPlanStatus.PRECHECKED.value
        plan.precheck_at = now_utc()
        plan.total_conflicts = len(booking_conflicts)
        plan.pending_conflicts = len(booking_conflicts)
        plan.resolved_conflicts = 0
        self.db.flush()

        self._write_log(
            plan_id=plan_id,
            action="PRECHECK",
            old_status=old_status,
            new_status=DeactivationPlanStatus.PRECHECKED.value,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            details={
                "booking_conflicts": len(booking_conflicts),
                "reschedule_conflicts": len(reschedule_conflicts),
                "waitlist_conflicts": len(waitlist_conflicts),
                "bulk_import_conflicts": len(bulk_import_conflicts),
                "expanded_windows": len(expanded),
            },
        )
        self.db.commit()

        return {
            "success": True,
            "plan_id": plan_id,
            "conflict_count": total,
            "booking_conflicts": booking_conflicts,
            "reschedule_request_conflicts": reschedule_conflicts,
            "waitlist_conflicts": waitlist_conflicts,
            "bulk_import_conflicts": bulk_import_conflicts,
            "expanded_windows": expanded,
            "rule_version": settings.rule_version,
        }

    def confirm_plan(
        self,
        plan_id: str,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.MANAGE_DEACTIVATION):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无停用计划管理权限")

        plan = self._get_plan(plan_id)
        if plan.status != DeactivationPlanStatus.PRECHECKED.value:
            if plan.status == DeactivationPlanStatus.DRAFT.value:
                self.precheck_plan(plan_id, actor_id, actor_role, actor_name)
                plan = self._get_plan(plan_id)
            else:
                raise DomainError("INVALID_STATUS", f"当前状态 {plan.status} 不允许确认，需先预检")

        expanded = json.loads(plan.expanded_windows) if plan.expanded_windows else []
        booking_conflicts = self._find_booking_conflicts(plan, expanded)

        self._create_conflict_snapshots(plan, booking_conflicts)

        old_status = plan.status
        plan.status = DeactivationPlanStatus.PROCESSING.value
        plan.confirmed_at = now_utc()
        plan.processing_started_at = now_utc()
        plan.total_conflicts = len(booking_conflicts)
        plan.pending_conflicts = len(booking_conflicts)
        plan.resolved_conflicts = 0
        self.db.flush()

        self._write_log(
            plan_id=plan_id,
            action="CONFIRM",
            old_status=old_status,
            new_status=DeactivationPlanStatus.PROCESSING.value,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            details={"total_conflicts": plan.total_conflicts},
        )
        self.db.commit()

        return {
            "success": True,
            "plan_id": plan_id,
            "status": plan.status,
            "total_conflicts": plan.total_conflicts,
            "rule_version": settings.rule_version,
        }

    def batch_resolve(
        self,
        plan_id: str,
        resolutions: List[Dict[str, Any]],
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.MANAGE_DEACTIVATION):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无停用计划管理权限")

        plan = self._get_plan(plan_id)
        if plan.status not in (
            DeactivationPlanStatus.PROCESSING.value,
            DeactivationPlanStatus.CONFIRMED.value,
        ):
            raise DomainError("INVALID_STATUS", f"当前状态 {plan.status} 不允许批量处理冲突")

        if plan.status == DeactivationPlanStatus.CONFIRMED.value:
            plan.status = DeactivationPlanStatus.PROCESSING.value
            plan.processing_started_at = now_utc()
            self.db.flush()

        resolved = 0
        skipped = 0
        failed = 0
        results = []

        for r in resolutions:
            booking_id = r.get("booking_id")
            action = r.get("action")
            reason = r.get("reason", "")

            snapshot = self.db.query(DeactivationConflictSnapshot).filter(
                and_(
                    DeactivationConflictSnapshot.plan_id == plan_id,
                    DeactivationConflictSnapshot.booking_id == booking_id,
                    DeactivationConflictSnapshot.resolution == ConflictResolutionAction.PENDING.value,
                )
            ).first()

            if not snapshot:
                skipped += 1
                results.append({"booking_id": booking_id, "action": action, "status": "skipped", "reason": "冲突记录不存在或已处理"})
                continue

            try:
                if action == ConflictResolutionAction.CANCEL.value:
                    self._resolve_cancel(plan, snapshot, actor_id, actor_role, actor_name, reason)
                    resolved += 1
                    results.append({"booking_id": booking_id, "action": "cancel", "status": "resolved"})
                elif action == ConflictResolutionAction.RESCHEDULE.value:
                    suggested_start = r.get("suggested_start")
                    suggested_end = r.get("suggested_end")
                    self._resolve_reschedule(plan, snapshot, actor_id, actor_role, actor_name, reason, suggested_start, suggested_end)
                    resolved += 1
                    results.append({"booking_id": booking_id, "action": "reschedule", "status": "resolved"})
                elif action == ConflictResolutionAction.SKIP.value:
                    snapshot.resolution = ConflictResolutionAction.SKIP.value
                    snapshot.resolved_by_id = actor_id
                    snapshot.resolved_by_name = actor_name
                    snapshot.resolved_at = now_utc()
                    snapshot.resolution_reason = reason or "管理员跳过"
                    skipped += 1
                    results.append({"booking_id": booking_id, "action": "skip", "status": "skipped"})
                else:
                    failed += 1
                    results.append({"booking_id": booking_id, "action": action, "status": "failed", "reason": f"不支持的操作 {action}"})

                self._write_log(
                    plan_id=plan_id,
                    action=f"RESOLVE_{action.upper()}",
                    old_status=snapshot.resolution,
                    new_status=action,
                    actor_id=actor_id,
                    actor_name=actor_name,
                    actor_role=actor_role,
                    details={"booking_id": booking_id, "reason": reason},
                    booking_id=booking_id,
                )
            except DomainError as e:
                failed += 1
                results.append({"booking_id": booking_id, "action": action, "status": "failed", "reason": e.message})
            except Exception as e:
                logger.exception(f"处理冲突失败 booking_id={booking_id}")
                failed += 1
                results.append({"booking_id": booking_id, "action": action, "status": "failed", "reason": str(e)})

        pending_count = self.db.query(DeactivationConflictSnapshot).filter(
            and_(
                DeactivationConflictSnapshot.plan_id == plan_id,
                DeactivationConflictSnapshot.resolution == ConflictResolutionAction.PENDING.value,
            )
        ).count()
        resolved_count = self.db.query(DeactivationConflictSnapshot).filter(
            and_(
                DeactivationConflictSnapshot.plan_id == plan_id,
                DeactivationConflictSnapshot.resolution != ConflictResolutionAction.PENDING.value,
            )
        ).count()

        plan.pending_conflicts = pending_count
        plan.resolved_conflicts = resolved_count

        if pending_count == 0:
            plan.status = DeactivationPlanStatus.PROCESSED.value
            plan.processed_at = now_utc()
            self._write_log(
                plan_id=plan_id,
                action="ALL_RESOLVED",
                old_status=DeactivationPlanStatus.PROCESSING.value,
                new_status=DeactivationPlanStatus.PROCESSED.value,
                actor_id=actor_id,
                actor_name=actor_name,
                actor_role=actor_role,
            )

        self.db.flush()
        self.db.commit()

        return {
            "success": True,
            "plan_id": plan_id,
            "total": len(resolutions),
            "resolved": resolved,
            "skipped": skipped,
            "failed": failed,
            "results": results,
            "rule_version": settings.rule_version,
        }

    def _resolve_cancel(
        self,
        plan: DeactivationPlan,
        snapshot: DeactivationConflictSnapshot,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
        reason: str,
    ) -> None:
        agg = self.store.load_aggregate(snapshot.booking_id)
        if agg.version == 0:
            snapshot.resolution = ConflictResolutionAction.SKIP.value
            snapshot.resolution_reason = "预订已不存在"
            snapshot.resolved_by_id = actor_id
            snapshot.resolved_by_name = actor_name
            snapshot.resolved_at = now_utc()
            return

        if agg.status in (BookingStatus.CANCELLED, BookingStatus.REJECTED, BookingStatus.COMPLETED):
            snapshot.resolution = ConflictResolutionAction.SKIP.value
            snapshot.resolution_reason = f"预订状态为 {agg.status.value}，无需取消"
            snapshot.resolved_by_id = actor_id
            snapshot.resolved_by_name = actor_name
            snapshot.resolved_at = now_utc()
            return

        handler = CommandHandler(self.db)
        cmd = CancelBookingCmd(
            booking_id=snapshot.booking_id,
            canceller_id=actor_id,
            canceller_name=actor_name,
            reason=f"停用计划 {plan.plan_id}: {reason or plan.reason}",
            expected_version=agg.version,
        )
        handler.cancel_booking(cmd, actor_id, actor_role, actor_name)

        snapshot.resolution = ConflictResolutionAction.CANCEL.value
        snapshot.resolved_by_id = actor_id
        snapshot.resolved_by_name = actor_name
        snapshot.resolved_at = now_utc()
        snapshot.resolution_reason = reason or "停用计划取消"
        self.db.flush()

    def _resolve_reschedule(
        self,
        plan: DeactivationPlan,
        snapshot: DeactivationConflictSnapshot,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
        reason: str,
        suggested_start: Optional[str],
        suggested_end: Optional[str],
    ) -> None:
        suggestion = None
        if suggested_start and suggested_end:
            suggestion = {
                "suggested_start": suggested_start,
                "suggested_end": suggested_end,
                "suggested_room_id": plan.room_id,
            }

        snapshot.resolution = ConflictResolutionAction.RESCHEDULE.value
        snapshot.resolved_by_id = actor_id
        snapshot.resolved_by_name = actor_name
        snapshot.resolved_at = now_utc()
        snapshot.resolution_reason = reason or "建议改期"
        snapshot.reschedule_suggestion = json.dumps(suggestion, ensure_ascii=False) if suggestion else None
        self.db.flush()

    def revoke_plan(
        self,
        plan_id: str,
        reason: Optional[str],
        expected_version: int,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.MANAGE_DEACTIVATION):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无停用计划管理权限")

        plan = self._get_plan(plan_id)
        if plan.status in (DeactivationPlanStatus.REVOKED.value, DeactivationPlanStatus.CANCELLED.value):
            raise DomainError("INVALID_STATUS", f"计划已处于 {plan.status} 状态")

        if plan.version != expected_version:
            raise DomainError(
                "VERSION_CONFLICT",
                f"计划版本不匹配: 期望 {expected_version}, 实际 {plan.version}，可能已被其他管理员处理",
                {"expected": expected_version, "actual": plan.version},
            )

        snapshots = self.db.query(DeactivationConflictSnapshot).filter(
            and_(
                DeactivationConflictSnapshot.plan_id == plan_id,
                DeactivationConflictSnapshot.resolution == ConflictResolutionAction.CANCEL.value,
            )
        ).all()

        for snap in snapshots:
            agg = self.store.load_aggregate(snap.booking_id)
            if agg.version > snap.booking_version:
                raise DomainError(
                    "VERSION_CONFLICT",
                    f"预约 {snap.booking_id} 在停用计划处理后版本已变化(快照版本={snap.booking_version}, 当前版本={agg.version})，不能覆盖他人操作",
                    {
                        "booking_id": snap.booking_id,
                        "snapshot_version": snap.booking_version,
                        "current_version": agg.version,
                    },
                )

        old_status = plan.status
        plan.status = DeactivationPlanStatus.REVOKED.value
        plan.revoked_at = now_utc()
        plan.revoker_id = actor_id
        plan.revoker_name = actor_name
        plan.version += 1

        self._write_log(
            plan_id=plan_id,
            action="REVOKE",
            old_status=old_status,
            new_status=DeactivationPlanStatus.REVOKED.value,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            details={"reason": reason, "cancelled_bookings_to_restore": len(snapshots)},
        )
        self.db.commit()

        return self._plan_to_dict(plan)

    def get_plan(self, plan_id: str, actor_id: str, actor_role: UserRole) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.VIEW_DEACTIVATION):
            raise DomainError("PERMISSION_DENIED", "无查看停用计划权限")
        plan = self._get_plan(plan_id)
        return self._plan_to_dict(plan)

    def list_plans(
        self,
        actor_id: str,
        actor_role: UserRole,
        room_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.VIEW_DEACTIVATION):
            raise DomainError("PERMISSION_DENIED", "无查看停用计划权限")

        q = self.db.query(DeactivationPlan)
        if room_id:
            q = q.filter(DeactivationPlan.room_id == room_id)
        if status:
            status_list = [s.strip() for s in status.split(",") if s.strip()]
            if status_list:
                q = q.filter(DeactivationPlan.status.in_(status_list))

        total = q.count()
        items = q.order_by(DeactivationPlan.created_at.desc()).offset(offset).limit(limit).all()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "rule_version": settings.rule_version,
            "items": [self._plan_to_dict(p) for p in items],
        }

    def list_conflicts(
        self,
        plan_id: str,
        resolution: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        plan = self._get_plan(plan_id)
        q = self.db.query(DeactivationConflictSnapshot).filter(
            DeactivationConflictSnapshot.plan_id == plan_id,
        )
        if resolution:
            q = q.filter(DeactivationConflictSnapshot.resolution == resolution)

        total = q.count()
        items = q.order_by(DeactivationConflictSnapshot.created_at.asc()).offset(offset).limit(limit).all()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "rule_version": settings.rule_version,
            "items": [self._snapshot_to_dict(s) for s in items],
        }

    def list_logs(
        self,
        plan_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        plan = self._get_plan(plan_id)
        q = self.db.query(DeactivationActionLog).filter(
            DeactivationActionLog.plan_id == plan_id,
        )
        total = q.count()
        items = q.order_by(DeactivationActionLog.created_at.asc()).offset(offset).limit(limit).all()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "rule_version": settings.rule_version,
            "items": [self._action_log_to_dict(l) for l in items],
        }

    def export_affected(
        self,
        plan_id: str,
        format: str = "csv",
    ) -> Dict[str, Any]:
        plan = self._get_plan(plan_id)
        snapshots = self.db.query(DeactivationConflictSnapshot).filter(
            DeactivationConflictSnapshot.plan_id == plan_id,
        ).order_by(DeactivationConflictSnapshot.created_at.asc()).all()

        header = [
            "冲突ID", "预约ID", "房间ID", "预约人ID", "预约人姓名", "标题",
            "预约开始时间", "预约结束时间", "预约状态", "冲突类型",
            "冲突窗口开始", "冲突窗口结束", "处理方式", "处理人ID",
            "处理人姓名", "处理时间", "处理原因",
        ]

        rows = []
        for s in snapshots:
            rows.append([
                s.snapshot_id,
                s.booking_id,
                s.booking_room_id,
                s.booking_owner_id,
                s.booking_owner_name or "",
                s.booking_title or "",
                s.booking_start_time.isoformat() if s.booking_start_time else "",
                s.booking_end_time.isoformat() if s.booking_end_time else "",
                s.booking_status,
                s.conflict_type,
                s.conflict_window_start.isoformat() if s.conflict_window_start else "",
                s.conflict_window_end.isoformat() if s.conflict_window_end else "",
                s.resolution,
                s.resolved_by_id or "",
                s.resolved_by_name or "",
                s.resolved_at.isoformat() if s.resolved_at else "",
                s.resolution_reason or "",
            ])

        if format == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(header)
            for r in rows:
                writer.writerow(r)
            return {
                "rule_version": settings.rule_version,
                "format": "csv",
                "plan_id": plan_id,
                "row_count": len(rows),
                "content": buf.getvalue(),
                "header": header,
            }
        else:
            json_items = [dict(zip(header, r)) for r in rows]
            return {
                "rule_version": settings.rule_version,
                "format": "json",
                "plan_id": plan_id,
                "row_count": len(json_items),
                "items": json_items,
            }

    def recover_incomplete_plans(self) -> int:
        processing_plans = self.db.query(DeactivationPlan).filter(
            DeactivationPlan.status == DeactivationPlanStatus.PROCESSING.value,
        ).all()

        recovered = 0
        for plan in processing_plans:
            pending_count = self.db.query(DeactivationConflictSnapshot).filter(
                and_(
                    DeactivationConflictSnapshot.plan_id == plan.plan_id,
                    DeactivationConflictSnapshot.resolution == ConflictResolutionAction.PENDING.value,
                )
            ).count()

            if pending_count == 0:
                resolved_count = self.db.query(DeactivationConflictSnapshot).filter(
                    and_(
                        DeactivationConflictSnapshot.plan_id == plan.plan_id,
                        DeactivationConflictSnapshot.resolution != ConflictResolutionAction.PENDING.value,
                    )
                ).count()

                plan.status = DeactivationPlanStatus.PROCESSED.value
                plan.processed_at = now_utc()
                plan.pending_conflicts = 0
                plan.resolved_conflicts = resolved_count

                self._write_log(
                    plan_id=plan.plan_id,
                    action="RECOVERY_COMPLETE",
                    old_status=DeactivationPlanStatus.PROCESSING.value,
                    new_status=DeactivationPlanStatus.PROCESSED.value,
                    actor_id=None,
                    actor_name="SYSTEM",
                    actor_role=None,
                    details={"recovered": True},
                )
            else:
                self._write_log(
                    plan_id=plan.plan_id,
                    action="RECOVERY_CONTINUE",
                    old_status=DeactivationPlanStatus.PROCESSING.value,
                    new_status=DeactivationPlanStatus.PROCESSING.value,
                    actor_id=None,
                    actor_name="SYSTEM",
                    actor_role=None,
                    details={"pending_conflicts": pending_count},
                )

            recovered += 1

        if recovered > 0:
            self.db.commit()

        return recovered

    def _get_plan(self, plan_id: str) -> DeactivationPlan:
        plan = self.db.query(DeactivationPlan).filter(DeactivationPlan.plan_id == plan_id).first()
        if not plan:
            raise DomainError("PLAN_NOT_FOUND", f"停用计划 {plan_id} 不存在")
        return plan

    def _find_booking_conflicts(
        self,
        plan: DeactivationPlan,
        expanded_windows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        all_bookings = self.store.get_all_active_bookings()
        conflicts = []
        for b in all_bookings:
            if b.room_id != plan.room_id:
                continue
            if b.status in (BookingStatus.CANCELLED, BookingStatus.REJECTED, BookingStatus.RELEASED, BookingStatus.COMPLETED):
                continue
            for w in expanded_windows:
                w_start = datetime.fromisoformat(w["start"]) if isinstance(w["start"], str) else w["start"]
                w_end = datetime.fromisoformat(w["end"]) if isinstance(w["end"], str) else w["end"]
                if b.start_time and b.end_time and overlaps(w_start, w_end, b.start_time, b.end_time):
                    conflicts.append({
                        "booking_id": b.booking_id,
                        "room_id": b.room_id,
                        "owner_id": b.owner_id,
                        "owner_name": b.owner_name,
                        "title": b.title,
                        "start_time": b.start_time.isoformat(),
                        "end_time": b.end_time.isoformat(),
                        "status": b.status.value,
                        "version": b.version,
                        "conflict_window_start": w["start"],
                        "conflict_window_end": w["end"],
                    })
                    break
        return conflicts

    def _find_reschedule_conflicts(
        self,
        plan: DeactivationPlan,
        expanded_windows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        pending_requests = self.db.query(RescheduleRequest).filter(
            and_(
                RescheduleRequest.status == "pending",
                RescheduleRequest.new_room_id == plan.room_id,
            )
        ).all()

        conflicts = []
        for req in pending_requests:
            for w in expanded_windows:
                w_start = datetime.fromisoformat(w["start"]) if isinstance(w["start"], str) else w["start"]
                w_end = datetime.fromisoformat(w["end"]) if isinstance(w["end"], str) else w["end"]
                if req.new_start_time and req.new_end_time and overlaps(w_start, w_end, req.new_start_time, req.new_end_time):
                    conflicts.append({
                        "request_id": req.request_id,
                        "booking_id": req.booking_id,
                        "requester_name": req.requester_name,
                        "new_room_id": req.new_room_id,
                        "new_start_time": req.new_start_time.isoformat(),
                        "new_end_time": req.new_end_time.isoformat(),
                        "status": req.status,
                    })
                    break
        return conflicts

    def _find_waitlist_conflicts(
        self,
        plan: DeactivationPlan,
        expanded_windows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        active_waitlists = self.db.query(WaitlistEntry).filter(
            and_(
                WaitlistEntry.room_id == plan.room_id,
                WaitlistEntry.status.in_(["waiting", "matched"]),
            )
        ).all()

        conflicts = []
        for wl in active_waitlists:
            for w in expanded_windows:
                w_start = datetime.fromisoformat(w["start"]) if isinstance(w["start"], str) else w["start"]
                w_end = datetime.fromisoformat(w["end"]) if isinstance(w["end"], str) else w["end"]
                if wl.desired_start_time and wl.desired_end_time and overlaps(w_start, w_end, wl.desired_start_time, wl.desired_end_time):
                    conflicts.append({
                        "waitlist_id": wl.waitlist_id,
                        "requester_name": wl.requester_name,
                        "room_id": wl.room_id,
                        "desired_start_time": wl.desired_start_time.isoformat(),
                        "desired_end_time": wl.desired_end_time.isoformat(),
                        "status": wl.status,
                    })
                    break
        return conflicts

    def _find_bulk_import_conflicts(
        self,
        plan: DeactivationPlan,
        expanded_windows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        active_batches = self.db.query(BulkImportBatch).filter(
            BulkImportBatch.status.in_(["draft", "prechecked", "prechecking", "confirming"]),
        ).all()

        if not active_batches:
            return []

        batch_ids = [b.batch_id for b in active_batches]
        drafts = self.db.query(BulkImportDraft).filter(
            and_(
                BulkImportDraft.batch_id.in_(batch_ids),
                BulkImportDraft.room_id == plan.room_id,
            )
        ).all()

        conflicts = []
        for d in drafts:
            if not d.start_time or not d.end_time:
                continue
            for w in expanded_windows:
                w_start = datetime.fromisoformat(w["start"]) if isinstance(w["start"], str) else w["start"]
                w_end = datetime.fromisoformat(w["end"]) if isinstance(w["end"], str) else w["end"]
                if overlaps(w_start, w_end, d.start_time, d.end_time):
                    conflicts.append({
                        "batch_id": d.batch_id,
                        "draft_index": d.draft_index,
                        "room_id": d.room_id,
                        "owner_name": d.owner_name,
                        "title": d.title,
                        "start_time": d.start_time.isoformat(),
                        "end_time": d.end_time.isoformat(),
                    })
                    break
        return conflicts

    def _create_conflict_snapshots(
        self,
        plan: DeactivationPlan,
        booking_conflicts: List[Dict[str, Any]],
    ) -> None:
        for c in booking_conflicts:
            snap = DeactivationConflictSnapshot(
                snapshot_id=_generate_snapshot_id(),
                plan_id=plan.plan_id,
                booking_id=c["booking_id"],
                booking_room_id=c["room_id"],
                booking_owner_id=c["owner_id"],
                booking_owner_name=c.get("owner_name"),
                booking_title=c.get("title"),
                booking_start_time=datetime.fromisoformat(c["start_time"]) if isinstance(c["start_time"], str) else c["start_time"],
                booking_end_time=datetime.fromisoformat(c["end_time"]) if isinstance(c["end_time"], str) else c["end_time"],
                booking_status=c["status"],
                booking_version=c["version"],
                conflict_type="booking",
                conflict_window_start=datetime.fromisoformat(c["conflict_window_start"]) if isinstance(c["conflict_window_start"], str) else c["conflict_window_start"],
                conflict_window_end=datetime.fromisoformat(c["conflict_window_end"]) if isinstance(c["conflict_window_end"], str) else c["conflict_window_end"],
                resolution=ConflictResolutionAction.PENDING.value,
                plan_version_at_snapshot=plan.version,
                rule_version=settings.rule_version,
            )
            self.db.add(snap)
        self.db.flush()

    def _plan_to_dict(self, plan: DeactivationPlan) -> Dict[str, Any]:
        expanded = json.loads(plan.expanded_windows) if plan.expanded_windows else []
        recurrence_rule = json.loads(plan.recurrence_rule) if plan.recurrence_rule else None
        return {
            "plan_id": plan.plan_id,
            "room_id": plan.room_id,
            "reason": plan.reason,
            "impact_scope": plan.impact_scope,
            "allow_auto_reschedule": plan.allow_auto_reschedule,
            "recurrence_type": plan.recurrence_type,
            "recurrence_rule": recurrence_rule,
            "window_start": plan.window_start.isoformat() if plan.window_start else None,
            "window_end": plan.window_end.isoformat() if plan.window_end else None,
            "until_date": plan.until_date.isoformat() if plan.until_date else None,
            "status": plan.status,
            "version": plan.version,
            "creator_id": plan.creator_id,
            "creator_name": plan.creator_name,
            "creator_role": plan.creator_role,
            "precheck_at": plan.precheck_at.isoformat() if plan.precheck_at else None,
            "confirmed_at": plan.confirmed_at.isoformat() if plan.confirmed_at else None,
            "processing_started_at": plan.processing_started_at.isoformat() if plan.processing_started_at else None,
            "processed_at": plan.processed_at.isoformat() if plan.processed_at else None,
            "revoked_at": plan.revoked_at.isoformat() if plan.revoked_at else None,
            "revoker_id": plan.revoker_id,
            "revoker_name": plan.revoker_name,
            "expanded_windows": expanded,
            "total_conflicts": plan.total_conflicts,
            "resolved_conflicts": plan.resolved_conflicts,
            "pending_conflicts": plan.pending_conflicts,
            "rule_version": settings.rule_version,
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
            "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        }

    def _snapshot_to_dict(self, snap: DeactivationConflictSnapshot) -> Dict[str, Any]:
        reschedule_suggestion = json.loads(snap.reschedule_suggestion) if snap.reschedule_suggestion else None
        return {
            "snapshot_id": snap.snapshot_id,
            "plan_id": snap.plan_id,
            "booking_id": snap.booking_id,
            "booking_room_id": snap.booking_room_id,
            "booking_owner_id": snap.booking_owner_id,
            "booking_owner_name": snap.booking_owner_name,
            "booking_title": snap.booking_title,
            "booking_start_time": snap.booking_start_time.isoformat() if snap.booking_start_time else None,
            "booking_end_time": snap.booking_end_time.isoformat() if snap.booking_end_time else None,
            "booking_status": snap.booking_status,
            "booking_version": snap.booking_version,
            "conflict_type": snap.conflict_type,
            "conflict_window_start": snap.conflict_window_start.isoformat() if snap.conflict_window_start else None,
            "conflict_window_end": snap.conflict_window_end.isoformat() if snap.conflict_window_end else None,
            "resolution": snap.resolution,
            "resolved_by_id": snap.resolved_by_id,
            "resolved_by_name": snap.resolved_by_name,
            "resolved_at": snap.resolved_at.isoformat() if snap.resolved_at else None,
            "resolution_reason": snap.resolution_reason,
            "reschedule_suggestion": reschedule_suggestion,
            "rule_version": settings.rule_version,
            "created_at": snap.created_at.isoformat() if snap.created_at else None,
        }

    def _action_log_to_dict(self, log: DeactivationActionLog) -> Dict[str, Any]:
        return {
            "log_id": log.log_id,
            "plan_id": log.plan_id,
            "action": log.action,
            "old_status": log.old_status,
            "new_status": log.new_status,
            "details": json.loads(log.details) if log.details else None,
            "booking_id": log.booking_id,
            "actor_id": log.actor_id,
            "actor_name": log.actor_name,
            "actor_role": log.actor_role,
            "created_at": log.created_at.isoformat() if log.created_at else None,
            "rule_version": settings.rule_version,
        }
