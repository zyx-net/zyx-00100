from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List, Optional
from datetime import time, timedelta


class RoomConfig(BaseSettings):
    room_id: str
    name: str
    capacity: int
    floor: int
    facilities: List[str] = Field(default_factory=list)
    require_approval: bool = False
    min_duration_minutes: int = 30
    max_duration_minutes: int = 240
    booking_window_days: int = 14
    check_in_grace_minutes: int = 15
    time_slot_step_minutes: int = 30
    available_from: time = time(8, 0)
    available_to: time = time(22, 0)


class AppSettings(BaseSettings):
    database_url: str = "sqlite:///./room_booking.db"
    rule_version: str = "v1.0.0"
    default_cancel_before_minutes: int = 30
    default_auto_release_after_start_minutes: int = 30
    rooms: List[RoomConfig] = [
        RoomConfig(
            room_id="room-101",
            name="创新空间 A",
            capacity=8,
            floor=1,
            facilities=["投影仪", "白板", "视频会议"],
            require_approval=False,
            min_duration_minutes=30,
            max_duration_minutes=180,
            booking_window_days=14,
            check_in_grace_minutes=15,
        ),
        RoomConfig(
            room_id="room-102",
            name="创新空间 B",
            capacity=12,
            floor=1,
            facilities=["投影仪", "白板", "视频会议", "电话会议"],
            require_approval=False,
            min_duration_minutes=30,
            max_duration_minutes=240,
            booking_window_days=14,
            check_in_grace_minutes=15,
        ),
        RoomConfig(
            room_id="room-201",
            name="董事会议室",
            capacity=20,
            floor=2,
            facilities=["8K 投影仪", "白板", "视频会议", "音响系统", "茶水服务"],
            require_approval=True,
            min_duration_minutes=60,
            max_duration_minutes=480,
            booking_window_days=30,
            check_in_grace_minutes=20,
        ),
        RoomConfig(
            room_id="room-202",
            name="头脑风暴室",
            capacity=6,
            floor=2,
            facilities=["白板墙", "可移动桌椅", "电视屏幕"],
            require_approval=False,
            min_duration_minutes=30,
            max_duration_minutes=180,
            booking_window_days=7,
            check_in_grace_minutes=10,
        ),
    ]

    class Config:
        env_prefix = "APP_"


settings = AppSettings()


def get_room_config(room_id: str) -> Optional[RoomConfig]:
    for room in settings.rooms:
        if room.room_id == room_id:
            return room
    return None
