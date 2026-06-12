from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, Index, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
