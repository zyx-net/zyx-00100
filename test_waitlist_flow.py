"""
候补队列全链路测试脚本
覆盖: 提交、重复提交、权限隔离、冲突匹配、确认占位、过期失效、重启后查询
运行: python test_waitlist_flow.py
直接调用内部服务层（无 HTTP 开销）。
"""
from __future__ import annotations
import sys, os, json, time
from datetime import datetime, timezone, timedelta

db_path = os.path.join(os.path.dirname(__file__), "waitlist_flow_test.db")
if os.path.exists(db_path):
    try:
        time.sleep(0.5)
        os.remove(db_path)
    except Exception as e:
        print(f"Warning: Could not remove existing database: {e}")

os.environ["APP_DATABASE_URL"] = f"sqlite:///{db_path}"

from app.config import settings
settings.database_url = f"sqlite:///{db_path}"

import importlib
import app.db
importlib.reload(app.db)

from app.db import init_db, SessionLocal, WaitlistEntry, WaitlistMatchLog, WaitlistActionLog
from app.seed import seed_users
from app.services.command_handler import CommandHandler, DomainError
from app.services.commands import (
    CreateBookingCmd, CancelBookingCmd, ReleaseBookingCmd,
    SubmitWaitlistCmd, ConfirmWaitlistCmd, CancelWaitlistCmd, RejectWaitlistCmd,
)
from app.services.waitlist_service import WaitlistService, CONFIRMATION_WINDOW_MINUTES
from app.domain.permissions import UserRole, BookingStatus, WaitlistStatus
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
                return False
            except DomainError as e:
                print(f"[{FAIL}] {desc} -> 未预期 DomainError: {e.code} {e.message}")
                return False
            except Exception as e:
                print(f"[{FAIL}] {desc} -> EXC: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                return False
        return inner
    return wrap


def make_slot(days_ahead: int, hour: int, minute: int = 0, duration_hours: int = 1):
    start = datetime.now(TZ) + timedelta(days=days_ahead)
    start = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end = start + timedelta(hours=duration_hours)
    return start, end


@t("00 初始化数据库与用户")
def step_00(db, ctx):
    init_db()
    seed_users(db)


@t("01 张三创建 room-101 10:00-11:00 的预订（作为被占用的时段）")
def step_01(db, ctx):
    handler = CommandHandler(db)
    start, end = make_slot(2, 10)
    cmd = CreateBookingCmd(
        room_id="room-101",
        owner_id="u-zhangsan",
        owner_name="张三",
        team_id="team-a",
        title="项目启动会",
        start_time=start,
        end_time=end,
        attendees=["u-lisi"],
        description="第一阶段",
    )
    result = handler.create_booking(cmd, "u-zhangsan", UserRole.MEMBER, "张三")
    b = result["booking"]
    assert b["status"] == "approved"
    ctx["bk_zhangsan"] = b["booking_id"]
    ctx["slot_start"] = start
    ctx["slot_end"] = end


@t("02 李四尝试提交候补 - 成功")
def step_02(db, ctx):
    svc = WaitlistService(db)
    start, end = ctx["slot_start"], ctx["slot_end"]
    cmd = SubmitWaitlistCmd(
        room_id="room-101",
        requester_id="u-lisi",
        requester_name="李四",
        team_id="team-a",
        title="李四的紧急会议",
        desired_start_time=start,
        desired_end_time=end,
        flex_before_minutes=30,
        flex_after_minutes=30,
        attendees=["u-wangwu"],
        priority_note="客户来访，优先级高",
        contact_info="lisi@example.com",
        description="重要客户沟通",
    )
    result = svc.submit_waitlist(cmd, "u-lisi", UserRole.MEMBER, "李四")
    wl = result["waitlist"]
    assert wl["status"] == WaitlistStatus.WAITING.value
    assert wl["requester_id"] == "u-lisi"
    assert wl["flex_before_minutes"] == 30
    ctx["wl_lisi"] = wl["waitlist_id"]
    print(f"    -> waitlist_id={wl['waitlist_id']}")


@t("03 李四对同一时间窗重复候补 - 被拒绝（防脏数据）")
def step_03(db, ctx):
    svc = WaitlistService(db)
    start, end = ctx["slot_start"], ctx["slot_end"]
    cmd = SubmitWaitlistCmd(
        room_id="room-101",
        requester_id="u-lisi",
        requester_name="李四",
        title="李四的重复尝试",
        desired_start_time=start,
        desired_end_time=end,
        flex_before_minutes=60,
        flex_after_minutes=0,
    )
    try:
        svc.submit_waitlist(cmd, "u-lisi", UserRole.MEMBER, "李四")
        assert False, "应该抛出 DUPLICATE_WAITLIST"
    except DomainError as e:
        assert e.code == "DUPLICATE_WAITLIST"
        assert "existing_waitlist_id" in e.details
        assert e.details["existing_waitlist_id"] == ctx["wl_lisi"]


@t("04 王五在同一时段提交候补（不同用户，可以）")
def step_04(db, ctx):
    svc = WaitlistService(db)
    start, end = ctx["slot_start"], ctx["slot_end"]
    cmd = SubmitWaitlistCmd(
        room_id="room-101",
        requester_id="u-wangwu",
        requester_name="王五",
        title="王五的会议",
        desired_start_time=start,
        desired_end_time=end,
        flex_before_minutes=0,
        flex_after_minutes=0,
        priority_note="日常站会",
    )
    result = svc.submit_waitlist(cmd, "u-wangwu", UserRole.MEMBER, "王五")
    assert result["waitlist"]["status"] == WaitlistStatus.WAITING.value
    ctx["wl_wangwu"] = result["waitlist"]["waitlist_id"]


@t("05 权限隔离 - 李四只能看到自己的候补，看不到王五的")
def step_05(db, ctx):
    svc = WaitlistService(db)
    lisi_list = svc.list_waitlists("u-lisi", UserRole.MEMBER)
    lisi_ids = [w["waitlist_id"] for w in lisi_list["items"]]
    assert ctx["wl_lisi"] in lisi_ids
    assert ctx["wl_wangwu"] not in lisi_ids

    wangwu_list = svc.list_waitlists("u-wangwu", UserRole.MEMBER)
    wangwu_ids = [w["waitlist_id"] for w in wangwu_list["items"]]
    assert ctx["wl_wangwu"] in wangwu_ids
    assert ctx["wl_lisi"] not in wangwu_ids


@t("06 管理员可以按房间筛选看到所有候补")
def step_06(db, ctx):
    svc = WaitlistService(db)
    admin_list = svc.list_waitlists(
        "u-admin", UserRole.SYSTEM_ADMIN, room_id="room-101"
    )
    admin_ids = [w["waitlist_id"] for w in admin_list["items"]]
    assert ctx["wl_lisi"] in admin_ids
    assert ctx["wl_wangwu"] in admin_ids
    assert admin_list["total"] >= 2


@t("07 普通成员尝试查看他人候补 - 权限拒绝")
def step_07(db, ctx):
    svc = WaitlistService(db)
    try:
        svc.get_waitlist(ctx["wl_wangwu"], "u-lisi", UserRole.MEMBER)
        assert False, "应该抛出 PERMISSION_DENIED"
    except DomainError as e:
        assert e.code == "PERMISSION_DENIED"


@t("08 目标时段无冲突时不允许候补（引导直接预订）")
def step_08(db, ctx):
    svc = WaitlistService(db)
    start, end = make_slot(5, 14)
    cmd = SubmitWaitlistCmd(
        room_id="room-101",
        requester_id="u-lisi",
        requester_name="李四",
        title="空闲时段尝试候补",
        desired_start_time=start,
        desired_end_time=end,
    )
    try:
        svc.submit_waitlist(cmd, "u-lisi", UserRole.MEMBER, "李四")
        assert False, "应该抛出 NO_CONFLICT"
    except DomainError as e:
        assert e.code == "NO_CONFLICT"


@t("09 张三取消预订 - 自动触发候补匹配，李四（先提交+有浮动）应被匹配")
def step_09(db, ctx):
    handler = CommandHandler(db)
    svc = WaitlistService(db)

    bk = ctx["bk_zhangsan"]
    agg = svc.store.load_aggregate(bk)

    cmd = CancelBookingCmd(
        booking_id=bk,
        canceller_id="u-zhangsan",
        canceller_name="张三",
        reason="临时有事",
        expected_version=agg.version,
    )
    result = handler.cancel_booking(cmd, "u-zhangsan", UserRole.MEMBER, "张三")
    assert result["booking"]["status"] == BookingStatus.CANCELLED.value

    wl_lisi = svc._get_entry(ctx["wl_lisi"])
    print(f"    -> 李四候补状态: {wl_lisi.status}")
    assert wl_lisi.status == WaitlistStatus.MATCHED.value
    assert wl_lisi.matched_booking_id == bk
    assert wl_lisi.expire_at is not None

    wl_wangwu = svc._get_entry(ctx["wl_wangwu"])
    print(f"    -> 王五候补状态: {wl_wangwu.status}")
    assert wl_wangwu.status == WaitlistStatus.WAITING.value

    match_logs = db.query(WaitlistMatchLog).filter(
        WaitlistMatchLog.waitlist_id == ctx["wl_lisi"]
    ).all()
    assert len(match_logs) >= 1
    assert match_logs[-1].match_status == "matched"
    assert match_logs[-1].trigger_event == "BOOKING_CANCELLED"

    action_logs = db.query(WaitlistActionLog).filter(
        WaitlistActionLog.waitlist_id == ctx["wl_lisi"]
    ).all()
    actions = [a.action for a in action_logs]
    assert "SUBMIT" in actions
    assert "MATCH" in actions


@t("10 李四确认候补 - 生成正式预订，候补状态变为 confirmed")
def step_10(db, ctx):
    svc = WaitlistService(db)
    cmd = ConfirmWaitlistCmd(
        waitlist_id=ctx["wl_lisi"],
        confirmer_id="u-lisi",
        confirmer_name="李四",
        reason="确认接受该时段",
    )
    result = svc.confirm_waitlist(cmd, "u-lisi", UserRole.MEMBER, "李四")
    assert result["waitlist"]["status"] == WaitlistStatus.CONFIRMED.value
    assert result["waitlist"]["result_booking_id"] is not None
    assert result["booking"] is not None
    assert result["booking"]["status"] == "approved"
    assert result["booking"]["owner_id"] == "u-lisi"
    ctx["bk_from_waitlist"] = result["booking"]["booking_id"]
    print(f"    -> 生成预订 booking_id={result['booking']['booking_id']}")


@t("11 确认后生成的预订在日程中真实存在，冲突检查生效")
def step_11(db, ctx):
    svc = WaitlistService(db)
    conflicts = svc.store.find_conflicting_bookings(
        "room-101", ctx["slot_start"], ctx["slot_end"]
    )
    conflict_ids = [c.booking_id for c in conflicts]
    assert ctx["bk_from_waitlist"] in conflict_ids


@t("12 过期失效测试 - 构造一个已过期的 matched 候补，清理后变 expired")
def step_12(db, ctx):
    svc = WaitlistService(db)
    start, end = make_slot(3, 15)

    owner_cmd = CreateBookingCmd(
        room_id="room-102",
        owner_id="u-admin",
        owner_name="管理员",
        title="占位用",
        start_time=start,
        end_time=end,
    )
    handler = CommandHandler(db)
    owner_r = handler.create_booking(owner_cmd, "u-admin", UserRole.SYSTEM_ADMIN, "管理员")
    ctx["bk_owner_expire"] = owner_r["booking"]["booking_id"]

    wl_cmd = SubmitWaitlistCmd(
        room_id="room-102",
        requester_id="u-zhaoliu",
        requester_name="赵六",
        title="赵六要候补",
        desired_start_time=start,
        desired_end_time=end,
        flex_before_minutes=60,
        flex_after_minutes=60,
    )
    wl_r = svc.submit_waitlist(wl_cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
    ctx["wl_expire_test"] = wl_r["waitlist"]["waitlist_id"]

    agg = svc.store.load_aggregate(ctx["bk_owner_expire"])
    cancel_cmd = CancelBookingCmd(
        booking_id=ctx["bk_owner_expire"],
        canceller_id="u-admin",
        canceller_name="管理员",
        reason="释放",
        expected_version=agg.version,
    )
    handler.cancel_booking(cancel_cmd, "u-admin", UserRole.SYSTEM_ADMIN, "管理员")

    entry = svc._get_entry(ctx["wl_expire_test"])
    assert entry.status == WaitlistStatus.MATCHED.value

    from app.db import now_utc as _now_utc
    entry.expire_at = _now_utc() - timedelta(minutes=5)
    db.commit()

    expired_count = svc.expire_stale_waitlists()
    assert expired_count >= 1

    entry = svc._get_entry(ctx["wl_expire_test"])
    assert entry.status == WaitlistStatus.EXPIRED.value
    assert entry.expired_at is not None
    assert entry.expire_reason == "确认超时自动失效"


@t("13 过期的候补不能确认")
def step_13(db, ctx):
    svc = WaitlistService(db)
    cmd = ConfirmWaitlistCmd(
        waitlist_id=ctx["wl_expire_test"],
        confirmer_id="u-zhaoliu",
        confirmer_name="赵六",
    )
    try:
        svc.confirm_waitlist(cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
        assert False, "应该抛出 WAITLIST_EXPIRED"
    except DomainError as e:
        assert e.code == "WAITLIST_EXPIRED"


@t("14 用户主动取消候补")
def step_14(db, ctx):
    svc = WaitlistService(db)
    start, end = make_slot(4, 16)

    owner_cmd = CreateBookingCmd(
        room_id="room-102",
        owner_id="u-admin",
        owner_name="管理员",
        title="占位用2",
        start_time=start,
        end_time=end,
    )
    handler = CommandHandler(db)
    handler.create_booking(owner_cmd, "u-admin", UserRole.SYSTEM_ADMIN, "管理员")

    wl_cmd = SubmitWaitlistCmd(
        room_id="room-102",
        requester_id="u-zhaoliu",
        requester_name="赵六",
        title="赵六要取消的候补",
        desired_start_time=start,
        desired_end_time=end,
    )
    wl_r = svc.submit_waitlist(wl_cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
    wl_id = wl_r["waitlist"]["waitlist_id"]

    cancel_cmd = CancelWaitlistCmd(
        waitlist_id=wl_id,
        canceller_id="u-zhaoliu",
        canceller_name="赵六",
        reason="不需要了",
    )
    result = svc.cancel_waitlist(cancel_cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
    assert result["waitlist"]["status"] == WaitlistStatus.CANCELLED.value


@t("15 管理员驳回候补")
def step_15(db, ctx):
    svc = WaitlistService(db)
    start, end = make_slot(4, 17)

    owner_cmd = CreateBookingCmd(
        room_id="room-102",
        owner_id="u-admin",
        owner_name="管理员",
        title="占位用3",
        start_time=start,
        end_time=end,
    )
    handler = CommandHandler(db)
    handler.create_booking(owner_cmd, "u-admin", UserRole.SYSTEM_ADMIN, "管理员")

    wl_cmd = SubmitWaitlistCmd(
        room_id="room-102",
        requester_id="u-zhaoliu",
        requester_name="赵六",
        title="赵六要被驳回的候补",
        desired_start_time=start,
        desired_end_time=end,
    )
    wl_r = svc.submit_waitlist(wl_cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
    wl_id = wl_r["waitlist"]["waitlist_id"]

    reject_cmd = RejectWaitlistCmd(
        waitlist_id=wl_id,
        rejecter_id="u-admin",
        rejecter_name="管理员",
        reason="资源调配原因",
    )
    result = svc.reject_waitlist(reject_cmd, "u-admin", UserRole.SYSTEM_ADMIN, "管理员")
    assert result["waitlist"]["status"] == WaitlistStatus.REJECTED.value


@t("16 普通成员尝试驳回候补 - 权限拒绝")
def step_16(db, ctx):
    svc = WaitlistService(db)
    reject_cmd = RejectWaitlistCmd(
        waitlist_id=ctx["wl_wangwu"],
        rejecter_id="u-lisi",
        rejecter_name="李四",
        reason="我想驳回",
    )
    try:
        svc.reject_waitlist(reject_cmd, "u-lisi", UserRole.MEMBER, "李四")
        assert False, "应该抛出 PERMISSION_DENIED"
    except DomainError as e:
        assert e.code == "PERMISSION_DENIED"


@t("17 持久化验证 - 关闭会话重开后仍能查到候补记录")
def step_17(db, ctx):
    db.close()

    import app.db as db_module
    importlib.reload(db_module)
    from app.db import SessionLocal as SessionLocal2

    db2 = SessionLocal2()
    try:
        svc = WaitlistService(db2)
        result = svc.get_waitlist(ctx["wl_lisi"], "u-lisi", UserRole.MEMBER)
        assert result["waitlist"]["waitlist_id"] == ctx["wl_lisi"]
        assert result["waitlist"]["status"] == WaitlistStatus.CONFIRMED.value
        assert result["waitlist"]["result_booking_id"] == ctx["bk_from_waitlist"]

        admin_list = svc.list_waitlists("u-admin", UserRole.SYSTEM_ADMIN)
        assert admin_list["total"] >= 5

        match_logs = db2.query(WaitlistMatchLog).filter(
            WaitlistMatchLog.waitlist_id == ctx["wl_lisi"]
        ).all()
        assert len(match_logs) >= 1

        action_logs = db2.query(WaitlistActionLog).filter(
            WaitlistActionLog.waitlist_id == ctx["wl_lisi"]
        ).all()
        assert len(action_logs) >= 2
    finally:
        db2.close()

    db = SessionLocal()
    ctx["db"] = db


@t("18 改期触发候补匹配 - 预留测试（释放原时段）")
def step_18(db, ctx):
    from app.services.commands import RescheduleBookingCmd
    handler = CommandHandler(db)
    svc = WaitlistService(db)

    start, end = make_slot(6, 9)
    new_start, new_end = make_slot(6, 13)

    owner_cmd = CreateBookingCmd(
        room_id="room-101",
        owner_id="u-zhangsan",
        owner_name="张三",
        title="要被改期的预订",
        start_time=start,
        end_time=end,
    )
    owner_r = handler.create_booking(owner_cmd, "u-zhangsan", UserRole.MEMBER, "张三")
    ctx["bk_to_reschedule"] = owner_r["booking"]["booking_id"]

    wl_cmd = SubmitWaitlistCmd(
        room_id="room-101",
        requester_id="u-zhaoliu",
        requester_name="赵六",
        title="改期释放候补",
        desired_start_time=start,
        desired_end_time=end,
        flex_before_minutes=60,
        flex_after_minutes=60,
    )
    wl_r = svc.submit_waitlist(wl_cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
    ctx["wl_reschedule_test"] = wl_r["waitlist"]["waitlist_id"]

    agg = svc.store.load_aggregate(ctx["bk_to_reschedule"])
    resched_cmd = RescheduleBookingCmd(
        booking_id=ctx["bk_to_reschedule"],
        rescheduler_id="u-admin",
        rescheduler_name="管理员",
        new_start_time=new_start,
        new_end_time=new_end,
        reason="改期到下午",
        expected_version=agg.version,
    )
    handler.reschedule_booking(resched_cmd, "u-admin", UserRole.SYSTEM_ADMIN, "管理员")

    entry = svc._get_entry(ctx["wl_reschedule_test"])
    assert entry.status == WaitlistStatus.MATCHED.value
    match_logs = db.query(WaitlistMatchLog).filter(
        WaitlistMatchLog.waitlist_id == ctx["wl_reschedule_test"]
    ).all()
    triggers = [m.trigger_event for m in match_logs]
    assert "BOOKING_RESCHEDULED" in triggers


def main():
    print("=" * 70)
    print("候补队列全链路测试")
    print("=" * 70)

    db = SessionLocal()
    ctx = {}
    passed = 0
    total = 0

    steps = [
        step_00, step_01, step_02, step_03, step_04,
        step_05, step_06, step_07, step_08, step_09,
        step_10, step_11, step_12, step_13, step_14,
        step_15, step_16, step_17, step_18,
    ]

    try:
        for s in steps:
            total += 1
            try:
                ok = s(db, ctx)
            except Exception as _e:
                ok = False
            if ok:
                passed += 1
    finally:
        db.close()

    print("=" * 70)
    print(f"测试结果: {passed}/{total} 通过")
    if passed == total:
        print("所有候补队列测试通过 ✓")
    else:
        print(f"有 {total - passed} 个测试失败 ✗")
    print("=" * 70)
    return passed == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
