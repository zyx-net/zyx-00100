"""
批量导入预约草稿 - 服务层测试
运行: python test_bulk_import.py
直接调用内部服务层（无 HTTP 开销）。

覆盖场景:
1. 导入成功（JSON & CSV）
2. 部分字段错误
3. 权限隔离（普通成员替别人导入被拒）
4. 重复确认被拒
5. 冲突重检（预检通过后别人先占了，确认时失败）
6. 撤销批次
7. 重启后继续查询/确认/撤销
8. 批次内部冲突
9. 可重试错误标记
"""
from __future__ import annotations
import sys, os, json, io, csv, time, importlib
from datetime import datetime, timezone, timedelta

db_path = os.path.join(os.path.dirname(__file__), "bulk_import_test.db")
if os.path.exists(db_path):
    try:
        time.sleep(0.3)
        os.remove(db_path)
    except Exception as e:
        print(f"Warning: Could not remove existing database: {e}")

os.environ["APP_DATABASE_URL"] = f"sqlite:///{db_path}"
from app.config import settings
settings.database_url = f"sqlite:///{db_path}"
import app.db
importlib.reload(app.db)

from app.db import init_db, SessionLocal
from app.seed import seed_users
from app.services.bulk_import_service import BulkImportService
from app.services.command_handler import CommandHandler, DomainError
from app.services.commands import CreateBookingCmd
from app.domain.permissions import UserRole, BulkImportBatchStatus
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


def make_slot(days_ahead, hour, minute=0, duration_hours=1):
    start = datetime.now(TZ) + timedelta(days=days_ahead)
    start = start.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None)
    end = start + timedelta(hours=duration_hours)
    return start, end


def _make_rows(days_offsets, room="room-101", owner_id="u-admin", owner_name="Admin"):
    rows = []
    for i, d in enumerate(days_offsets):
        s, e = make_slot(d, 10 + i)
        rows.append({
            "room_id": room,
            "owner_id": owner_id,
            "owner_name": owner_name,
            "team_id": "team-a",
            "title": f"批量预约-{i}",
            "start_time": s.isoformat(),
            "end_time": e.isoformat(),
            "attendees": ["u-a", "u-b"],
            "description": f"备注{i}",
        })
    return rows


def _rows_to_csv(rows):
    buf = io.StringIO()
    if not rows:
        return ""
    fieldnames = list(rows[0].keys())
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        row = {}
        for k, v in r.items():
            if isinstance(v, list):
                row[k] = ",".join(v)
            else:
                row[k] = v
        writer.writerow(row)
    return buf.getvalue()


# ========= 测试步骤 =========

@t("00 初始化数据库与用户")
def step_00(db, ctx):
    init_db()
    seed_users(db)


@t("01 管理员用 JSON 格式导入多条预约草稿成功")
def step_01(db, ctx):
    svc = BulkImportService(db)
    rows = _make_rows([1, 2, 3], owner_id="u-admin", owner_name="系统管理员")
    batch = svc.upload_drafts(
        format="json",
        rows=rows,
        csv_content=None,
        filename="test.json",
        actor_id="u-admin",
        actor_role=UserRole.SYSTEM_ADMIN,
        actor_name="系统管理员",
    )
    assert batch["status"] == BulkImportBatchStatus.DRAFT.value, f"状态应为draft，实际{batch['status']}"
    assert batch["total_count"] == 3, f"总数应为3，实际{batch['total_count']}"
    assert len(batch["drafts"]) == 3
    ctx["batch_id_success"] = batch["batch_id"]
    ctx["success_rows"] = rows


@t("02 对上述批次执行预检 -> 全部通过")
def step_02(db, ctx):
    svc = BulkImportService(db)
    result = svc.run_precheck(
        batch_id=ctx["batch_id_success"],
        actor_id="u-admin",
        actor_role=UserRole.SYSTEM_ADMIN,
        actor_name="系统管理员",
    )
    assert result["precheck_passed"] is True, f"预检应通过：{result.get('precheck_summary')}"
    for d in result["drafts"]:
        assert d["precheck_status"] == "passed", f"draft {d['draft_index']} 状态: {d['precheck_status']} errors: {d['precheck_errors']}"


@t("03 确认提交 -> 成功生成 3 条预约")
def step_03(db, ctx):
    svc = BulkImportService(db)
    result = svc.confirm_batch(
        batch_id=ctx["batch_id_success"],
        actor_id="u-admin",
        actor_role=UserRole.SYSTEM_ADMIN,
        actor_name="系统管理员",
        note="测试确认",
    )
    assert result["success_count"] == 3, f"成功数应为3: {result}"
    assert result["failed_count"] == 0
    assert result["status"] == BulkImportBatchStatus.CONFIRMED.value
    # 检查是否都生成了 booking_id
    for r in result["results"]:
        assert r["status"] == "created"
        assert r["booking_id"].startswith("bk-")
    ctx["created_booking_ids"] = [r["booking_id"] for r in result["results"]]


@t("04 重复确认 -> 抛出 ALREADY_PROCESSED 错误")
def step_04(db, ctx):
    svc = BulkImportService(db)
    caught = None
    try:
        svc.confirm_batch(
            batch_id=ctx["batch_id_success"],
            actor_id="u-admin",
            actor_role=UserRole.SYSTEM_ADMIN,
            actor_name="系统管理员",
        )
    except DomainError as e:
        caught = e
    assert caught is not None, "应抛出错误"
    assert caught.code == "ALREADY_PROCESSED", f"错误码: {caught.code}"


@t("05 撤销预检后的批次（非已确认状态）")
def step_05(db, ctx):
    svc = BulkImportService(db)
    rows = _make_rows([4, 5], owner_id="u-admin", owner_name="系统管理员")
    batch = svc.upload_drafts(
        format="json",
        rows=rows,
        csv_content=None,
        filename=None,
        actor_id="u-admin",
        actor_role=UserRole.SYSTEM_ADMIN,
        actor_name="系统管理员",
    )
    svc.run_precheck(batch["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
    result = svc.cancel_batch(
        batch_id=batch["batch_id"],
        actor_id="u-admin",
        actor_role=UserRole.SYSTEM_ADMIN,
        actor_name="系统管理员",
        reason="测试撤销",
    )
    assert result["status"] == BulkImportBatchStatus.CANCELLED.value
    assert result["cancelled_by_name"] == "系统管理员"
    ctx["cancelled_batch_id"] = batch["batch_id"]


@t("06 撤销已确认批次 -> 应抛 INVALID_STATUS")
def step_06(db, ctx):
    svc = BulkImportService(db)
    caught = None
    try:
        svc.cancel_batch(
            batch_id=ctx["batch_id_success"],
            actor_id="u-admin",
            actor_role=UserRole.SYSTEM_ADMIN,
            actor_name="系统管理员",
            reason="尝试撤销已确认",
        )
    except DomainError as e:
        caught = e
    assert caught is not None
    assert caught.code == "INVALID_STATUS", f"错误码: {caught.code}"


@t("07 普通成员导入 - 字段完全正确但 owner_id 不是自己 -> 预检失败（权限隔离）")
def step_07(db, ctx):
    svc = BulkImportService(db)
    rows = _make_rows([6], owner_id="u-otherguy", owner_name="别人")
    batch = svc.upload_drafts(
        format="json",
        rows=rows,
        csv_content=None,
        filename=None,
        actor_id="u-zhangsan",
        actor_role=UserRole.MEMBER,
        actor_name="张三",
    )
    result = svc.run_precheck(
        batch_id=batch["batch_id"],
        actor_id="u-zhangsan",
        actor_role=UserRole.MEMBER,
        actor_name="张三",
    )
    assert result["precheck_passed"] is False
    errors = result["drafts"][0]["precheck_errors"]
    codes = [e["code"] for e in errors]
    assert "PERMISSION_DENIED" in codes, f"应有权限错误: {codes}"


@t("08 普通成员导入 - owner_id 是自己 -> 预检通过，管理员确认成功")
def step_08(db, ctx):
    svc = BulkImportService(db)
    rows = _make_rows([7], owner_id="u-lisi", owner_name="李四")
    batch = svc.upload_drafts(
        format="json",
        rows=rows,
        csv_content=None,
        filename=None,
        actor_id="u-lisi",
        actor_role=UserRole.MEMBER,
        actor_name="李四",
    )
    precheck = svc.run_precheck(batch["batch_id"], "u-lisi", UserRole.MEMBER, "李四")
    assert precheck["precheck_passed"] is True, f"预检失败: {precheck['drafts'][0]['precheck_errors']}"
    # 普通成员无确认权限
    caught = None
    try:
        svc.confirm_batch(batch["batch_id"], "u-lisi", UserRole.MEMBER, "李四")
    except DomainError as e:
        caught = e
    assert caught is not None and caught.code == "PERMISSION_DENIED"
    # 管理员可以确认
    confirm = svc.confirm_batch(
        batch["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员"
    )
    assert confirm["success_count"] == 1, f"失败: {confirm}"


@t("09 批量导入 - 部分字段错误（缺房间ID、时间格式错、房间不存在）")
def step_09(db, ctx):
    svc = BulkImportService(db)
    s1, e1 = make_slot(8, 9)
    s2, _ = make_slot(8, 10)
    rows = [
        {
            "room_id": "",
            "owner_id": "u-admin",
            "owner_name": "管理员",
            "team_id": "team-a",
            "title": "缺房间",
            "start_time": s1.isoformat(),
            "end_time": e1.isoformat(),
            "attendees": [],
        },
        {
            "room_id": "room-101",
            "owner_id": "u-admin",
            "owner_name": "管理员",
            "team_id": "team-a",
            "title": "时间格式错",
            "start_time": "not-a-date",
            "end_time": "also-bad",
            "attendees": [],
        },
        {
            "room_id": "room-999",
            "owner_id": "u-admin",
            "owner_name": "管理员",
            "team_id": "team-a",
            "title": "房间不存在",
            "start_time": s2.isoformat(),
            "end_time": (s2 + timedelta(hours=1)).isoformat(),
            "attendees": [],
        },
    ]
    batch = svc.upload_drafts(
        format="json", rows=rows, csv_content=None, filename=None,
        actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="管理员",
    )
    precheck = svc.run_precheck(batch["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, "管理员")
    assert precheck["precheck_passed"] is False
    drafts = precheck["drafts"]
    assert drafts[0]["precheck_status"] == "error"
    codes0 = [e["code"] for e in drafts[0]["precheck_errors"]]
    assert "REQUIRED" in codes0, codes0
    assert drafts[1]["precheck_status"] == "error"
    codes1 = [e["code"] for e in drafts[1]["precheck_errors"]]
    assert "INVALID_TIME_FORMAT" in codes1, codes1
    assert drafts[2]["precheck_status"] == "error"
    codes2 = [e["code"] for e in drafts[2]["precheck_errors"]]
    assert "ROOM_NOT_FOUND" in codes2, codes2
    ctx["partial_err_batch"] = batch["batch_id"]


@t("10 预检未通过 -> 确认被拒（PRECHECK_REQUIRED）")
def step_10(db, ctx):
    svc = BulkImportService(db)
    caught = None
    try:
        svc.confirm_batch(ctx["partial_err_batch"], "u-admin", UserRole.SYSTEM_ADMIN, "管理员")
    except DomainError as e:
        caught = e
    assert caught is not None, "确认应被拒绝"
    assert caught.code == "PRECHECK_REQUIRED", f"错误码应为PRECHECK_REQUIRED，实际{caught.code}"


@t("11 冲突重检 - 预检通过后别人先占了，确认时失败并标记可重试")
def step_11(db, ctx):
    svc = BulkImportService(db)
    handler = CommandHandler(db)
    s, e = make_slot(10, 14)
    rows = [{
        "room_id": "room-101",
        "owner_id": "u-admin",
        "owner_name": "管理员",
        "team_id": "team-a",
        "title": "冲突测试",
        "start_time": s.isoformat(),
        "end_time": e.isoformat(),
        "attendees": [],
    }]
    batch = svc.upload_drafts(
        format="json", rows=rows, csv_content=None, filename=None,
        actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="管理员",
    )
    precheck = svc.run_precheck(batch["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, "管理员")
    assert precheck["precheck_passed"] is True
    # 在预检和确认之间，先手动创建一个占坑的
    cmd = CreateBookingCmd(
        room_id="room-101",
        owner_id="u-other",
        owner_name="别人抢先",
        team_id="team-b",
        title="抢先预约",
        start_time=s,
        end_time=e,
        attendees=[],
    )
    handler.create_booking(cmd, "u-wangwu", UserRole.MEMBER, "王五")
    # 现在确认 -> 内部会重新预检
    confirm = svc.confirm_batch(
        batch["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, "管理员"
    )
    # 应该失败，且标记为 retryable
    assert confirm["failed_count"] >= 1
    assert confirm["retryable_count"] >= 1, f"应有可重试项: {confirm}"


@t("12 批次内部冲突（两条同一房间同一时间）")
def step_12(db, ctx):
    svc = BulkImportService(db)
    s, e = make_slot(11, 15)
    rows = [
        {
            "room_id": "room-101", "owner_id": "u-admin", "owner_name": "管理员",
            "team_id": "team-a", "title": "内部冲突A",
            "start_time": s.isoformat(), "end_time": e.isoformat(), "attendees": [],
        },
        {
            "room_id": "room-101", "owner_id": "u-admin", "owner_name": "管理员",
            "team_id": "team-a", "title": "内部冲突B",
            "start_time": s.isoformat(), "end_time": e.isoformat(), "attendees": [],
        },
    ]
    batch = svc.upload_drafts(
        format="json", rows=rows, csv_content=None, filename=None,
        actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="管理员",
    )
    precheck = svc.run_precheck(batch["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, "管理员")
    # 第二条应报 INTERNAL_CONFLICT
    assert precheck["precheck_passed"] is False
    d2 = precheck["drafts"][1]
    assert d2["precheck_status"] == "error"
    codes = [e["code"] for e in d2["precheck_errors"]]
    assert "INTERNAL_CONFLICT" in codes, f"缺少内部冲突: {codes}"


@t("13 CSV 格式导入（含中文列名别名）")
def step_13(db, ctx):
    svc = BulkImportService(db)
    s1, e1 = make_slot(12, 10)
    s2, e2 = make_slot(12, 13)
    rows = [
        {
            "会议室ID": "room-101",
            "申请人ID": "u-admin",
            "申请人姓名": "管理员",
            "团队ID": "team-a",
            "会议标题": "CSV测试1",
            "开始时间": s1.isoformat(),
            "结束时间": e1.isoformat(),
            "参会人": "u-x,u-y",
            "备注": "来自CSV",
        },
        {
            "会议室ID": "room-102",
            "申请人ID": "u-admin",
            "申请人姓名": "管理员",
            "团队ID": "team-a",
            "会议标题": "CSV测试2",
            "开始时间": s2.isoformat(),
            "结束时间": e2.isoformat(),
            "参会人": "u-z",
            "备注": "",
        },
    ]
    csv_content = _rows_to_csv(rows)
    batch = svc.upload_drafts(
        format="csv", rows=[], csv_content=csv_content, filename="test.csv",
        actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="管理员",
    )
    assert batch["source_format"] == "csv"
    assert batch["total_count"] == 2
    assert batch["drafts"][0]["room_id"] == "room-101"
    assert batch["drafts"][0]["attendees"] == ["u-x", "u-y"]
    precheck = svc.run_precheck(batch["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, "管理员")
    assert precheck["precheck_passed"] is True, f"预检失败: {[d['precheck_errors'] for d in precheck['drafts']]}"
    confirm = svc.confirm_batch(batch["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, "管理员")
    assert confirm["success_count"] == 2, f"确认失败: {confirm}"


@t("14 权限：成员查看列表只能看到自己的；管理员可以看到全部")
def step_14(db, ctx):
    svc = BulkImportService(db)
    member_list = svc.list_batches("u-zhangsan", UserRole.MEMBER, None, limit=100)
    for item in member_list["items"]:
        assert item["submitter_id"] == "u-zhangsan", f"成员不应看到他人批次: {item}"
    admin_list = svc.list_batches("u-admin", UserRole.SYSTEM_ADMIN, None, limit=100)
    assert len(admin_list["items"]) > len(member_list["items"]), "管理员应看到更多批次"


@t("15 重启后持久性：重新实例化db连接 -> 数据仍在，可查询/确认/撤销")
def step_15(db, ctx):
    # 先创建一个新批次（预检通过但未确认）和一个草稿状态的批次
    svc = BulkImportService(db)
    # 注意 booking_window_days: room-101 是14天，room-102 是14天，room-201 是30天
    # 选 day 9 和 10，在 14 天窗口内，且不与之前 step 冲突
    rows1 = _make_rows([9, 10], room="room-201", owner_id="u-admin", owner_name="管理员")
    b_prechecked = svc.upload_drafts(
        format="json", rows=rows1, csv_content=None, filename=None,
        actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="管理员",
    )
    svc.run_precheck(b_prechecked["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, "管理员")

    rows2 = _make_rows([11], room="room-201", owner_id="u-admin", owner_name="管理员")
    b_draft = svc.upload_drafts(
        format="json", rows=rows2, csv_content=None, filename=None,
        actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="管理员",
    )

    ctx["persist_prechecked_id"] = b_prechecked["batch_id"]
    ctx["persist_draft_id"] = b_draft["batch_id"]

    # 关闭当前session，模拟重启
    db.close()
    # 新建一个session
    new_db = SessionLocal()
    try:
        new_svc = BulkImportService(new_db)
        # 查询 persisted prechecked
        fetched = new_svc.get_batch(
            b_prechecked["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, include_drafts=True
        )
        assert fetched["status"] == BulkImportBatchStatus.PRECHECKED.value, (
            f"状态不对: {fetched['status']} precheck_passed={fetched['precheck_passed']} "
            f"errors={[d['precheck_errors'] for d in fetched.get('drafts', [])]}"
        )
        assert fetched["precheck_passed"] is True
        # 确认提交（重启后继续）
        confirm = new_svc.confirm_batch(
            b_prechecked["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, "管理员"
        )
        assert confirm["success_count"] == 2, f"确认失败: {confirm}"

        # 撤销 draft
        cancelled = new_svc.cancel_batch(
            b_draft["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN, "管理员", reason="重启后撤销"
        )
        assert cancelled["status"] == BulkImportBatchStatus.CANCELLED.value

        # 查询操作日志
        logs = new_svc.list_operation_logs(
            b_prechecked["batch_id"], "u-admin", UserRole.SYSTEM_ADMIN
        )
        ops = [l["operation"] for l in logs["items"]]
        assert "UPLOAD" in ops, f"日志应有UPLOAD: {ops}"
        assert "PRECHECK" in ops, f"日志应有PRECHECK: {ops}"
        assert "CONFIRM" in ops, f"日志应有CONFIRM: {ops}"
    finally:
        new_db.close()
    # 重新打开db给后续步骤用
    ctx["db"] = SessionLocal()


# ========= 运行 =========
def main():
    print("=" * 70)
    print("Bulk Import Service Layer Tests")
    print("=" * 70)

    db = SessionLocal()
    ctx = {"db": db}

    all_tests = [
        step_00, step_01, step_02, step_03, step_04, step_05, step_06,
        step_07, step_08, step_09, step_10, step_11, step_12, step_13,
        step_14, step_15,
    ]
    results = []
    for fn in all_tests:
        # step_15 可能关闭了db，这里用 ctx 中的最新 db
        cur_db = ctx.get("db", db)
        ok = fn(cur_db, ctx)
        results.append((fn.__name__, ok))

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    print("=" * 70)
    print(f"RESULT: {passed}/{total} passed")
    if passed != total:
        print("Failed tests:")
        for name, ok in results:
            if not ok:
                print(f"  - {name}")
    print("=" * 70)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
