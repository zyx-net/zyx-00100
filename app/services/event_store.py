from __future__ import annotations
from typing import List, Optional, Dict, Any
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
import json

from ..db import EventStore
from ..config import settings
from ..domain.aggregate import EventRecord, BookingAggregate, rebuild_aggregate, overlaps
from ..domain.permissions import BookingStatus


class ConcurrencyError(Exception):
    def __init__(self, stream_id: str, expected_version: int, actual_version: int):
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Concurrency conflict on stream {stream_id}: expected version {expected_version}, actual {actual_version}"
        )


class EventStoreService:
    def __init__(self, db: Session):
        self.db = db

    def load_events(
        self,
        stream_id: Optional[str] = None,
        stream_type: Optional[str] = None,
        event_type: Optional[str] = None,
        since_version: Optional[int] = None,
        since_created_at: Optional[datetime] = None,
        until_created_at: Optional[datetime] = None,
        room_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[EventRecord]:
        q = self.db.query(EventStore)
        filters = []
        if stream_id:
            filters.append(EventStore.stream_id == stream_id)
        if stream_type:
            filters.append(EventStore.stream_type == stream_type)
        if event_type:
            filters.append(EventStore.event_type == event_type)
        if since_version is not None:
            filters.append(EventStore.version >= since_version)
        if since_created_at:
            filters.append(EventStore.created_at >= since_created_at)
        if until_created_at:
            filters.append(EventStore.created_at <= until_created_at)
        if filters:
            q = q.filter(and_(*filters))
        q = q.order_by(EventStore.stream_id.asc(), EventStore.version.asc(), EventStore.id.asc())
        q = q.offset(offset).limit(limit)
        rows = q.all()
        records = []
        for r in rows:
            data = json.loads(r.event_data)
            meta = json.loads(r.metadata_) if r.metadata_ else {}
            if room_id and data.get("room_id") != room_id and data.get("new_room_id") != room_id and data.get("old_room_id") != room_id:
                if not any(data.get(k) == room_id for k in ("room_id", "new_room_id", "old_room_id")):
                    continue
            if user_id:
                found = False
                for k in ("owner_id", "approver_id", "canceller_id", "check_in_user_id",
                          "released_by_id", "arbitrator_id", "rescheduler_id"):
                    if data.get(k) == user_id:
                        found = True
                        break
                if not found:
                    continue
            records.append(EventRecord(
                event_id=r.id,
                stream_id=r.stream_id,
                stream_type=r.stream_type,
                version=r.version,
                event_type=r.event_type,
                event_data=data,
                metadata=meta,
                rule_version=r.rule_version,
                created_at=r.created_at,
            ))
        return records

    def load_stream(self, stream_id: str) -> List[EventRecord]:
        return self.load_events(stream_id=stream_id)

    def append_events(
        self,
        stream_id: str,
        stream_type: str,
        expected_version: int,
        events: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[EventRecord]:
        if not events:
            return []

        current_version = self._get_current_version(stream_id)
        if current_version != expected_version:
            raise ConcurrencyError(stream_id, expected_version, current_version)

        created = []
        version = expected_version
        global_meta = metadata or {}
        for evt in events:
            version += 1
            evt_data = evt.get("event_data", {})
            evt_meta = {**global_meta, **evt.get("metadata", {})}
            row = EventStore(
                stream_id=stream_id,
                stream_type=stream_type,
                version=version,
                event_type=evt["event_type"],
                event_data=json.dumps(evt_data, ensure_ascii=False, default=str),
                metadata_=json.dumps(evt_meta, ensure_ascii=False, default=str) if evt_meta else None,
                rule_version=settings.rule_version,
            )
            self.db.add(row)
            self.db.flush()
            self.db.refresh(row)
            created.append(EventRecord(
                event_id=row.id,
                stream_id=row.stream_id,
                stream_type=row.stream_type,
                version=row.version,
                event_type=row.event_type,
                event_data=evt_data,
                metadata=evt_meta,
                rule_version=row.rule_version,
                created_at=row.created_at,
            ))
        self.db.commit()
        return created

    def _get_current_version(self, stream_id: str) -> int:
        from sqlalchemy import func
        v = self.db.query(func.max(EventStore.version)).filter(
            EventStore.stream_id == stream_id
        ).scalar()
        return int(v) if v is not None else 0

    def load_aggregate(self, stream_id: str) -> BookingAggregate:
        events = self.load_stream(stream_id)
        return rebuild_aggregate(stream_id, events)

    def get_all_active_bookings(
        self,
        start_window: Optional[datetime] = None,
        end_window: Optional[datetime] = None,
        room_id: Optional[str] = None,
    ) -> List[BookingAggregate]:
        all_events = self.load_events(stream_type="booking", limit=100000)

        streams: Dict[str, List[EventRecord]] = {}
        for e in all_events:
            if room_id:
                data = e.event_data
                matches = False
                for k in ("room_id", "new_room_id"):
                    if data.get(k) == room_id:
                        matches = True
                        break
                if e.event_type == "booking_created" and data.get("room_id") != room_id:
                    continue
            streams.setdefault(e.stream_id, []).append(e)

        result = []
        for sid, evts in streams.items():
            agg = rebuild_aggregate(sid, sorted(evts, key=lambda e: (e.version, e.event_id)))
            if agg.status in (BookingStatus.CANCELLED, BookingStatus.REJECTED, BookingStatus.RELEASED):
                if not (start_window and end_window and agg.start_time and agg.end_time):
                    pass
            if start_window and end_window and agg.start_time and agg.end_time:
                if not overlaps(start_window, end_window, agg.start_time, agg.end_time):
                    continue
            result.append(agg)
        result.sort(key=lambda a: (a.start_time.isoformat() if a.start_time else "", a.room_id, a.booking_id))
        return result

    def find_conflicting_bookings(
        self,
        room_id: str,
        start_time: datetime,
        end_time: datetime,
        exclude_booking_id: Optional[str] = None,
    ) -> List[BookingAggregate]:
        all = self.get_all_active_bookings()
        conflicts = []
        for b in all:
            if exclude_booking_id and b.booking_id == exclude_booking_id:
                continue
            if b.room_id != room_id:
                continue
            if b.status in (BookingStatus.CANCELLED, BookingStatus.REJECTED, BookingStatus.RELEASED):
                continue
            if b.start_time and b.end_time and overlaps(start_time, end_time, b.start_time, b.end_time):
                conflicts.append(b)
        return conflicts
