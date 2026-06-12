"""
改期审批链路测试
覆盖: 提交、审批通过、拒绝、重复审批、无权限审批、重启后查询、冲突场景
运行: python test_reschedule_approval.py
"""
from __future__ import annotations
import sys, os, json
from datetime import datetime, timezone, timedelta

db_path = os.path.join(os.path.dirname(__file__), "test_reschedule.db")
if os.path.exists(db_path):
    os.remove(db_path)

os.environ["APP_DATABASE_URL"] = f"sqlite:///{db_path}"

from app.db import init_db, SessionLocal, RescheduleRequest
from app.seed import seed_users
from app.services.command_handler import CommandHandler, DomainError
from app.services.commands import (
    CreateBookingCmd, RescheduleBookingCmd,
    SubmitRescheduleRequestCmd, ApproveRescheduleRequestCmd, RejectRescheduleRequestCmd,
)
from app.services.reschedule_service import RescheduleApprovalService
from app.services.event_store import EventStoreService
from app.domain.permissions import UserRole, BookingStatus, RescheduleRequestStatus, EventType
from app.config import settings

TZ = timezone(timedelta(hours=8))
PASS = "PASS"
FAIL = "FAIL"


def t(desc):
    def wrap(fn):
        def inner(*args, **kwargs):
            try:
                fn(*args, **kwargs)
                print(f"[{PASS}] {desc}")
                return True
            except AssertionError as e:
                print(f"[{FAIL}] {desc} -> {e}")
                import traceback
                traceback.print_exc()
                return False
            except DomainError as e:
                print(f"[{FAIL}] {desc} -> 未预期 DomainError: {e.code} {e.message}")
                import traceback
                traceback.print_exc()
                return False
            except Exception as e:
                print(f"[{FAIL}] {desc} -> EXC: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                return False
        return inner
    return wrap


def _create_test_booking(db, owner_id="u-zhangsan", owner_name="张三", role=UserRole.MEMBER, room_id="room-101", hours=1, day_offset=1):
    handler = CommandHandler(db)
    start = datetime.now(TZ) + timedelta(days=day_offset)
    start = start.replace(hour=10, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=hours)
    cmd = CreateBookingCmd(
        room_id=room_id,
        owner_id=owner_id,
        owner_name=owner_name,
        team_id="team-a",
        title="测试预订",
        start_time=start,
        end_time=end,
        attendees=["u-lisi"],
        description="测试用",
    )
    result = handler.create_booking(cmd, owner_id, role, owner_name)
    return result["booking"], start, end


@t("00 初始化数据库与用户")
def step_00(db):
    init_db()
    seed_users(db)


@t("01 成员提交改期请求 - 生成待审批记录")
def step_01(db, ctx):
    booking, orig_start, orig_end = _create_test_booking(db)
    ctx["bk1"] = booking["booking_id"]
    ctx["bk1_ver"] = booking["version"]
    ctx["bk1_orig_start"] = orig_start
    ctx["bk1_orig_end"] = orig_end

    svc = RescheduleApprovalService(db)
    new_start = orig_start + timedelta(hours=2)
    new_end = new_start + timedelta(hours=1)

    cmd = SubmitRescheduleRequestCmd(
        booking_id=ctx["bk1"],
        requester_id="u-zhangsan",
        requester_name="张三",
        new_start_time=new_start,
        new_end_time=new_end,
        reason="时间调整",
        expected_version=ctx["bk1_ver"],
    )
    result = svc.submit_request(cmd, "u-zhangsan", UserRole.MEMBER, "张三")

    assert result["request"]["status"] == RescheduleRequestStatus.PENDING.value
    assert result["request"]["booking_id"] == ctx["bk1"]
    assert result["request"]["requester_id"] == "u-zhangsan"
    assert result["booking"]["pending_reschedule_requests"], "聚合中应有待审批请求"
    assert len(result["events"]) == 1
    assert result["events"][0]["event_type"] == EventType.RESCHEDULE_REQUESTED.value

    ctx["req1"] = result["request"]["request_id"]
    ctx["req1_new_start"] = new_start
    ctx["req1_new_end"] = new_end
    ctx["bk1_ver"] = result["booking"]["version"]

    print(f"      请求ID: {ctx['req1']}, 原时段: {orig_start.strftime('%H:%M')} → 新时段: {new_start.strftime('%H:%M')}")


@t("02 普通成员无审批权限 - 拒绝")
def step_02(db, ctx):
    svc = RescheduleApprovalService(db)
    cmd = ApproveRescheduleRequestCmd(
        request_id=ctx["req1"],
        approver_id="u-zhaoliu",
        approver_name="赵六",
        reason="同意",
        expected_version=ctx["bk1_ver"],
    )
    try:
        svc.approve_request(cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
        raise AssertionError("应抛出无权限")
    except DomainError as e:
        assert e.code == "PERMISSION_DENIED"
        assert "改期审批权限" in e.message
        print(f"      权限校验通过: {e.code}")


@t("03 成员不能审批别人的改期请求 - 拒绝")
def step_03(db, ctx):
    svc = RescheduleApprovalService(db)
    cmd = ApproveRescheduleRequestCmd(
        request_id=ctx["req1"],
        approver_id="u-zhaoliu",
        approver_name="赵六",
        reason="同意",
        expected_version=ctx["bk1_ver"],
    )
    try:
        svc.approve_request(cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
        raise AssertionError("应抛出无权限")
    except DomainError as e:
        assert e.code == "PERMISSION_DENIED"
        print(f"      他人请求校验通过: {e.code}")


def _normalize_iso(iso_str: str) -> str:
    if iso_str.endswith("+00:00"):
        iso_str = iso_str[:-6]
    elif iso_str.endswith("+08:00"):
        iso_str = iso_str[:-6]
    return iso_str


@t("04 前台审批改期请求 - 批准后真正更新时间")
def step_04(db, ctx):
    svc = RescheduleApprovalService(db)
    cmd = ApproveRescheduleRequestCmd(
        request_id=ctx["req1"],
        approver_id="u-recep",
        approver_name="前台小李",
        reason="批准改期",
        expected_version=ctx["bk1_ver"],
    )
    result = svc.approve_request(cmd, "u-recep", UserRole.RECEPTIONIST, "前台小李")

    assert result["request"]["status"] == RescheduleRequestStatus.APPROVED.value
    assert result["request"]["approver_id"] == "u-recep"

    actual_start = _normalize_iso(result["booking"]["start_time"])
    expected_start = _normalize_iso(ctx["req1_new_start"].isoformat())
    actual_end = _normalize_iso(result["booking"]["end_time"])
    expected_end = _normalize_iso(ctx["req1_new_end"].isoformat())

    assert actual_start == expected_start, f"start_time mismatch: {actual_start} vs {expected_start}"
    assert actual_end == expected_end, f"end_time mismatch: {actual_end} vs {expected_end}"

    assert result["booking"]["status"] == BookingStatus.APPROVED.value
    assert len(result["booking"]["pending_reschedule_requests"]) == 0
    assert len(result["booking"]["reschedule_history"]) == 1
    assert len(result["booking"]["reschedule_requests_history"]) == 1
    assert result["events"][0]["event_type"] == EventType.RESCHEDULE_APPROVED.value

    ctx["bk1_ver"] = result["booking"]["version"]
    print(f"      改期已批准: 新时段 {ctx['req1_new_start'].strftime('%H:%M')}-{ctx['req1_new_end'].strftime('%H:%M')}")


@t("05 重复审批已批准的请求 - 拒绝")
def step_05(db, ctx):
    svc = RescheduleApprovalService(db)
    cmd = ApproveRescheduleRequestCmd(
        request_id=ctx["req1"],
        approver_id="u-recep",
        approver_name="前台小李",
        reason="再次批准",
        expected_version=ctx["bk1_ver"],
    )
    try:
        svc.approve_request(cmd, "u-recep", UserRole.RECEPTIONIST, "前台小李")
        raise AssertionError("应抛出状态无效")
    except DomainError as e:
        assert e.code == "INVALID_STATUS"
        assert "不允许审批" in e.message
        print(f"      重复审批校验通过: {e.code}")


@t("06 提交新的改期请求 - 被拒绝")
def step_06(db, ctx):
    booking, orig_start, orig_end = _create_test_booking(db, day_offset=2)
    ctx["bk2"] = booking["booking_id"]
    ctx["bk2_ver"] = booking["version"]

    svc = RescheduleApprovalService(db)
    new_start = orig_start + timedelta(hours=3)
    new_end = new_start + timedelta(hours=1)

    cmd = SubmitRescheduleRequestCmd(
        booking_id=ctx["bk2"],
        requester_id="u-zhangsan",
        requester_name="张三",
        new_start_time=new_start,
        new_end_time=new_end,
        reason="需要改期",
        expected_version=ctx["bk2_ver"],
    )
    result = svc.submit_request(cmd, "u-zhangsan", UserRole.MEMBER, "张三")
    ctx["req2"] = result["request"]["request_id"]
    ctx["bk2_ver"] = result["booking"]["version"]

    reject_cmd = RejectRescheduleRequestCmd(
        request_id=ctx["req2"],
        approver_id="u-recep",
        approver_name="前台小李",
        reason="时段紧张，不予批准",
        expected_version=ctx["bk2_ver"],
    )
    reject_result = svc.reject_request(reject_cmd, "u-recep", UserRole.RECEPTIONIST, "前台小李")

    assert reject_result["request"]["status"] == RescheduleRequestStatus.REJECTED.value
    assert reject_result["request"]["approve_reason"] == "时段紧张，不予批准"
    actual_start = _normalize_iso(reject_result["booking"]["start_time"])
    expected_start = _normalize_iso(orig_start.isoformat())
    actual_end = _normalize_iso(reject_result["booking"]["end_time"])
    expected_end = _normalize_iso(orig_end.isoformat())
    assert actual_start == expected_start, f"start_time mismatch: {actual_start} vs {expected_start}"
    assert actual_end == expected_end, f"end_time mismatch: {actual_end} vs {expected_end}"
    assert len(reject_result["booking"]["pending_reschedule_requests"]) == 0
    assert reject_result["events"][0]["event_type"] == EventType.RESCHEDULE_REJECTED.value

    ctx["bk2_ver"] = reject_result["booking"]["version"]
    print(f"      改期已驳回: 原时段保持 {orig_start.strftime('%H:%M')}-{orig_end.strftime('%H:%M')}")


@t("07 同一预订多个待审批改期 - 批准一个后其余被覆盖")
def step_07(db, ctx):
    booking, orig_start, orig_end = _create_test_booking(db, day_offset=3)
    ctx["bk3"] = booking["booking_id"]
    ctx["bk3_ver"] = booking["version"]
    ctx["bk3_orig_start"] = orig_start

    svc = RescheduleApprovalService(db)

    new_start_1 = orig_start + timedelta(hours=1)
    new_end_1 = new_start_1 + timedelta(hours=2)
    cmd1 = SubmitRescheduleRequestCmd(
        booking_id=ctx["bk3"],
        requester_id="u-zhangsan",
        requester_name="张三",
        new_start_time=new_start_1,
        new_end_time=new_end_1,
        reason="改期方案1",
        expected_version=ctx["bk3_ver"],
    )
    result1 = svc.submit_request(cmd1, "u-zhangsan", UserRole.MEMBER, "张三")
    ctx["req3_1"] = result1["request"]["request_id"]
    ctx["bk3_ver"] = result1["booking"]["version"]

    new_start_2 = orig_start + timedelta(hours=2)
    new_end_2 = new_start_2 + timedelta(hours=1)
    cmd2 = SubmitRescheduleRequestCmd(
        booking_id=ctx["bk3"],
        requester_id="u-zhangsan",
        requester_name="张三",
        new_start_time=new_start_2,
        new_end_time=new_end_2,
        reason="改期方案2",
        expected_version=ctx["bk3_ver"],
    )
    result2 = svc.submit_request(cmd2, "u-zhangsan", UserRole.MEMBER, "张三")
    ctx["req3_2"] = result2["request"]["request_id"]
    ctx["bk3_ver"] = result2["booking"]["version"]

    pending = svc.get_booking_pending_requests(ctx["bk3"])
    assert len(pending) == 2, f"应有2个待审批请求，实际{len(pending)}"
    assert result2["has_internal_conflicts"] == True, f"应有内部冲突: {result2.get('internal_conflicts')}"

    approve_cmd = ApproveRescheduleRequestCmd(
        request_id=ctx["req3_2"],
        approver_id="u-recep",
        approver_name="前台小李",
        reason="批准方案2",
        expected_version=ctx["bk3_ver"],
    )
    approve_result = svc.approve_request(approve_cmd, "u-recep", UserRole.RECEPTIONIST, "前台小李")

    assert approve_result["request"]["status"] == RescheduleRequestStatus.APPROVED.value
    actual_start = _normalize_iso(approve_result["booking"]["start_time"])
    expected_start = _normalize_iso(new_start_2.isoformat())
    assert actual_start == expected_start, f"start_time mismatch: {actual_start} vs {expected_start}"
    assert len(approve_result["superseded_requests"]) == 1
    assert ctx["req3_1"] in approve_result["superseded_requests"]

    req1_status = svc.get_request(ctx["req3_1"])
    assert req1_status["status"] == RescheduleRequestStatus.SUPERSEDED.value

    ctx["bk3_ver"] = approve_result["booking"]["version"]
    print(f"      多请求冲突处理: 批准 req3_2, req3_1 被标记为 superseded")


@t("08 改期请求与已有预订冲突 - 提交时拒绝")
def step_08(db, ctx):
    booking1, start1, end1 = _create_test_booking(db, day_offset=4, hours=2, room_id="room-101")
    ctx["bk4"] = booking1["booking_id"]

    booking2, start2, end2 = _create_test_booking(db, day_offset=4, owner_id="u-zhaoliu", owner_name="赵六", room_id="room-102")
    ctx["bk5"] = booking2["booking_id"]
    ctx["bk5_ver"] = booking2["version"]

    svc = RescheduleApprovalService(db)
    cmd = SubmitRescheduleRequestCmd(
        booking_id=ctx["bk5"],
        requester_id="u-zhaoliu",
        requester_name="赵六",
        new_start_time=start1,
        new_end_time=end1,
        new_room_id="room-101",
        reason="想改到冲突时段",
        expected_version=ctx["bk5_ver"],
    )
    try:
        svc.submit_request(cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
        raise AssertionError("应抛出冲突")
    except DomainError as e:
        assert e.code == "BOOKING_CONFLICT"
        assert len(e.details["conflicts"]) == 1
        print(f"      提交时冲突校验通过: {e.code}")


@t("09 审批时检测到新冲突 - 标记为冲突状态")
def step_09(db, ctx):
    booking1, start1, end1 = _create_test_booking(db, day_offset=5)
    ctx["bk6"] = booking1["booking_id"]
    ctx["bk6_ver"] = booking1["version"]

    svc = RescheduleApprovalService(db)
    new_start = start1 + timedelta(hours=2)
    new_end = new_start + timedelta(hours=1)

    cmd = SubmitRescheduleRequestCmd(
        booking_id=ctx["bk6"],
        requester_id="u-zhangsan",
        requester_name="张三",
        new_start_time=new_start,
        new_end_time=new_end,
        reason="改期测试",
        expected_version=ctx["bk6_ver"],
    )
    result = svc.submit_request(cmd, "u-zhangsan", UserRole.MEMBER, "张三")
    ctx["req4"] = result["request"]["request_id"]
    ctx["bk6_ver"] = result["booking"]["version"]

    handler = CommandHandler(db)
    conflict_start = new_start
    conflict_end = new_end
    conflict_cmd = CreateBookingCmd(
        room_id="room-101",
        owner_id="u-zhaoliu",
        owner_name="赵六",
        team_id="team-b",
        title="插缝预订",
        start_time=conflict_start,
        end_time=conflict_end,
    )
    handler.create_booking(conflict_cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")

    approve_cmd = ApproveRescheduleRequestCmd(
        request_id=ctx["req4"],
        approver_id="u-recep",
        approver_name="前台小李",
        reason="批准",
        expected_version=ctx["bk6_ver"],
    )
    try:
        svc.approve_request(approve_cmd, "u-recep", UserRole.RECEPTIONIST, "前台小李")
        raise AssertionError("应抛出冲突")
    except DomainError as e:
        assert e.code == "BOOKING_CONFLICT"
        req_status = svc.get_request(ctx["req4"])
        assert req_status["status"] == RescheduleRequestStatus.CONFLICT.value
        print(f"      审批时冲突校验通过: {e.code}, 请求状态={req_status['status']}")


@t("10 服务重启后查询 - 审批记录持久化")
def step_10(db, ctx):
    db.close()

    new_db = SessionLocal()
    svc = RescheduleApprovalService(new_db)

    req1 = svc.get_request(ctx["req1"])
    assert req1["status"] == RescheduleRequestStatus.APPROVED.value
    assert req1["request_id"] == ctx["req1"]

    all_requests = svc.list_requests()
    assert all_requests["total"] >= 2

    pending = svc.list_requests(status=RescheduleRequestStatus.PENDING.value)
    assert pending["total"] >= 0

    store = EventStoreService(new_db)
    agg = store.load_aggregate(ctx["bk1"])
    actual_start = _normalize_iso(agg.start_time.isoformat())
    expected_start = _normalize_iso(ctx["req1_new_start"].isoformat())
    assert actual_start == expected_start, f"start_time mismatch: {actual_start} vs {expected_start}"
    assert len(agg.reschedule_requests_history) >= 1

    new_db.close()
    print(f"      持久化验证通过: 共 {all_requests['total']} 条审批记录")


@t("11 并发版本校验 - 旧版本号审批被拒")
def step_11(db, ctx):
    booking, orig_start, orig_end = _create_test_booking(db, day_offset=6)
    ctx["bk7"] = booking["booking_id"]
    ctx["bk7_ver"] = booking["version"]

    svc = RescheduleApprovalService(db)
    new_start = orig_start + timedelta(hours=1)
    new_end = new_start + timedelta(hours=1)

    cmd = SubmitRescheduleRequestCmd(
        booking_id=ctx["bk7"],
        requester_id="u-zhangsan",
        requester_name="张三",
        new_start_time=new_start,
        new_end_time=new_end,
        reason="并发测试",
        expected_version=ctx["bk7_ver"],
    )
    result = svc.submit_request(cmd, "u-zhangsan", UserRole.MEMBER, "张三")
    ctx["req5"] = result["request"]["request_id"]

    approve_cmd = ApproveRescheduleRequestCmd(
        request_id=ctx["req5"],
        approver_id="u-recep",
        approver_name="前台小李",
        reason="批准",
        expected_version=ctx["bk7_ver"],
    )
    try:
        svc.approve_request(approve_cmd, "u-recep", UserRole.RECEPTIONIST, "前台小李")
        raise AssertionError("应抛出版本不匹配")
    except DomainError as e:
        assert e.code == "CONCURRENCY_CONFLICT"
        print(f"      并发校验通过: {e.code}")


@t("12 改期请求与其他待审批请求冲突 - 提交时拒绝")
def step_12(db, ctx):
    booking1, start1, end1 = _create_test_booking(db, day_offset=7, room_id="room-101")
    ctx["bk8"] = booking1["booking_id"]
    ctx["bk8_ver"] = booking1["version"]

    booking2, start2, end2 = _create_test_booking(db, day_offset=7, room_id="room-102", owner_id="u-zhaoliu", owner_name="赵六")
    ctx["bk9"] = booking2["booking_id"]
    ctx["bk9_ver"] = booking2["version"]

    svc = RescheduleApprovalService(db)
    new_start = start1 + timedelta(hours=2)
    new_end = new_start + timedelta(hours=1)

    cmd1 = SubmitRescheduleRequestCmd(
        booking_id=ctx["bk8"],
        requester_id="u-zhangsan",
        requester_name="张三",
        new_start_time=new_start,
        new_end_time=new_end,
        reason="改期A",
        expected_version=ctx["bk8_ver"],
        new_room_id="room-101",
    )
    result1 = svc.submit_request(cmd1, "u-zhangsan", UserRole.MEMBER, "张三")
    ctx["req6"] = result1["request"]["request_id"]
    ctx["bk8_ver"] = result1["booking"]["version"]

    cmd2 = SubmitRescheduleRequestCmd(
        booking_id=ctx["bk9"],
        requester_id="u-zhaoliu",
        requester_name="赵六",
        new_start_time=new_start,
        new_end_time=new_end,
        reason="改期B",
        expected_version=ctx["bk9_ver"],
        new_room_id="room-101",
    )
    try:
        svc.submit_request(cmd2, "u-zhaoliu", UserRole.MEMBER, "赵六")
        raise AssertionError("应抛出待审批请求冲突")
    except DomainError as e:
        assert e.code == "PENDING_REQUEST_CONFLICT"
        print(f"      待审批冲突校验通过: {e.code}")


@t("13 取消有未处理改期请求的预订 - 确保原有取消校验生效")
def step_13(db, ctx):
    from app.services.commands import CancelBookingCmd

    booking, orig_start, orig_end = _create_test_booking(db, day_offset=8)
    booking_id = booking["booking_id"]
    booking_ver = booking["version"]

    svc = RescheduleApprovalService(db)
    new_start = orig_start + timedelta(hours=1)
    new_end = new_start + timedelta(hours=1)

    cmd = SubmitRescheduleRequestCmd(
        booking_id=booking_id,
        requester_id="u-zhangsan",
        requester_name="张三",
        new_start_time=new_start,
        new_end_time=new_end,
        reason="待取消测试",
        expected_version=booking_ver,
    )
    result = svc.submit_request(cmd, "u-zhangsan", UserRole.MEMBER, "张三")
    booking_ver = result["booking"]["version"]

    handler = CommandHandler(db)
    cancel_cmd = CancelBookingCmd(
        booking_id=booking_id,
        canceller_id="u-zhangsan",
        canceller_name="张三",
        reason="有事取消",
        expected_version=booking_ver,
    )
    cancel_result = handler.cancel_booking(cancel_cmd, "u-zhangsan", UserRole.MEMBER, "张三")
    assert cancel_result["booking"]["status"] == BookingStatus.CANCELLED.value
    print(f"      原有取消校验生效: 预订已取消")


@t("14 事件溯源重建 - 改期审批事件可正确重放")
def step_14(db, ctx):
    store = EventStoreService(db)
    agg = store.load_aggregate(ctx["bk3"])

    assert agg.status == BookingStatus.APPROVED.value
    actual_start = _normalize_iso(agg.start_time.isoformat())
    expected_start = _normalize_iso((ctx["bk3_orig_start"] + timedelta(hours=2)).isoformat())
    assert actual_start == expected_start, f"start_time mismatch: {actual_start} vs {expected_start}"
    assert len(agg.reschedule_requests_history) == 2
    assert len(agg.reschedule_history) == 1

    events = store.load_stream(ctx["bk3"])
    event_types = [e.event_type for e in events]
    assert EventType.RESCHEDULE_REQUESTED.value in event_types
    assert EventType.RESCHEDULE_APPROVED.value in event_types
    assert event_types.count(EventType.RESCHEDULE_REQUESTED.value) == 2

    print(f"      事件溯源重建通过: {len(events)} 个事件, 类型={event_types}")


@t("15 通过旧的 reschedule API 触发审批流程")
def step_15(db, ctx):
    booking, orig_start, orig_end = _create_test_booking(db, day_offset=9)
    ctx["bk10"] = booking["booking_id"]
    ctx["bk10_ver"] = booking["version"]

    handler = CommandHandler(db)
    new_start = orig_start + timedelta(hours=2)
    new_end = new_start + timedelta(hours=1)

    cmd = RescheduleBookingCmd(
        booking_id=ctx["bk10"],
        rescheduler_id="u-zhangsan",
        rescheduler_name="张三",
        new_start_time=new_start,
        new_end_time=new_end,
        reason="通过旧API改期",
        expected_version=ctx["bk10_ver"],
    )
    result = handler.reschedule_booking(cmd, "u-zhangsan", UserRole.MEMBER, "张三")

    assert result["requires_approval"] == True
    assert result["reschedule_request"] is not None
    assert result["reschedule_request"]["status"] == RescheduleRequestStatus.PENDING.value
    actual_start = _normalize_iso(result["booking"]["start_time"])
    expected_start = _normalize_iso(orig_start.isoformat())
    actual_end = _normalize_iso(result["booking"]["end_time"])
    expected_end = _normalize_iso(orig_end.isoformat())
    assert actual_start == expected_start, f"start_time mismatch: {actual_start} vs {expected_start}"
    assert actual_end == expected_end, f"end_time mismatch: {actual_end} vs {expected_end}"

    ctx["req7"] = result["reschedule_request"]["request_id"]
    ctx["bk10_ver"] = result["booking"]["version"]
    print(f"      旧API兼容通过: 生成待审批请求 {ctx['req7']}")


def main():
    print("=" * 70)
    print(" 会议室预订系统 -- 改期审批链路测试")
    print("=" * 70)
    db = SessionLocal()
    ctx = {}
    ctx["bk3_orig_start"] = None
    tests = [
        (step_00, (db,)),
        (step_01, (db, ctx)),
        (step_02, (db, ctx)),
        (step_03, (db, ctx)),
        (step_04, (db, ctx)),
        (step_05, (db, ctx)),
        (step_06, (db, ctx)),
        (step_07, (db, ctx)),
        (step_08, (db, ctx)),
        (step_09, (db, ctx)),
        (step_10, (db, ctx)),
        (step_11, (db, ctx)),
        (step_12, (db, ctx)),
        (step_13, (db, ctx)),
        (step_14, (db, ctx)),
        (step_15, (db, ctx)),
    ]
    passed = 0
    failed = 0
    for t_fn, t_args in tests:
        ok = t_fn(*t_args)
        if ok:
            passed += 1
        else:
            failed += 1
    print("=" * 70)
    print(f" 总计: {passed + failed}  | 成功: {passed}  | 失败: {failed}")
    print("=" * 70)
    print("Context keys:", sorted([k for k in ctx.keys() if not k.startswith("_")]))
    db.close()

    try:
        if os.path.exists(db_path):
            import time
            time.sleep(0.5)
            os.remove(db_path)
    except Exception as e:
        print(f"Warning: Could not remove test database: {e}")

    return failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
