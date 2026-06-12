from __future__ import annotations
from typing import Optional, List
from datetime import datetime
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse, JSONResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..config import settings
from ..domain.permissions import UserRole
from ..services.command_handler import CommandHandler, DomainError
from ..services.commands import (
    CreateBookingCmd, ApproveBookingCmd, RejectBookingCmd,
    RescheduleBookingCmd, CancelBookingCmd, CheckInCmd,
    ReleaseBookingCmd, ArbitrateCmd, CompleteBookingCmd,
)
from ..services.queries import QueryService
from ..services.arbitration import ArbitrationService
from ..models import schemas as S

router = APIRouter(prefix="/api/v1", tags=["会议室预订"])


def _parse_actor(
    x_actor_id: str = Header(..., alias="X-Actor-Id", description="操作人ID"),
    x_actor_role: str = Header(..., alias="X-Actor-Role", description="操作人角色 member/team_admin/receptionist/system_admin"),
    x_actor_name: str = Header(..., alias="X-Actor-Name", description="操作人姓名"),
):
    try:
        role = UserRole(x_actor_role)
    except ValueError:
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_ROLE",
            "message": f"无效角色 {x_actor_role}",
            "details": {"valid_roles": [r.value for r in UserRole]},
        })
    return {"actor_id": x_actor_id, "actor_role": role, "actor_name": x_actor_name}


def _error_resp(exc: DomainError, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "success": False,
        "error": exc.to_dict(),
        "rule_version": settings.rule_version,
    })


# ---------- 命令接口 ----------

@router.post("/bookings", response_model=S.CommandResponse)
def create_booking(
    req: S.CreateBookingRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    handler = CommandHandler(db)
    cmd = CreateBookingCmd(
        room_id=req.room_id,
        owner_id=req.owner_id,
        owner_name=req.owner_name,
        team_id=req.team_id,
        title=req.title,
        start_time=req.start_time,
        end_time=req.end_time,
        attendees=req.attendees,
        description=req.description,
    )
    try:
        result = handler.create_booking(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code == "BOOKING_CONFLICT" else 400)
    return {
        "success": True,
        "booking": result["booking"],
        "events": result["events"],
        "rule_version": settings.rule_version,
    }


@router.post("/bookings/{booking_id}/approve", response_model=S.CommandResponse)
def approve_booking(
    booking_id: str,
    req: S.ApproveBookingRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    handler = CommandHandler(db)
    if req.booking_id and req.booking_id != booking_id:
        return _error_resp(DomainError("ID_MISMATCH", "路径ID与请求体ID不一致"), 400)
    cmd = ApproveBookingCmd(
        booking_id=booking_id,
        approver_id=req.approver_id,
        approver_name=req.approver_name,
        reason=req.reason,
        expected_version=req.expected_version,
    )
    try:
        result = handler.approve_booking(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code == "CONCURRENCY_CONFLICT" else 400)
    return {
        "success": True,
        "booking": result["booking"],
        "events": result["events"],
        "rule_version": settings.rule_version,
    }


@router.post("/bookings/{booking_id}/reject", response_model=S.CommandResponse)
def reject_booking(
    booking_id: str,
    req: S.RejectBookingRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    handler = CommandHandler(db)
    cmd = RejectBookingCmd(
        booking_id=booking_id,
        approver_id=req.approver_id,
        approver_name=req.approver_name,
        reason=req.reason,
        expected_version=req.expected_version,
    )
    try:
        result = handler.reject_booking(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code == "CONCURRENCY_CONFLICT" else 400)
    return {
        "success": True,
        "booking": result["booking"],
        "events": result["events"],
        "rule_version": settings.rule_version,
    }


@router.post("/bookings/{booking_id}/reschedule", response_model=S.CommandResponse)
def reschedule_booking(
    booking_id: str,
    req: S.RescheduleBookingRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    handler = CommandHandler(db)
    cmd = RescheduleBookingCmd(
        booking_id=booking_id,
        rescheduler_id=req.rescheduler_id,
        rescheduler_name=req.rescheduler_name,
        new_start_time=req.new_start_time,
        new_end_time=req.new_end_time,
        new_room_id=req.new_room_id,
        reason=req.reason,
        expected_version=req.expected_version,
    )
    try:
        result = handler.reschedule_booking(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code in ("BOOKING_CONFLICT", "CONCURRENCY_CONFLICT") else 400)
    return {
        "success": True,
        "booking": result["booking"],
        "events": result["events"],
        "rule_version": settings.rule_version,
    }


@router.post("/bookings/{booking_id}/cancel", response_model=S.CommandResponse)
def cancel_booking(
    booking_id: str,
    req: S.CancelBookingRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    handler = CommandHandler(db)
    cmd = CancelBookingCmd(
        booking_id=booking_id,
        canceller_id=req.canceller_id,
        canceller_name=req.canceller_name,
        reason=req.reason,
        expected_version=req.expected_version,
    )
    try:
        result = handler.cancel_booking(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code == "CONCURRENCY_CONFLICT" else 400)
    return {
        "success": True,
        "booking": result["booking"],
        "events": result["events"],
        "rule_version": settings.rule_version,
    }


@router.post("/bookings/{booking_id}/check-in", response_model=S.CommandResponse)
def check_in(
    booking_id: str,
    req: S.CheckInRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    handler = CommandHandler(db)
    cmd = CheckInCmd(
        booking_id=booking_id,
        check_in_user_id=req.check_in_user_id,
        check_in_user_name=req.check_in_user_name,
        check_in_time=req.check_in_time,
        expected_version=req.expected_version,
    )
    try:
        result = handler.check_in(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code == "CONCURRENCY_CONFLICT" else 400)
    return {
        "success": True,
        "booking": result["booking"],
        "events": result["events"],
        "rule_version": settings.rule_version,
    }


@router.post("/bookings/{booking_id}/release", response_model=S.CommandResponse)
def release_booking(
    booking_id: str,
    req: S.ReleaseBookingRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    handler = CommandHandler(db)
    cmd = ReleaseBookingCmd(
        booking_id=booking_id,
        released_by_id=req.released_by_id,
        released_by_name=req.released_by_name,
        reason=req.reason,
        release_time=req.release_time,
        expected_version=req.expected_version,
    )
    try:
        result = handler.release_booking(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code == "CONCURRENCY_CONFLICT" else 400)
    return {
        "success": True,
        "booking": result["booking"],
        "events": result["events"],
        "rule_version": settings.rule_version,
    }


@router.post("/bookings/{booking_id}/arbitrate", response_model=S.CommandResponse)
def arbitrate(
    booking_id: str,
    req: S.ArbitrateRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    handler = CommandHandler(db)
    cmd = ArbitrateCmd(
        booking_id=booking_id,
        arbitrator_id=req.arbitrator_id,
        arbitrator_name=req.arbitrator_name,
        decision=req.decision,
        reason=req.reason,
        affected_booking_ids=req.affected_booking_ids,
        arbitration_time=req.arbitration_time,
        expected_version=req.expected_version,
    )
    try:
        result = handler.arbitrate(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code == "CONCURRENCY_CONFLICT" else 403 if e.code == "PERMISSION_DENIED" else 400)
    return {
        "success": True,
        "booking": result["booking"],
        "events": result["events"],
        "rule_version": settings.rule_version,
    }


# ---------- 查询接口 ----------

@router.get("/rooms", response_model=S.RoomsResponse)
def list_rooms(db: Session = Depends(get_db)):
    svc = QueryService(db)
    return svc.get_rooms()


@router.get("/bookings/{booking_id}")
def get_booking(booking_id: str, db: Session = Depends(get_db)):
    svc = QueryService(db)
    b = svc.get_booking(booking_id)
    if not b:
        raise HTTPException(status_code=404, detail={
            "code": "BOOKING_NOT_FOUND", "message": f"预订 {booking_id} 不存在"
        })
    return {"rule_version": settings.rule_version, "booking": b}


@router.get("/schedule", response_model=S.ScheduleResponse)
def get_schedule(
    start: Optional[datetime] = Query(None, description="时间窗开始 ISO"),
    end: Optional[datetime] = Query(None, description="时间窗结束 ISO"),
    room_id: Optional[str] = Query(None),
    owner_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="逗号分隔状态过滤"),
    include_historical: bool = Query(False),
    db: Session = Depends(get_db),
):
    svc = QueryService(db)
    status_list = [s.strip() for s in status.split(",")] if status else None
    return svc.get_schedule(
        start=start, end=end, room_id=room_id,
        owner_id=owner_id, status_filter=status_list, include_historical=include_historical,
    )


@router.get("/events", response_model=S.EventQueryResponse)
def query_events(
    stream_id: Optional[str] = Query(None, description="预订ID"),
    event_type: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    room_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    svc = QueryService(db)
    return svc.query_events(
        stream_id=stream_id, event_type=event_type, since=since, until=until,
        room_id=room_id, user_id=user_id, limit=limit, offset=offset,
    )


@router.get("/export")
def export_schedule(
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
    room_id: Optional[str] = Query(None),
    format: str = Query("csv", pattern="^(csv|json)$"),
    download: bool = Query(False),
    db: Session = Depends(get_db),
):
    svc = QueryService(db)
    result = svc.export_schedule(start=start, end=end, room_id=room_id, format=format)
    if format == "csv" and download:
        return PlainTextResponse(
            content=result.get("content") or "",
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="schedule.csv"'},
        )
    return result


@router.get("/conflicts/analyze", response_model=S.ConflictAnalysisResponse)
def analyze_conflict(
    room_id: str = Query(...),
    start: datetime = Query(...),
    end: datetime = Query(...),
    db: Session = Depends(get_db),
):
    svc = ArbitrationService(db)
    return svc.analyze_conflicts(room_id, start, end)


@router.get("/conflicts/suggest", response_model=S.SuggestionResponse)
def suggest_alternatives(
    room_id: str = Query(...),
    desired_start: datetime = Query(...),
    desired_end: datetime = Query(...),
    search_days: int = Query(7, ge=1, le=60),
    db: Session = Depends(get_db),
):
    svc = ArbitrationService(db)
    suggestions = svc.suggest_alternative_slots(room_id, desired_start, desired_end, search_days=search_days)
    return {"rule_version": settings.rule_version, "suggestions": suggestions}


@router.post("/maintenance/auto-release")
def auto_release(
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    svc = ArbitrationService(db)
    return svc.auto_release_stale(actor["actor_id"], actor["actor_name"], actor["actor_role"])


@router.get("/health")
def health():
    return {"status": "ok", "rule_version": settings.rule_version, "now": datetime.now().isoformat()}
