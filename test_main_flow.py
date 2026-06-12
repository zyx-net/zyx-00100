"""
端到端主链路 + 错误场景测试脚本
运行: python test_main_flow.py
直接调用内部服务层（无 HTTP 开销）。
"""
from __future__ import annotations
import sys, os, json
from datetime import datetime, timezone, timedelta

db_path = os.path.join(os.path.dirname(__file__), "room_booking.db")
if os.path.exists(db_path):
    os.remove(db_path)

from app.db import init_db, SessionLocal
from app.seed import seed_users
from app.services.command_handler import CommandHandler, DomainError
from app.services.commands import (
    CreateBookingCmd, ApproveBookingCmd, RejectBookingCmd,
    RescheduleBookingCmd, CancelBookingCmd, CheckInCmd,
    ReleaseBookingCmd, ArbitrateCmd,
)
from app.services.queries import QueryService
from app.services.arbitration import ArbitrationService
from app.services.event_store import EventStoreService
from app.domain.permissions import UserRole, BookingStatus
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


@t("00 初始化数据库与用户")
def step_00(db):
    init_db()
    seed_users(db)


@t("01 主链路-1 成员张三创建无需审批的 room-101 预订")
def step_01(db, ctx):
    handler = CommandHandler(db)
    start = datetime.now(TZ) + timedelta(days=1)
    start = start.replace(hour=10, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    cmd = CreateBookingCmd(
        room_id="room-101",
        owner_id="u-zhangsan",
        owner_name="张三",
        team_id="team-a",
        title="产品周会",
        start_time=start,
        end_time=end,
        attendees=["u-lisi"],
        description="迭代同步",
    )
    result = handler.create_booking(cmd, "u-zhangsan", UserRole.MEMBER, "张三")
    b = result["booking"]
    assert b["status"] == "approved", f"期待 approved，got {b['status']}"
    assert b["version"] == 1
    ctx["bk1"] = b["booking_id"]
    ctx["bk1_start"] = start
    ctx["bk1_end"] = end
    ctx["bk1_ver"] = 1


@t("02 主链路-2 team_admin 王五创建需要审批的 room-201 预订")
def step_02(db, ctx):
    handler = CommandHandler(db)
    start = datetime.now(TZ) + timedelta(days=1)
    start = start.replace(hour=14, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=2)
    cmd = CreateBookingCmd(
        room_id="room-201",
        owner_id="u-wangwu",
        owner_name="王五",
        team_id="team-a",
        title="客户路演",
        start_time=start,
        end_time=end,
        attendees=["u-zhangsan"],
    )
    result = handler.create_booking(cmd, "u-wangwu", UserRole.TEAM_ADMIN, "王五")
    b = result["booking"]
    assert b["status"] == "pending_approval"
    assert b["version"] == 1
    assert b["require_approval"] == True
    ctx["bk2"] = b["booking_id"]
    ctx["bk2_ver"] = 1


@t("03 主链路-3 前台小李审批 room-201 预订")
def step_03(db, ctx):
    handler = CommandHandler(db)
    cmd = ApproveBookingCmd(
        booking_id=ctx["bk2"],
        approver_id="u-recep",
        approver_name="前台小李",
        reason="资源充足",
        expected_version=ctx["bk2_ver"],
    )
    result = handler.approve_booking(cmd, "u-recep", UserRole.RECEPTIONIST, "前台小李")
    b = result["booking"]
    assert b["status"] == "approved"
    assert b["version"] == 2
    ctx["bk2_ver"] = 2


@t("04 主链路-4 张三对 room-101 预订签到（在宽限期内）")
def step_04(db, ctx):
    handler = CommandHandler(db)
    check_time = ctx["bk1_start"] + timedelta(minutes=5)
    cmd = CheckInCmd(
        booking_id=ctx["bk1"],
        check_in_user_id="u-zhangsan",
        check_in_user_name="张三",
        check_in_time=check_time,
        expected_version=ctx["bk1_ver"],
    )
    result = handler.check_in(cmd, "u-zhangsan", UserRole.MEMBER, "张三")
    b = result["booking"]
    assert b["status"] == "checked_in"
    assert b["version"] == 2
    ctx["bk1_ver"] = 2


@t("05 主链路-5 创建第三个已批准的预订模拟过期未签到")
def step_05(db, ctx):
    handler = CommandHandler(db)
    start = datetime.now(TZ) + timedelta(days=1)
    start = start.replace(hour=9, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    cmd = CreateBookingCmd(
        room_id="room-102",
        owner_id="u-zhaoliu",
        owner_name="赵六",
        team_id="team-b",
        title="项目对齐",
        start_time=start,
        end_time=end,
    )
    result = handler.create_booking(cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
    b = result["booking"]
    assert b["status"] == "approved"
    ctx["bk3"] = b["booking_id"]
    ctx["bk3_start"] = start
    ctx["bk3_ver"] = 1


@t("06 主链路-6 前台小李释放过期未签到房间 room-102")
def step_06(db, ctx):
    handler = CommandHandler(db)
    release_time = ctx["bk3_start"] + timedelta(minutes=30)
    cmd = ReleaseBookingCmd(
        booking_id=ctx["bk3"],
        released_by_id="u-recep",
        released_by_name="前台小李",
        reason="过宽限期未签到",
        release_time=release_time,
        expected_version=ctx["bk3_ver"],
    )
    result = handler.release_booking(cmd, "u-recep", UserRole.RECEPTIONIST, "前台小李")
    b = result["booking"]
    assert b["status"] == "released"
    ctx["bk3_ver"] = 2


@t("07 主链路-7 导出日程 CSV/JSON 格式一致")
def step_07(db, ctx):
    svc = QueryService(db)
    csv_result = svc.export_schedule(format="csv")
    json_result = svc.export_schedule(format="json")
    assert csv_result["row_count"] == json_result["row_count"]
    assert csv_result["row_count"] >= 3
    assert csv_result["rule_version"] == json_result["rule_version"]
    assert json_result["rule_version"] == settings.rule_version
    ctx["export_row_count"] = csv_result["row_count"]
    print(f"      导出 {csv_result['row_count']} 行，rule_version={csv_result['rule_version']}")


@t("08 回归-成员改自己的预订成功（原 UnboundLocalError 场景：member 无 RESCHEDULE_BOOKING 权限走 fallback）")
def step_08(db, ctx):
    handler = CommandHandler(db)
    # 创建专属临时 booking 给张三改期（不依赖 bk1 的 checked_in 状态）
    t_start = (datetime.now(TZ) + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    t_end = t_start + timedelta(hours=1)
    r = handler.create_booking(
        CreateBookingCmd(
            room_id="room-102", owner_id="u-zhangsan", owner_name="张三",
            team_id="team-a", title="改期测试-成员自改",
            start_time=t_start, end_time=t_end,
        ),
        "u-zhangsan", UserRole.MEMBER, "张三",
    )
    tmp_id = r["booking"]["booking_id"]
    tmp_ver = r["booking"]["version"]
    # 张三（member）改自己的：member 没有 RESCHEDULE_BOOKING，会触发 agg.owner_id 判断分支
    new_start = t_start + timedelta(hours=1)
    new_end = new_start + timedelta(hours=1)
    cmd = RescheduleBookingCmd(
        booking_id=tmp_id,
        rescheduler_id="u-zhangsan",
        rescheduler_name="张三",
        new_start_time=new_start,
        new_end_time=new_end,
        reason="成员改自己的预订",
        expected_version=tmp_ver,
    )
    result = handler.reschedule_booking(cmd, "u-zhangsan", UserRole.MEMBER, "张三")
    b = result["booking"]
    assert b["status"] == "approved"
    assert b["version"] == tmp_ver + 1
    assert b["start_time"] == new_start.isoformat()
    assert len(b["reschedule_history"]) == 1
    print(f"      成员自改: {tmp_id} v{tmp_ver}→v{b['version']} 时段={new_start.strftime('%H:%M')}-{new_end.strftime('%H:%M')}")


@t("09 回归-成员赵六改张三的预订被拒绝 PERMISSION_DENIED")
def step_09(db, ctx):
    handler = CommandHandler(db)
    # 创建张三的专属临时 booking
    t_start = (datetime.now(TZ) + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
    t_end = t_start + timedelta(hours=1)
    r = handler.create_booking(
        CreateBookingCmd(
            room_id="room-102", owner_id="u-zhangsan", owner_name="张三",
            team_id="team-a", title="改期测试-他人改",
            start_time=t_start, end_time=t_end,
        ),
        "u-zhangsan", UserRole.MEMBER, "张三",
    )
    tmp_id = r["booking"]["booking_id"]
    tmp_ver = r["booking"]["version"]
    # 赵六（member，非 owner）改张三的 → 被拒
    new_start = t_start + timedelta(hours=2)
    new_end = new_start + timedelta(hours=1)
    cmd = RescheduleBookingCmd(
        booking_id=tmp_id,
        rescheduler_id="u-zhaoliu",
        rescheduler_name="赵六",
        new_start_time=new_start,
        new_end_time=new_end,
        expected_version=tmp_ver,
    )
    try:
        handler.reschedule_booking(cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
        raise AssertionError("应抛出无权改期")
    except DomainError as e:
        assert e.code == "PERMISSION_DENIED", f"期待 PERMISSION_DENIED，got {e.code}: {e.message}"
        print(f"      成员改他人: {e.code}")


@t("10 错误场景-1 重叠预订返回 BOOKING_CONFLICT")
def step_10(db, ctx):
    handler = CommandHandler(db)
    start = ctx["bk1_start"]
    end = ctx["bk1_end"]
    cmd = CreateBookingCmd(
        room_id="room-101",
        owner_id="u-zhaoliu",
        owner_name="赵六",
        title="冲突会议",
        start_time=start,
        end_time=end,
    )
    try:
        handler.create_booking(cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
        raise AssertionError("应抛出冲突")
    except DomainError as e:
        assert e.code == "BOOKING_CONFLICT", f"期待 BOOKING_CONFLICT，got {e.code}"
        assert "conflicts" in e.details
        print(f"      冲突详情数量: {len(e.details['conflicts'])}")


@t("11 错误场景-2 过期签到返回 CHECK_IN_GRACE_EXPIRED")
def step_11(db, ctx):
    handler = CommandHandler(db)
    start = datetime.now(TZ) + timedelta(days=2)
    start = start.replace(hour=16, minute=0)
    end = start + timedelta(hours=1)
    create_cmd = CreateBookingCmd(
        room_id="room-202",
        owner_id="u-zhaoliu",
        owner_name="赵六",
        title="待过期签到",
        start_time=start,
        end_time=end,
    )
    r = handler.create_booking(create_cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
    b = r["booking"]
    check_time = start + timedelta(minutes=30)
    cmd = CheckInCmd(
        booking_id=b["booking_id"],
        check_in_user_id="u-zhaoliu",
        check_in_user_name="赵六",
        check_in_time=check_time,
        expected_version=1,
    )
    try:
        handler.check_in(cmd, "u-zhaoliu", UserRole.MEMBER, "赵六")
        raise AssertionError("应抛出宽限期过期")
    except DomainError as e:
        assert e.code == "CHECK_IN_GRACE_EXPIRED", f"期待 CHECK_IN_GRACE_EXPIRED，got {e.code}"


@t("12 错误场景-3 旧版本号更新返回 CONCURRENCY_CONFLICT")
def step_12(db, ctx):
    handler = CommandHandler(db)
    # bk1 状态是 checked_in（version=2），可以对其发送 release 命令
    # 故意传 expected_version=0 触发并发冲突
    cmd = ReleaseBookingCmd(
        booking_id=ctx["bk1"],
        released_by_id="u-recep",
        released_by_name="前台小李",
        reason="版本冲突测试",
        release_time=ctx["bk1_start"] + timedelta(minutes=40),
        expected_version=0,
    )
    try:
        handler.release_booking(cmd, "u-recep", UserRole.RECEPTIONIST, "前台小李")
        raise AssertionError("应抛出并发冲突")
    except DomainError as e:
        assert e.code == "CONCURRENCY_CONFLICT", f"期待 CONCURRENCY_CONFLICT，got {e.code}"


@t("13 错误场景-4 越权仲裁（非系统管理员）返回 PERMISSION_DENIED")
def step_13(db, ctx):
    handler = CommandHandler(db)
    cmd = ArbitrateCmd(
        booking_id=ctx["bk2"],
        arbitrator_id="u-wangwu",
        arbitrator_name="王五",
        decision="OVERRULE",
        reason="测试",
        expected_version=ctx["bk2_ver"],
    )
    try:
        handler.arbitrate(cmd, "u-wangwu", UserRole.TEAM_ADMIN, "王五")
        raise AssertionError("应抛出越权")
    except DomainError as e:
        assert e.code == "PERMISSION_DENIED"
        assert "系统管理员" in e.message


@t("14 回归-改期传入结束早于开始返回 INVALID_TIME_RANGE，不产生 UnboundLocalError")
def step_14(db, ctx):
    handler = CommandHandler(db)
    # 先创建张三的临时 booking
    t_start = (datetime.now(TZ) + timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
    t_end = t_start + timedelta(hours=1)
    r = handler.create_booking(
        CreateBookingCmd(
            room_id="room-102", owner_id="u-zhangsan", owner_name="张三",
            team_id="team-a", title="改期非法时间测试",
            start_time=t_start, end_time=t_end,
        ),
        "u-zhangsan", UserRole.MEMBER, "张三",
    )
    tmp_id = r["booking"]["booking_id"]
    tmp_ver = r["booking"]["version"]
    # 传入 end < start，先通过权限/所有者校验 → 非法时间校验
    new_start = (datetime.now(TZ) + timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    new_end = new_start - timedelta(hours=1)
    cmd = RescheduleBookingCmd(
        booking_id=tmp_id,
        rescheduler_id="u-zhangsan",
        rescheduler_name="张三",
        new_start_time=new_start,
        new_end_time=new_end,
        expected_version=tmp_ver,
    )
    try:
        handler.reschedule_booking(cmd, "u-zhangsan", UserRole.MEMBER, "张三")
        raise AssertionError("应抛出非法时间")
    except DomainError as e:
        assert e.code == "INVALID_TIME_RANGE", f"期待 INVALID_TIME_RANGE，got {e.code}"
    # 关键验证：聚合没被破坏，版本号不变
    agg = EventStoreService(db).load_aggregate(tmp_id)
    assert agg.version == tmp_ver, f"非法改期不应写入事件，版本不变：期望 {tmp_ver} 实际 {agg.version}"
    print(f"      非法时间: {e.code if 'e' in locals() else 'INVALID_TIME_RANGE'} 版本未破坏")


@t("20 一致性-1 事件重放得到当前日程 - 排序稳定，版本号递增")
def step_20(db, ctx):
    store = EventStoreService(db)
    agg = store.load_aggregate(ctx["bk1"])
    assert agg.booking_id == ctx["bk1"]
    assert agg.status == BookingStatus.CHECKED_IN
    assert agg.version == ctx["bk1_ver"]
    events = store.load_stream(ctx["bk1"])
    versions = [e.version for e in events]
    assert versions == sorted(versions), "版本号单调递增"
    print(f"      {ctx['bk1']} 版本序列={versions} 状态={agg.status.value}")


@t("21 一致性-2 从事件流重建日程与查询一致")
def step_21(db, ctx):
    query = QueryService(db)
    sched = query.get_schedule()
    bk1_in_sched = next((x for x in sched["items"] if x["booking_id"] == ctx["bk1"]), None)
    assert bk1_in_sched, "日程中应有 bk1"
    assert bk1_in_sched["status"] == "checked_in"
    starts = [x["start_time"] for x in sched["items"]]
    assert starts == sorted(starts), "日程按开始时间排序"


@t("22 所有事件 rule_version 一致")
def step_22(db, ctx):
    query = QueryService(db)
    evts = query.query_events(limit=1000)
    rule_versions = set()
    for item in evts["items"]:
        rule_versions.add(item["rule_version"])
    print(f"      事件总数={evts['total']}，rule_version={rule_versions}")
    assert rule_versions == {settings.rule_version}


@t("23 冲突分析与替代时间建议可正常返回")
def step_23(db, ctx):
    arb = ArbitrationService(db)
    start = ctx["bk1_start"]
    end = ctx["bk1_end"]
    analysis = arb.analyze_conflicts("room-101", start, end)
    assert analysis["has_conflict"] == True
    assert analysis["incumbent"] is not None
    assert analysis["rule_version"] == settings.rule_version
    assert "affected" in analysis
    suggestions = arb.suggest_alternative_slots("room-101", start, end, search_days=1)
    print(f"      冲突={analysis['conflict_count']} 建议={len(suggestions)}")


@t("24 回归-无冲突窗口 analyze_conflicts 返回 rule_version 不 500")
def step_24(db, ctx):
    arb = ArbitrationService(db)
    free_start = datetime.now(TZ) + timedelta(days=5)
    free_start = free_start.replace(hour=8, minute=0, second=0, microsecond=0)
    free_end = free_start + timedelta(hours=1)
    analysis = arb.analyze_conflicts("room-101", free_start, free_end)
    assert analysis["has_conflict"] == False, f"应无冲突 got {analysis}"
    assert analysis["conflict_count"] == 0
    assert analysis["recommendation"] == "ALLOW"
    assert analysis["rule_version"] == settings.rule_version, \
        f"rule_version 缺失或不一致: {analysis.get('rule_version')}"
    assert analysis["incumbent"] is None
    assert analysis["affected"] == []
    assert "window" in analysis
    print(f"      无冲突: rule_version={analysis['rule_version']} rec={analysis['recommendation']}")


def main():
    print("=" * 70)
    print(" 会议室预订系统 -- 端到端测试")
    print("=" * 70)
    db = SessionLocal()
    ctx = {}
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
        (step_20, (db, ctx)),
        (step_21, (db, ctx)),
        (step_22, (db, ctx)),
        (step_23, (db, ctx)),
        (step_24, (db, ctx)),
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
    print("Context keys:", sorted(ctx.keys()))
    db.close()
    return failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
