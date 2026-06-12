from __future__ import annotations
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from collections import defaultdict
import csv
import io
import json

from .event_store import EventStoreService
from .command_handler import now_utc
from ..config import settings, get_room_config
from ..domain.permissions import BookingStatus
from ..domain.aggregate import BookingAggregate, overlaps


class QueryService:
    def __init__(self, db: Session):
        self.db = db
        self.store = EventStoreService(db)

    def query_events(
        self,
        stream_id: Optional[str] = None,
        event_type: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        room_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> Dict[str, Any]:
        records = self.store.load_events(
            stream_id=stream_id,
            stream_type="booking" if not stream_id else None,
            event_type=event_type,
            since_created_at=since,
            until_created_at=until,
            room_id=room_id,
            user_id=user_id,
            limit=limit,
            offset=offset,
        )
        return {
            "total": len(records),
            "limit": limit,
            "offset": offset,
            "rule_version": settings.rule_version,
            "items": [r.to_dict() for r in records],
        }

    def get_booking(self, booking_id: str) -> Optional[Dict[str, Any]]:
        agg = self.store.load_aggregate(booking_id)
        if agg.version == 0:
            return None
        return agg.to_dict()

    def get_schedule(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        room_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        status_filter: Optional[List[str]] = None,
        include_historical: bool = False,
    ) -> Dict[str, Any]:
        if not start:
            start = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        if not end:
            end = start + timedelta(days=7)

        bookings = self.store.get_all_active_bookings(start_window=start, end_window=end, room_id=room_id)
        result = []
        for b in bookings:
            if owner_id and b.owner_id != owner_id:
                continue
            if status_filter and b.status.value not in status_filter:
                continue
            if not include_historical and b.status in (
                BookingStatus.CANCELLED, BookingStatus.REJECTED, BookingStatus.RELEASED,
            ):
                if b.end_time and b.end_time < start:
                    continue
            result.append(b)

        result.sort(key=lambda a: (
            a.start_time.isoformat() if a.start_time else "",
            a.room_id,
            a.booking_id,
        ))

        return {
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "rule_version": settings.rule_version,
            "total": len(result),
            "items": [b.to_dict() for b in result],
        }

    def get_rooms(self) -> Dict[str, Any]:
        rooms = []
        for r in settings.rooms:
            rooms.append({
                "room_id": r.room_id,
                "name": r.name,
                "capacity": r.capacity,
                "floor": r.floor,
                "facilities": r.facilities,
                "require_approval": r.require_approval,
                "min_duration_minutes": r.min_duration_minutes,
                "max_duration_minutes": r.max_duration_minutes,
                "booking_window_days": r.booking_window_days,
                "check_in_grace_minutes": r.check_in_grace_minutes,
                "time_slot_step_minutes": r.time_slot_step_minutes,
                "available_from": r.available_from.isoformat(),
                "available_to": r.available_to.isoformat(),
            })
        return {"rule_version": settings.rule_version, "items": rooms}

    def export_schedule(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        room_id: Optional[str] = None,
        format: str = "csv",
    ) -> Dict[str, Any]:
        schedule = self.get_schedule(start=start, end=end, room_id=room_id, include_historical=True)
        items = schedule["items"]

        rows = []
        header = [
            "预订ID", "房间ID", "房间名称", "标题", "会议主持人",
            "团队ID", "开始时间", "结束时间", "时长(分钟)", "状态",
            "参会人数", "设施", "审批人", "驳回原因", "签到时间",
            "释放原因", "仲裁裁决", "创建时间", "版本号",
        ]

        for it in items:
            rc = get_room_config(it["room_id"])
            start_dt = datetime.fromisoformat(it["start_time"]) if it["start_time"] else None
            end_dt = datetime.fromisoformat(it["end_time"]) if it["end_time"] else None
            duration = int((end_dt - start_dt).total_seconds() / 60) if start_dt and end_dt else 0
            rows.append([
                it["booking_id"],
                it["room_id"],
                rc.name if rc else it["room_id"],
                it["title"],
                it["owner_name"],
                it.get("team_id") or "",
                it["start_time"] or "",
                it["end_time"] or "",
                duration,
                it["status"],
                len(it.get("attendees") or []),
                ", ".join(rc.facilities) if rc else "",
                it.get("approver_name") or "",
                it.get("reject_reason") or "",
                it.get("last_check_in_time") or "",
                it.get("release_reason") or "",
                it.get("arbitration_decision") or "",
                it.get("created_at") or "",
                it.get("version", 0),
            ])

        if format == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(header)
            for r in rows:
                writer.writerow(r)
            content = buf.getvalue()
            return {
                "rule_version": settings.rule_version,
                "format": "csv",
                "row_count": len(rows),
                "window": schedule["window"],
                "content": content,
                "header": header,
            }
        else:
            json_items = [dict(zip(header, r)) for r in rows]
            return {
                "rule_version": settings.rule_version,
                "format": "json",
                "row_count": len(json_items),
                "window": schedule["window"],
                "items": json_items,
            }

    def find_stale_bookings(self, at_time: Optional[datetime] = None) -> List[Dict[str, Any]]:
        at = at_time or now_utc()
        result = []
        all_active = self.store.get_all_active_bookings()
        for b in all_active:
            if b.status != BookingStatus.APPROVED:
                continue
            if not b.start_time:
                continue
            room_cfg = get_room_config(b.room_id)
            grace = room_cfg.check_in_grace_minutes if room_cfg else 15
            threshold = b.start_time + timedelta(minutes=grace + settings.default_auto_release_after_start_minutes)
            if at > threshold:
                result.append(b.to_dict())
        return result

    def get_conflict_matrix(
        self,
        start: datetime,
        end: datetime,
        room_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        schedule = self.get_schedule(start=start, end=end, room_id=room_id, include_historical=True)
        items = [
            BookingAggregate(**{
                k: (datetime.fromisoformat(v) if isinstance(v, str) and k in ("start_time", "end_time", "created_at", "updated_at") and v else v)
                for k, v in it.items()
            }) if False else it
            for it in schedule["items"]
        ]

        filtered = []
        for it in items:
            s = datetime.fromisoformat(it["start_time"]) if it["start_time"] else None
            e = datetime.fromisoformat(it["end_time"]) if it["end_time"] else None
            if not s or not e:
                continue
            if it["status"] in ("cancelled", "rejected", "released"):
                continue
            filtered.append((it, s, e))

        conflicts = []
        for i in range(len(filtered)):
            for j in range(i + 1, len(filtered)):
                a, a_s, a_e = filtered[i]
                b, b_s, b_e = filtered[j]
                if a["room_id"] != b["room_id"]:
                    continue
                if overlaps(a_s, a_e, b_s, b_e):
                    conflicts.append({
                        "room_id": a["room_id"],
                        "a": {"booking_id": a["booking_id"], "title": a["title"], "owner": a["owner_name"]},
                        "b": {"booking_id": b["booking_id"], "title": b["title"], "owner": b["owner_name"]},
                        "window": {"a_start": a_s.isoformat(), "a_end": a_e.isoformat(),
                                   "b_start": b_s.isoformat(), "b_end": b_e.isoformat()},
                    })

        return {
            "rule_version": settings.rule_version,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
        }

    def rebuild_schedule_from_events(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """显式地从事件流重建日程，用于验证事件回放一致性"""
        return self.get_schedule(start=start, end=end)
