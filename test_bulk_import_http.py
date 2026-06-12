"""
批量导入预约草稿 - HTTP API 端到端流程测试
运行前先启动服务: uvicorn app.main:app --host 0.0.0.0 --port 8002
运行: python test_bulk_import_http.py

覆盖用户可见的完整流程:
1. Admin 用 JSON 上传草稿 -> 预检 -> 确认
2. Admin 用 CSV 格式上传（含中文别名表头）
3. Member 替他人导入 -> 预检失败（权限隔离）
4. Member 替自己导入 -> 预检通过，但 member 无 confirm 权限(403)，admin 可以确认
5. 字段错误批次 -> 预检失败，确认被拒 PRECHECK_REQUIRED
6. 批次内部冲突（同房间同时间）-> 预检失败 INTERNAL_CONFLICT
7. 预检后他人抢占时段 -> 确认失败并标记 retryable
8. 查看批次详情、列表、操作日志
9. 撤销未确认的批次
10. 重复确认被拒绝
11. 不存在的批次 -> 404
"""
import requests
import json
import csv
import io
import sys
from datetime import datetime, timezone, timedelta

BASE_URL = "http://localhost:8002/api/v1"
TZ = timezone(timedelta(hours=8))

PASS = "PASS"
FAIL = "FAIL"
results = []


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg} expected={expected} actual={actual}")


def assert_true(cond, msg=""):
    if not cond:
        raise AssertionError(f"{msg}")


def t(desc):
    def wrap(fn):
        def inner(*args, **kwargs):
            try:
                fn(*args, **kwargs)
                print(f"[{PASS}] {desc}")
                results.append((desc, True))
                return True
            except AssertionError as e:
                print(f"[{FAIL}] {desc} -> {e}")
                results.append((desc, False))
                return False
            except Exception as e:
                print(f"[{FAIL}] {desc} -> EXC: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                results.append((desc, False))
                return False
        return inner
    return wrap


def hdrs(actor_id, actor_role, actor_name):
    return {
        "X-Actor-Id": actor_id,
        "X-Actor-Role": actor_role,
        "X-Actor-Name": actor_name,
        "Content-Type": "application/json",
    }


def make_slot(days_ahead, hour, minute=0, duration_hours=1):
    start = datetime.now(TZ) + timedelta(days=days_ahead)
    start = start.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None)
    end = start + timedelta(hours=duration_hours)
    return start, end


def make_rows(days_offsets, room="room-201", owner_id="u-admin", owner_name="Admin User"):
    """Use room-201 with 30-day booking window for more headroom"""
    rows = []
    for i, d in enumerate(days_offsets):
        s, e = make_slot(d, 9 + i)
        rows.append({
            "room_id": room,
            "owner_id": owner_id,
            "owner_name": owner_name,
            "team_id": "team-a",
            "title": f"BulkHTTP-{i}",
            "start_time": s.isoformat(),
            "end_time": e.isoformat(),
            "attendees": ["u-a", "u-b"],
            "description": f"from HTTP test {i}",
        })
    return rows


def rows_to_csv(rows):
    """Generate CSV with Chinese column aliases (still valid via normalize_row)"""
    buf = io.StringIO()
    if not rows:
        return ""
    field_map = {
        "room_id": "会议室ID",
        "owner_id": "申请人ID",
        "owner_name": "申请人姓名",
        "team_id": "团队ID",
        "title": "会议标题",
        "start_time": "开始时间",
        "end_time": "结束时间",
        "attendees": "参会人",
        "description": "备注",
    }
    writer = csv.DictWriter(buf, fieldnames=list(field_map.values()))
    writer.writeheader()
    for r in rows:
        mapped = {}
        for k, v in field_map.items():
            val = r.get(k)
            if isinstance(val, list):
                val = ",".join(val)
            mapped[v] = val
        writer.writerow(mapped)
    return buf.getvalue()


ctx = {}


# ============ Test Steps ============

@t("01 Health check - server running")
def step_01():
    r = requests.get(f"{BASE_URL}/health")
    assert_eq(r.status_code, 200)
    assert_true("rule_version" in r.json())


@t("02 Admin uploads 3 drafts via JSON")
def step_02():
    rows = make_rows([10, 11, 12])
    r = requests.post(f"{BASE_URL}/bulk-import/upload",
                      headers=hdrs("u-admin", "system_admin", "Admin User"),
                      json={"format": "json", "filename": "http_test.json", "rows": rows})
    assert_eq(r.status_code, 200, f"body={r.text}")
    data = r.json()
    assert_eq(data["status"], "draft")
    assert_eq(data["total_count"], 3)
    assert_eq(len(data["drafts"]), 3)
    ctx["batch_json"] = data["batch_id"]


@t("03 Run precheck on JSON batch -> all pass")
def step_03():
    r = requests.post(f"{BASE_URL}/bulk-import/{ctx['batch_json']}/precheck",
                      headers=hdrs("u-admin", "system_admin", "Admin User"))
    assert_eq(r.status_code, 200, f"body={r.text}")
    data = r.json()
    assert_true(data["precheck_passed"], f"precheck failed: {data['drafts']}")
    assert_eq(data["error_count"], 0)
    assert_eq(data["passed_count"], 3)
    for d in data["drafts"]:
        assert_eq(d["precheck_status"], "passed")


@t("04 Confirm batch -> 3 bookings created successfully")
def step_04():
    r = requests.post(f"{BASE_URL}/bulk-import/{ctx['batch_json']}/confirm",
                      headers=hdrs("u-admin", "system_admin", "Admin User"),
                      json={"note": "HTTP test confirm"})
    assert_eq(r.status_code, 200, f"body={r.text}")
    data = r.json()
    assert_eq(data["success_count"], 3, f"detail: {data}")
    assert_eq(data["failed_count"], 0)
    assert_eq(data["status"], "confirmed")
    ids = [x["booking_id"] for x in data["results"] if x.get("booking_id")]
    assert_eq(len(ids), 3)
    ctx["created_ids"] = ids


@t("05 Get batch detail -> status confirmed")
def step_05():
    r = requests.get(f"{BASE_URL}/bulk-import/{ctx['batch_json']}",
                     headers=hdrs("u-admin", "system_admin", "Admin User"))
    assert_eq(r.status_code, 200)
    data = r.json()
    assert_eq(data["status"], "confirmed")
    assert_eq(data["success_count"], 3)
    created = [d for d in data["drafts"] if d["result_status"] == "created"]
    assert_eq(len(created), 3)


@t("06 Duplicate confirm -> 400 ALREADY_PROCESSED")
def step_06():
    r = requests.post(f"{BASE_URL}/bulk-import/{ctx['batch_json']}/confirm",
                      headers=hdrs("u-admin", "system_admin", "Admin User"), json={})
    assert_eq(r.status_code, 400, f"actual {r.status_code}: {r.text}")
    assert_eq(r.json()["error"]["code"], "ALREADY_PROCESSED")


@t("07 List operation logs -> UPLOAD/PRECHECK/CONFIRM present")
def step_07():
    r = requests.get(f"{BASE_URL}/bulk-import/{ctx['batch_json']}/logs",
                     headers=hdrs("u-admin", "system_admin", "Admin User"))
    assert_eq(r.status_code, 200)
    ops = [x["operation"] for x in r.json()["items"]]
    assert_true("UPLOAD" in ops, f"ops: {ops}")
    assert_true("PRECHECK" in ops, f"ops: {ops}")
    assert_true("CONFIRM" in ops, f"ops: {ops}")


@t("08 Admin upload CSV with Chinese column aliases -> confirm OK")
def step_08():
    rows = make_rows([13, 14], owner_id="u-admin", owner_name="Admin User")
    csv_str = rows_to_csv(rows)
    r = requests.post(f"{BASE_URL}/bulk-import/upload",
                      headers=hdrs("u-admin", "system_admin", "Admin User"),
                      json={"format": "csv", "filename": "http_test.csv", "csv_content": csv_str})
    assert_eq(r.status_code, 200, f"body={r.text}")
    batch_id = r.json()["batch_id"]
    assert_eq(r.json()["source_format"], "csv")
    assert_eq(r.json()["total_count"], 2)
    # precheck
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/precheck",
                      headers=hdrs("u-admin", "system_admin", "Admin User"))
    assert_eq(r.status_code, 200)
    assert_true(r.json()["precheck_passed"],
                f"precheck errors: {[d['precheck_errors'] for d in r.json()['drafts']]}")
    # confirm
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/confirm",
                      headers=hdrs("u-admin", "system_admin", "Admin User"), json={})
    assert_eq(r.status_code, 200, f"body={r.text}")
    assert_eq(r.json()["success_count"], 2, f"detail: {r.json()}")


@t("09 Member imports for another owner -> PERMISSION_DENIED in precheck")
def step_09():
    rows = make_rows([15], owner_id="u-someoneelse", owner_name="Someone Else")
    r = requests.post(f"{BASE_URL}/bulk-import/upload",
                      headers=hdrs("u-zhangsan", "member", "Zhang San"),
                      json={"format": "json", "rows": rows})
    assert_eq(r.status_code, 200)
    batch_id = r.json()["batch_id"]
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/precheck",
                      headers=hdrs("u-zhangsan", "member", "Zhang San"))
    assert_eq(r.status_code, 200)
    data = r.json()
    assert_true(not data["precheck_passed"])
    codes = [e["code"] for e in data["drafts"][0]["precheck_errors"]]
    assert_true("PERMISSION_DENIED" in codes, f"codes: {codes}")


@t("10 Member imports for self -> precheck passes, but member confirm gets 403, admin confirm OK")
def step_10():
    rows = make_rows([16], owner_id="u-lisi", owner_name="Li Si")
    r = requests.post(f"{BASE_URL}/bulk-import/upload",
                      headers=hdrs("u-lisi", "member", "Li Si"),
                      json={"format": "json", "rows": rows})
    batch_id = r.json()["batch_id"]
    # precheck
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/precheck",
                      headers=hdrs("u-lisi", "member", "Li Si"))
    assert_eq(r.status_code, 200)
    assert_true(r.json()["precheck_passed"],
                f"precheck failed: {[d['precheck_errors'] for d in r.json()['drafts']]}")
    # member cannot confirm
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/confirm",
                      headers=hdrs("u-lisi", "member", "Li Si"), json={})
    assert_eq(r.status_code, 403, f"actual {r.status_code}: {r.text}")
    assert_eq(r.json()["error"]["code"], "PERMISSION_DENIED")
    # admin can confirm
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/confirm",
                      headers=hdrs("u-admin", "system_admin", "Admin User"), json={})
    assert_eq(r.status_code, 200, f"body={r.text}")
    assert_eq(r.json()["success_count"], 1)


@t("11 Bad-field batch -> precheck fails, confirm rejected PRECHECK_REQUIRED")
def step_11():
    s1, e1 = make_slot(17, 9)
    rows = [
        {"room_id": "", "owner_id": "u-admin", "owner_name": "Admin", "team_id": "t",
         "title": "No room", "start_time": s1.isoformat(), "end_time": e1.isoformat(), "attendees": []},
        {"room_id": "room-201", "owner_id": "u-admin", "owner_name": "Admin", "team_id": "t",
         "title": "Bad time", "start_time": "not-a-date", "end_time": "bad-time", "attendees": []},
    ]
    r = requests.post(f"{BASE_URL}/bulk-import/upload",
                      headers=hdrs("u-admin", "system_admin", "Admin User"),
                      json={"format": "json", "rows": rows})
    batch_id = r.json()["batch_id"]
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/precheck",
                      headers=hdrs("u-admin", "system_admin", "Admin User"))
    assert_eq(r.status_code, 200)
    data = r.json()
    assert_true(not data["precheck_passed"])
    assert_eq(data["error_count"], 2)
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/confirm",
                      headers=hdrs("u-admin", "system_admin", "Admin User"), json={})
    assert_eq(r.status_code, 400, f"actual {r.status_code}: {r.text}")
    assert_eq(r.json()["error"]["code"], "PRECHECK_REQUIRED")


@t("12 Intra-batch conflict (same room, same time) -> INTERNAL_CONFLICT")
def step_12():
    s, e = make_slot(18, 9)
    rows = [
        {"room_id": "room-201", "owner_id": "u-admin", "owner_name": "Admin", "team_id": "t",
         "title": "Conflict A", "start_time": s.isoformat(), "end_time": e.isoformat(), "attendees": []},
        {"room_id": "room-201", "owner_id": "u-admin", "owner_name": "Admin", "team_id": "t",
         "title": "Conflict B", "start_time": s.isoformat(), "end_time": e.isoformat(), "attendees": []},
    ]
    r = requests.post(f"{BASE_URL}/bulk-import/upload",
                      headers=hdrs("u-admin", "system_admin", "Admin User"),
                      json={"format": "json", "rows": rows})
    batch_id = r.json()["batch_id"]
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/precheck",
                      headers=hdrs("u-admin", "system_admin", "Admin User"))
    assert_eq(r.status_code, 200)
    d2 = r.json()["drafts"][1]
    codes = [e["code"] for e in d2["precheck_errors"]]
    assert_true("INTERNAL_CONFLICT" in codes, f"codes: {codes}")


@t("13 Race: after precheck, another booking takes slot -> confirm fails with retryable_count >= 1")
def step_13():
    s, e = make_slot(19, 14)
    rows = [{"room_id": "room-201", "owner_id": "u-admin", "owner_name": "Admin", "team_id": "t",
             "title": "Race test", "start_time": s.isoformat(), "end_time": e.isoformat(), "attendees": []}]
    r = requests.post(f"{BASE_URL}/bulk-import/upload",
                      headers=hdrs("u-admin", "system_admin", "Admin User"),
                      json={"format": "json", "rows": rows})
    batch_id = r.json()["batch_id"]
    # precheck passes
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/precheck",
                      headers=hdrs("u-admin", "system_admin", "Admin User"))
    assert_eq(r.status_code, 200)
    assert_true(r.json()["precheck_passed"], "precheck should pass")
    # another user takes the slot
    r = requests.post(f"{BASE_URL}/bookings",
                      headers=hdrs("u-wangwu", "member", "Wang Wu"),
                      json={"room_id": "room-201", "owner_id": "u-wangwu", "owner_name": "Wang Wu",
                            "team_id": "team-b", "title": "Sneaky booking",
                            "start_time": s.isoformat(), "end_time": e.isoformat(), "attendees": []})
    assert_eq(r.status_code, 200, f"sneaky failed: {r.status_code} {r.text}")
    ctx["sneaky_booking_id"] = r.json()["booking"]["booking_id"]
    # now confirm -> should fail with retryable
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/confirm",
                      headers=hdrs("u-admin", "system_admin", "Admin User"), json={})
    assert_eq(r.status_code, 200, f"body={r.text}")
    data = r.json()
    assert_eq(data["failed_count"], 1, f"failed_count: {data}")
    assert_true(data["retryable_count"] >= 1, f"retryable expected: {data}")


@t("14 Cancel a prechecked (not confirmed) batch")
def step_14():
    rows = make_rows([20, 21])
    r = requests.post(f"{BASE_URL}/bulk-import/upload",
                      headers=hdrs("u-admin", "system_admin", "Admin User"),
                      json={"format": "json", "rows": rows})
    batch_id = r.json()["batch_id"]
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/precheck",
                      headers=hdrs("u-admin", "system_admin", "Admin User"))
    assert_eq(r.status_code, 200)
    # cancel
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/cancel",
                      headers=hdrs("u-admin", "system_admin", "Admin User"),
                      json={"reason": "HTTP test cancel"})
    assert_eq(r.status_code, 200, f"body={r.text}")
    assert_eq(r.json()["status"], "cancelled")
    assert_eq(r.json()["cancelled_by_name"], "Admin User")
    # re-cancel -> 400
    r = requests.post(f"{BASE_URL}/bulk-import/{batch_id}/cancel",
                      headers=hdrs("u-admin", "system_admin", "Admin User"), json={})
    assert_eq(r.status_code, 400)
    assert_eq(r.json()["error"]["code"], "INVALID_STATUS")


@t("15 List batches - admin sees many, member sees only own")
def step_15():
    r = requests.get(f"{BASE_URL}/bulk-import",
                     headers=hdrs("u-admin", "system_admin", "Admin User"))
    admin_items = r.json()["items"]
    assert_true(len(admin_items) >= 8, f"admin sees only {len(admin_items)} batches")
    r = requests.get(f"{BASE_URL}/bulk-import",
                     headers=hdrs("u-zhangsan", "member", "Zhang San"))
    for it in r.json()["items"]:
        assert_eq(it["submitter_id"], "u-zhangsan", "member sees other's batch!")


@t("16 Non-existent batch -> 404 BATCH_NOT_FOUND")
def step_16():
    r = requests.get(f"{BASE_URL}/bulk-import/batch-nonexistent-123",
                     headers=hdrs("u-admin", "system_admin", "Admin User"))
    assert_eq(r.status_code, 404)
    assert_eq(r.json()["error"]["code"], "BATCH_NOT_FOUND")


# ============ main ============

def main():
    print("=" * 70)
    print("Bulk Import HTTP API End-to-End Test")
    print("=" * 70)

    steps = [
        step_01, step_02, step_03, step_04, step_05, step_06, step_07,
        step_08, step_09, step_10, step_11, step_12, step_13, step_14,
        step_15, step_16,
    ]
    for fn in steps:
        fn()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    print("=" * 70)
    print(f"RESULT: {passed}/{total} passed")
    if passed != total:
        print("Failed tests:")
        for desc, ok in results:
            if not ok:
                print(f"  - {desc}")
    print("=" * 70)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
