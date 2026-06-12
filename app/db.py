from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, Index, BigInteger, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime, timezone
from .config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class EventStore(Base):
    __tablename__ = "event_store"

    id = Column(Integer, primary_key=True, autoincrement=True)
    stream_id = Column(String(128), nullable=False, index=True)
    stream_type = Column(String(64), nullable=False, index=True)
    version = Column(BigInteger, nullable=False)
    event_type = Column(String(128), nullable=False)
    event_data = Column(Text, nullable=False)
    metadata_ = Column("metadata", Text, nullable=True)
    rule_version = Column(String(32), nullable=False, default="v1.0.0")
    created_at = Column(DateTime, nullable=False, default=now_utc)

    __table_args__ = (
        Index("ux_stream_version", "stream_id", "version", unique=True),
        Index("ix_event_created_at", "created_at"),
        Index("ix_event_type", "event_type"),
    )


class UserDirectory(Base):
    __tablename__ = "user_directory"

    user_id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    email = Column(String(256), nullable=True)
    role = Column(String(32), nullable=False, default="member")
    team_id = Column(String(64), nullable=True, index=True)
    team_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=now_utc)


class RescheduleRequest(Base):
    __tablename__ = "reschedule_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(128), nullable=False, unique=True, index=True)
    booking_id = Column(String(128), nullable=False, index=True)
    requester_id = Column(String(64), nullable=False)
    requester_name = Column(String(128), nullable=False)
    requester_role = Column(String(32), nullable=False)

    old_start_time = Column(DateTime, nullable=False)
    old_end_time = Column(DateTime, nullable=False)
    old_room_id = Column(String(64), nullable=False)

    new_start_time = Column(DateTime, nullable=False)
    new_end_time = Column(DateTime, nullable=False)
    new_room_id = Column(String(64), nullable=False)

    reason = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="pending", index=True)

    approver_id = Column(String(64), nullable=True)
    approver_name = Column(String(128), nullable=True)
    approve_reason = Column(Text, nullable=True)
    approved_at = Column(DateTime, nullable=True)

    booking_version = Column(BigInteger, nullable=False)
    rule_version = Column(String(32), nullable=False, default="v1.0.0")

    created_at = Column(DateTime, nullable=False, default=now_utc)
    updated_at = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    __table_args__ = (
        Index("ix_booking_status", "booking_id", "status"),
        Index("ix_room_time", "new_room_id", "new_start_time", "new_end_time"),
        Index("ix_created_at", "created_at"),
    )


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    waitlist_id = Column(String(128), nullable=False, unique=True, index=True)
    room_id = Column(String(64), nullable=False, index=True)
    requester_id = Column(String(64), nullable=False, index=True)
    requester_name = Column(String(128), nullable=False)
    requester_role = Column(String(32), nullable=False)
    team_id = Column(String(64), nullable=True)

    desired_start_time = Column(DateTime, nullable=False)
    desired_end_time = Column(DateTime, nullable=False)
    flex_before_minutes = Column(Integer, nullable=False, default=0)
    flex_after_minutes = Column(Integer, nullable=False, default=0)

    title = Column(String(256), nullable=False)
    attendees = Column(Text, nullable=True)
    priority_note = Column(Text, nullable=True)
    contact_info = Column(String(256), nullable=True)
    description = Column(Text, nullable=True)

    status = Column(String(32), nullable=False, default="waiting", index=True)

    matched_booking_id = Column(String(128), nullable=True)
    matched_start_time = Column(DateTime, nullable=True)
    matched_end_time = Column(DateTime, nullable=True)
    match_reason = Column(Text, nullable=True)
    matched_at = Column(DateTime, nullable=True)

    confirmed_by_id = Column(String(64), nullable=True)
    confirmed_by_name = Column(String(128), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    result_booking_id = Column(String(128), nullable=True)

    expire_at = Column(DateTime, nullable=True, index=True)
    expired_at = Column(DateTime, nullable=True)
    expire_reason = Column(Text, nullable=True)

    rule_version = Column(String(32), nullable=False, default="v1.0.0")
    created_at = Column(DateTime, nullable=False, default=now_utc, index=True)
    updated_at = Column(DateTime, nullable=False, default=now_utc, onupdate=now_utc)

    __table_args__ = (
        Index("ix_room_status_time", "room_id", "status", "desired_start_time"),
        Index("ix_requester_status", "requester_id", "status"),
        Index("ix_status_expire", "status", "expire_at"),
    )


class WaitlistMatchLog(Base):
    __tablename__ = "waitlist_match_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    log_id = Column(String(128), nullable=False, unique=True, index=True)
    waitlist_id = Column(String(128), nullable=False, index=True)
    trigger_event = Column(String(64), nullable=False)
    trigger_booking_id = Column(String(128), nullable=True)
    freed_room_id = Column(String(64), nullable=True)
    freed_start_time = Column(DateTime, nullable=True)
    freed_end_time = Column(DateTime, nullable=True)

    match_status = Column(String(32), nullable=False)
    match_score = Column(Integer, nullable=True)
    match_details = Column(Text, nullable=True)

    operator_id = Column(String(64), nullable=True)
    operator_name = Column(String(128), nullable=True)
    rule_version = Column(String(32), nullable=False, default="v1.0.0")
    created_at = Column(DateTime, nullable=False, default=now_utc, index=True)


class WaitlistActionLog(Base):
    __tablename__ = "waitlist_action_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    log_id = Column(String(128), nullable=False, unique=True, index=True)
    waitlist_id = Column(String(128), nullable=False, index=True)
    action = Column(String(64), nullable=False)
    old_status = Column(String(32), nullable=True)
    new_status = Column(String(32), nullable=True)
    reason = Column(Text, nullable=True)

    actor_id = Column(String(64), nullable=True)
    actor_name = Column(String(128), nullable=True)
    actor_role = Column(String(32), nullable=True)
    rule_version = Column(String(32), nullable=False, default="v1.0.0")
    created_at = Column(DateTime, nullable=False, default=now_utc, index=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
