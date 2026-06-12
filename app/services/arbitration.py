from __future__ import annotations
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from .event_store import EventStoreService
from .command_handler import now_utc
from ..domain.permissions import BookingStatus, UserRole
from ..domain.aggregate import overlaps
from ..config import settings, get_room_config


class ArbitrationService:
    """冲突裁决与调度建议服务"""

    def __init__(self, db: Session):
        self.db = db
        self.store = EventStoreService(db)

    def analyze_conflicts(
        self,
        room_id: str,
        start: datetime,
        end: datetime,
        proposed_owner_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """分析给定时间窗的冲突情况并给出裁决建议"""
        conflicts = self.store.find_conflicting_bookings(room_id, start, end)

        if not conflicts:
            return {
                "rule_version": settings.rule_version,
                "room_id": room_id,
                "window": {"start": start.isoformat(), "end": end.isoformat()},
                "has_conflict": False,
                "conflict_count": 0,
                "recommendation": "ALLOW",
                "reason": "该时间段无冲突",
                "incumbent": None,
                "affected": [],
            }

        room_cfg = get_room_config(room_id)

        # 优先级规则（高 → 低):
        # 1) 已经签到 > 已批准 > 待审批
        # 2) 优先级相同按创建时间越早越优先（事件版本号小的优先）
        # 3) 团队管理员/系统管理员预订优先于普通成员
        def priority(b):
            status_score = {
                BookingStatus.CHECKED_IN: 0,
                BookingStatus.COMPLETED: 0,
                BookingStatus.APPROVED: 1,
                BookingStatus.PENDING_APPROVAL: 2,
            }.get(b.status, 9)
            return (status_score, b.version or 0)

        sorted_conflicts = sorted(conflicts, key=priority)
        incumbent = sorted_conflicts[0]

        affected = []
        for c in conflicts:
            overlap_start = max(start, c.start_time)
            overlap_end = min(end, c.end_time)
            affected.append({
                "booking_id": c.booking_id,
                "title": c.title,
                "owner_id": c.owner_id,
                "owner_name": c.owner_name,
                "status": c.status.value,
                "start": c.start_time.isoformat() if c.start_time else None,
                "end": c.end_time.isoformat() if c.end_time else None,
                "overlap_minutes": int((overlap_end - overlap_start).total_seconds() / 60),
                "priority_score": priority(c),
            })

        rule_version = settings.rule_version

        # 建议策略：
        # - 如果 incumbent 已签到/已批准，建议 REJECT
        # - 如果 incumbent 待审批且提出者是高权限，可建议 ARBITRATE
        recommendation = "REJECT"
        reason = f"与已有预订 {incumbent.booking_id} 冲突"
        if incumbent.status == BookingStatus.PENDING_APPROVAL:
            recommendation = "ARBITRATE_NEEDED"
            reason = "涉及待审批预订，需系统管理员仲裁"

        return {
            "rule_version": rule_version,
            "room_id": room_id,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "has_conflict": True,
            "conflict_count": len(conflicts),
            "recommendation": recommendation,
            "reason": reason,
            "incumbent": {
                "booking_id": incumbent.booking_id,
                "title": incumbent.title,
                "owner_name": incumbent.owner_name,
                "status": incumbent.status.value,
                "start": incumbent.start_time.isoformat() if incumbent.start_time else None,
                "end": incumbent.end_time.isoformat() if incumbent.end_time else None,
            },
            "affected": affected,
        }

    def suggest_alternative_slots(
        self,
        room_id: str,
        desired_start: datetime,
        desired_end: datetime,
        search_days: int = 7,
        step_minutes: int = 30,
    ) -> List[Dict[str, Any]]:
        """为预订冲突的房间推荐替代时间段"""
        room_cfg = get_room_config(room_id)
        if not room_cfg:
            return []

        duration = desired_end - desired_start
        duration_minutes = int(duration.total_seconds() / 60)
        if duration_minutes < room_cfg.min_duration_minutes:
            duration_minutes = room_cfg.min_duration_minutes
        if duration_minutes > room_cfg.max_duration_minutes:
            duration_minutes = room_cfg.max_duration_minutes
        duration = timedelta(minutes=duration_minutes)

        suggestions = []
        now = now_utc()
        cursor = now.replace(second=0, microsecond=0)
        # 对齐到下一个时间步长
        if cursor.minute % step_minutes:
            cursor = cursor + timedelta(minutes=(step_minutes - cursor.minute % step_minutes))

        window_end = cursor + timedelta(days=search_days)

        while cursor < window_end and len(suggestions) < 10:
            end = cursor + duration
            # 检查是否在可用时间范围内
            day_start = cursor.time()
            day_end = end.time()
            if day_start >= room_cfg.available_from and day_end <= room_cfg.available_to:
                conflicts = self.store.find_conflicting_bookings(room_id, cursor, end)
                if not conflicts:
                    suggestions.append({
                        "room_id": room_id,
                        "start": cursor.isoformat(),
                        "end": end.isoformat(),
                        "duration_minutes": duration_minutes,
                        "from_now_minutes": int((cursor - now).total_seconds() / 60),
                    })
            cursor = cursor + timedelta(minutes=step_minutes)

        return suggestions

    def auto_release_stale(self, actor_id: str, actor_name: str, actor_role: UserRole) -> Dict[str, Any]:
        """自动释放超过签到宽限期且仍未签到的已批准预订"""
        from .command_handler import CommandHandler, DomainError, ReleaseBookingCmd
        from .queries import QueryService
        handler = CommandHandler(self.db)
        query = QueryService(self.db)
        stale = query.find_stale_bookings()
        released = []
        errors = []
        for s in stale:
            booking_id = s["booking_id"]
            cmd = ReleaseBookingCmd(
                booking_id=booking_id,
                released_by_id=actor_id,
                released_by_name=actor_name,
                reason="系统自动释放：未在宽限期内签到",
                expected_version=s["version"],
            )
            try:
                result = handler.release_booking(cmd, actor_id, actor_role, actor_name)
                released.append(result)
            except DomainError as e:
                errors.append({"booking_id": booking_id, "error": e.to_dict()})
        return {
            "rule_version": settings.rule_version,
            "released_count": len(released),
            "error_count": len(errors),
            "released": released,
            "errors": errors,
        }
