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
    SubmitRescheduleRequestCmd, ApproveRescheduleRequestCmd, RejectRescheduleRequestCmd,
    SubmitWaitlistCmd, ConfirmWaitlistCmd, CancelWaitlistCmd, RejectWaitlistCmd,
)
from ..services.queries import QueryService
from ..services.arbitration import ArbitrationService
from ..services.reschedule_service import RescheduleApprovalService
from ..services.waitlist_service import WaitlistService
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


@router.post("/bookings/{booking_id}/reschedule", response_model=S.RescheduleApprovalResponse)
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
        return _error_resp(e, 409 if e.code in ("BOOKING_CONFLICT", "CONCURRENCY_CONFLICT", "PENDING_REQUEST_CONFLICT") else 400)
    return {
        "success": True,
        "request": result.get("reschedule_request"),
        "booking": result["booking"],
        "events": result["events"],
        "rule_version": settings.rule_version,
        "requires_approval": result.get("requires_approval", False),
        "has_internal_conflicts": result.get("has_internal_conflicts", False),
        "internal_conflicts": result.get("internal_conflicts", []),
        "superseded_requests": result.get("superseded_requests", []),
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


@router.post("/reschedule-requests/{request_id}/approve", response_model=S.RescheduleApprovalResponse)
def approve_reschedule_request(
    request_id: str,
    req: S.ApproveRescheduleRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    svc = RescheduleApprovalService(db)
    if req.request_id and req.request_id != request_id:
        return _error_resp(DomainError("ID_MISMATCH", "路径ID与请求体ID不一致"), 400)
    cmd = ApproveRescheduleRequestCmd(
        request_id=request_id,
        approver_id=req.approver_id,
        approver_name=req.approver_name,
        reason=req.reason,
        expected_version=req.expected_version,
    )
    try:
        result = svc.approve_request(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code in ("BOOKING_CONFLICT", "CONCURRENCY_CONFLICT", "PENDING_REQUEST_CONFLICT") else 400)
    return {
        "success": True,
        "request": result["request"],
        "booking": result["booking"],
        "events": result["events"],
        "rule_version": settings.rule_version,
        "superseded_requests": result.get("superseded_requests", []),
    }


@router.post("/reschedule-requests/{request_id}/reject", response_model=S.RescheduleApprovalResponse)
def reject_reschedule_request(
    request_id: str,
    req: S.RejectRescheduleRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    svc = RescheduleApprovalService(db)
    if req.request_id and req.request_id != request_id:
        return _error_resp(DomainError("ID_MISMATCH", "路径ID与请求体ID不一致"), 400)
    cmd = RejectRescheduleRequestCmd(
        request_id=request_id,
        approver_id=req.approver_id,
        approver_name=req.approver_name,
        reason=req.reason,
        expected_version=req.expected_version,
    )
    try:
        result = svc.reject_request(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code == "CONCURRENCY_CONFLICT" else 400)
    return {
        "success": True,
        "request": result["request"],
        "booking": result["booking"],
        "events": result["events"],
        "rule_version": settings.rule_version,
    }


@router.get("/reschedule-requests/{request_id}", response_model=S.RescheduleApprovalResponse)
def get_reschedule_request(
    request_id: str,
    db: Session = Depends(get_db),
):
    svc = RescheduleApprovalService(db)
    try:
        req = svc.get_request(request_id)
    except DomainError as e:
        return _error_resp(e, 404)
    return {
        "success": True,
        "request": req,
        "rule_version": settings.rule_version,
    }


@router.get("/reschedule-requests", response_model=S.RescheduleRequestListResponse)
def list_reschedule_requests(
    booking_id: Optional[str] = Query(None, description="预订ID"),
    status: Optional[str] = Query(None, description="状态过滤"),
    requester_id: Optional[str] = Query(None, description="申请人ID"),
    room_id: Optional[str] = Query(None, description="房间ID"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    svc = RescheduleApprovalService(db)
    return svc.list_requests(
        booking_id=booking_id,
        status=status,
        requester_id=requester_id,
        room_id=room_id,
        limit=limit,
        offset=offset,
    )


@router.get("/bookings/{booking_id}/reschedule-requests/pending", response_model=S.RescheduleRequestListResponse)
def get_booking_pending_reschedule_requests(
    booking_id: str,
    db: Session = Depends(get_db),
):
    svc = RescheduleApprovalService(db)
    items = svc.get_booking_pending_requests(booking_id)
    return {
        "total": len(items),
        "limit": len(items),
        "offset": 0,
        "items": items,
        "rule_version": settings.rule_version,
    }


# ---------- 候补队列接口 ----------

@router.post("/waitlist", response_model=S.WaitlistActionResponse)
def submit_waitlist(
    req: S.SubmitWaitlistRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    svc = WaitlistService(db)
    cmd = SubmitWaitlistCmd(
        room_id=req.room_id,
        requester_id=req.requester_id,
        requester_name=req.requester_name,
        team_id=req.team_id,
        title=req.title,
        desired_start_time=req.desired_start_time,
        desired_end_time=req.desired_end_time,
        flex_before_minutes=req.flex_before_minutes,
        flex_after_minutes=req.flex_after_minutes,
        attendees=req.attendees,
        priority_note=req.priority_note,
        contact_info=req.contact_info,
        description=req.description,
    )
    try:
        result = svc.submit_waitlist(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code in ("DUPLICATE_WAITLIST", "NO_CONFLICT") else 400)
    return {
        "success": True,
        "waitlist": result["waitlist"],
        "rule_version": settings.rule_version,
    }


@router.get("/waitlist/{waitlist_id}", response_model=S.WaitlistActionResponse)
def get_waitlist(
    waitlist_id: str,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    svc = WaitlistService(db)
    try:
        result = svc.get_waitlist(waitlist_id, actor["actor_id"], actor["actor_role"])
    except DomainError as e:
        return _error_resp(e, 404 if e.code == "WAITLIST_NOT_FOUND" else 403)
    return {
        "success": True,
        "waitlist": result["waitlist"],
        "rule_version": settings.rule_version,
    }


@router.get("/waitlist", response_model=S.WaitlistListResponse)
def list_waitlists(
    room_id: Optional[str] = Query(None, description="房间ID过滤"),
    status: Optional[str] = Query(None, description="状态过滤"),
    requester_id: Optional[str] = Query(None, description="申请人ID过滤（仅管理员可用）"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    svc = WaitlistService(db)
    return svc.list_waitlists(
        actor_id=actor["actor_id"],
        actor_role=actor["actor_role"],
        room_id=room_id,
        status=status,
        requester_id=requester_id,
        limit=limit,
        offset=offset,
    )


@router.post("/waitlist/{waitlist_id}/confirm", response_model=S.WaitlistActionResponse)
def confirm_waitlist(
    waitlist_id: str,
    req: S.ConfirmWaitlistRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    svc = WaitlistService(db)
    if req.waitlist_id and req.waitlist_id != waitlist_id:
        return _error_resp(DomainError("ID_MISMATCH", "路径ID与请求体ID不一致"), 400)
    cmd = ConfirmWaitlistCmd(
        waitlist_id=waitlist_id,
        confirmer_id=req.confirmer_id,
        confirmer_name=req.confirmer_name,
        reason=req.reason,
    )
    try:
        result = svc.confirm_waitlist(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 409 if e.code in ("BOOKING_CONFLICT", "WAITLIST_EXPIRED") else 400)
    return {
        "success": True,
        "waitlist": result["waitlist"],
        "booking": result.get("booking"),
        "events": result.get("events", []),
        "rule_version": settings.rule_version,
    }


@router.post("/waitlist/{waitlist_id}/cancel", response_model=S.WaitlistActionResponse)
def cancel_waitlist(
    waitlist_id: str,
    req: S.CancelWaitlistRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    svc = WaitlistService(db)
    if req.waitlist_id and req.waitlist_id != waitlist_id:
        return _error_resp(DomainError("ID_MISMATCH", "路径ID与请求体ID不一致"), 400)
    cmd = CancelWaitlistCmd(
        waitlist_id=waitlist_id,
        canceller_id=req.canceller_id,
        canceller_name=req.canceller_name,
        reason=req.reason,
    )
    try:
        result = svc.cancel_waitlist(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 400)
    return {
        "success": True,
        "waitlist": result["waitlist"],
        "rule_version": settings.rule_version,
    }


@router.post("/waitlist/{waitlist_id}/reject", response_model=S.WaitlistActionResponse)
def reject_waitlist(
    waitlist_id: str,
    req: S.RejectWaitlistRequest,
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    svc = WaitlistService(db)
    if req.waitlist_id and req.waitlist_id != waitlist_id:
        return _error_resp(DomainError("ID_MISMATCH", "路径ID与请求体ID不一致"), 400)
    cmd = RejectWaitlistCmd(
        waitlist_id=waitlist_id,
        rejecter_id=req.rejecter_id,
        rejecter_name=req.rejecter_name,
        reason=req.reason,
    )
    try:
        result = svc.reject_waitlist(cmd, actor["actor_id"], actor["actor_role"], actor["actor_name"])
    except DomainError as e:
        return _error_resp(e, 403 if e.code == "PERMISSION_DENIED" else 400)
    return {
        "success": True,
        "waitlist": result["waitlist"],
        "rule_version": settings.rule_version,
    }


@router.post("/maintenance/expire-waitlist")
def expire_stale_waitlists(
    actor: dict = Depends(_parse_actor),
    db: Session = Depends(get_db),
):
    from ..domain.permissions import Permission, has_permission
    if not has_permission(actor["actor_role"], Permission.MANAGE_WAITLIST):
        return _error_resp(DomainError("PERMISSION_DENIED", "仅管理员可执行过期清理"), 403)
    svc = WaitlistService(db)
    count = svc.expire_stale_waitlists()
    return {
        "success": True,
        "expired_count": count,
        "rule_version": settings.rule_version,
    }


@router.get("/health")
def health():
    return {"status": "ok", "rule_version": settings.rule_version, "now": datetime.now().isoformat()}
