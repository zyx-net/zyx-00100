"""
API 链路测试脚本
测试用户可见的改期审批完整流程
"""
import requests
import json
import sys
import random
from datetime import datetime, timezone, timedelta

BASE_URL = "http://localhost:8001"
TZ = timezone(timedelta(hours=8))


def print_separator(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def pprint_response(title, response):
    print(f"\n--- {title} ---")
    print(f"Status: {response.status_code}")
    try:
        data = response.json()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return data
    except:
        print(f"Body: {response.text[:500]}")
        return None


def assert_success(data, message="API call failed"):
    if not data.get("success", True):
        print(f"ERROR: {message}")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        sys.exit(1)


def main():
    print_separator("会议室预订系统 - 改期审批 API 链路测试")

    # 1. 初始化：健康检查
    print_separator("1. 健康检查")
    r = requests.get(f"{BASE_URL}/api/v1/health")
    data = pprint_response("健康检查", r)
    assert r.status_code == 200
    assert data["status"] == "ok"
    print(f"✓ 服务正常，规则版本: {data['rule_version']}")

    # 2. 获取房间列表
    print_separator("2. 获取房间列表")
    r = requests.get(f"{BASE_URL}/api/v1/rooms")
    data = pprint_response("房间列表", r)
    assert r.status_code == 200
    print(f"✓ 共 {len(data['items'])} 个房间")

    # 3. 成员张三创建预订
    print_separator("3. 成员张三创建预订")
    # 使用动态时间避免冲突（在14天预订窗口内）
    day_offset = 5 + random.randint(0, 3)
    start_time = (datetime.now(TZ) + timedelta(days=day_offset)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    end_time = start_time + timedelta(hours=1)

    booking_data = {
        "room_id": "room-102",
        "owner_id": "u-zhangsan",
        "owner_name": "张三",
        "team_id": "team-a",
        "title": "API测试会议",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "attendees": ["u-lisi", "u-wangwu"],
    }

    headers = {
        "X-Actor-Id": "u-zhangsan",
        "X-Actor-Role": "member",
        "X-Actor-Name": "Zhang San",
        "Content-Type": "application/json",
    }

    r = requests.post(
        f"{BASE_URL}/api/v1/bookings",
        headers=headers,
        data=json.dumps(booking_data),
    )
    data = pprint_response("创建预订", r)
    assert r.status_code == 200
    assert_success(data, "创建预订失败")
    booking_id = data["booking"]["booking_id"]
    booking_version = data["booking"]["version"]
    print(f"✓ 预订创建成功，ID: {booking_id}")
    print(f"  原时段: {start_time.strftime('%Y-%m-%d %H:%M')} - {end_time.strftime('%H:%M')}")

    # 4. 张三提交改期请求
    print_separator("4. 张三提交改期请求（普通成员）")
    new_start = start_time + timedelta(hours=2)
    new_end = new_start + timedelta(hours=1)

    reschedule_data = {
        "booking_id": booking_id,
        "rescheduler_id": "u-zhangsan",
        "rescheduler_name": "Zhang San",
        "new_start_time": new_start.isoformat(),
        "new_end_time": new_end.isoformat(),
        "reason": "时间调整",
        "expected_version": booking_version,
    }

    r = requests.post(
        f"{BASE_URL}/api/v1/bookings/{booking_id}/reschedule",
        headers=headers,
        data=json.dumps(reschedule_data),
    )
    data = pprint_response("提交改期请求", r)
    assert r.status_code == 200
    assert_success(data, "提交改期请求失败")
    assert data.get("requires_approval") == True, "普通成员改期应该需要审批"
    assert data["request"]["status"] == "pending", f"状态应为 pending，实际: {data['request']['status']}"
    request_id = data["request"]["request_id"]
    booking_version = data["booking"]["version"]
    print(f"✓ 改期请求已提交，ID: {request_id}")
    print(f"  新时段: {new_start.strftime('%Y-%m-%d %H:%M')} - {new_end.strftime('%H:%M')}")
    print(f"  状态: 需要审批")

    # 5. 张三尝试审批自己的请求（应该被拒绝）
    print_separator("5. 张三尝试审批自己的请求（权限校验）")
    approve_data = {
        "request_id": request_id,
        "approver_id": "u-zhangsan",
        "approver_name": "Zhang San",
        "reason": "自己审批",
        "expected_version": booking_version,
    }
    r = requests.post(
        f"{BASE_URL}/api/v1/reschedule-requests/{request_id}/approve",
        headers=headers,
        data=json.dumps(approve_data),
    )
    data = pprint_response("张三审批请求", r)
    assert r.status_code == 400, f"普通成员审批应返回 400，实际: {r.status_code}"
    assert data["error"]["code"] == "PERMISSION_DENIED", f"错误码应为 PERMISSION_DENIED，实际: {data['error']['code']}"
    print(f"✓ 普通成员无审批权限，正确拒绝 (HTTP {r.status_code}, code: {data['error']['code']})")

    # 6. 前台小李审批改期请求
    print_separator("6. 前台小李审批改期请求")
    admin_headers = {
        "X-Actor-Id": "u-recep",
        "X-Actor-Role": "receptionist",
        "X-Actor-Name": "Reception Li",
        "Content-Type": "application/json",
    }

    approve_data = {
        "request_id": request_id,
        "approver_id": "u-recep",
        "approver_name": "Reception Li",
        "reason": "批准改期",
        "expected_version": booking_version,
    }
    r = requests.post(
        f"{BASE_URL}/api/v1/reschedule-requests/{request_id}/approve",
        headers=admin_headers,
        data=json.dumps(approve_data),
    )
    data = pprint_response("前台审批改期", r)
    assert r.status_code == 200
    assert_success(data, "审批失败")
    assert data["request"]["status"] == "approved", f"状态应为 approved，实际: {data['request']['status']}"
    assert data["request"]["approver_id"] == "u-recep", f"审批人应为 u-recep"
    print(f"✓ 改期已批准，审批人: {data['request']['approver_name']}")
    print(f"  新时段已生效: {data['booking']['start_time']} - {data['booking']['end_time']}")

    # 7. 查询改期请求详情
    print_separator("7. 查询改期请求详情")
    r = requests.get(
        f"{BASE_URL}/api/v1/reschedule-requests/{request_id}",
        headers=admin_headers,
    )
    data = pprint_response("请求详情", r)
    assert r.status_code == 200
    assert_success(data, "查询请求详情失败")
    assert data["request"]["status"] == "approved"
    print(f"✓ 请求状态: {data['request']['status']}")
    print(f"  提交人: {data['request']['requester_name']}")
    print(f"  审批人: {data['request']['approver_name']}")
    print(f"  审批原因: {data['request']['approve_reason']}")

    # 8. 查询预订的待审批请求
    print_separator("8. 查询预订的待审批请求")
    r = requests.get(
        f"{BASE_URL}/api/v1/bookings/{booking_id}/reschedule-requests/pending",
        headers=headers,
    )
    data = pprint_response("预订待审批请求", r)
    assert r.status_code == 200
    assert len(data["items"]) == 0, f"应该没有待审批请求，实际: {len(data['items'])}"
    print(f"✓ 该预订无待审批请求（已处理完毕）")

    # 9. 列出所有改期请求
    print_separator("9. 列出所有改期请求")
    r = requests.get(
        f"{BASE_URL}/api/v1/reschedule-requests",
        headers=admin_headers,
    )
    data = pprint_response("所有改期请求", r)
    assert r.status_code == 200
    print(f"✓ 共 {data['total']} 条审批记录")
    for req in data["items"]:
        print(f"  - {req['request_id']}: {req['status']} "
              f"({req['requester_name']} → {req.get('approver_name', '未审批')})")

    # 10. 测试拒绝流程
    print_separator("10. 测试拒绝流程")
    start_time2 = (datetime.now(TZ) + timedelta(days=day_offset + 1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    end_time2 = start_time2 + timedelta(hours=1)

    booking_data2 = {
        "room_id": "room-101",
        "owner_id": "u-lisi",
        "owner_name": "李四",
        "team_id": "team-a",
        "title": "API测试-需求评审",
        "start_time": start_time2.isoformat(),
        "end_time": end_time2.isoformat(),
    }

    headers2 = {
        "X-Actor-Id": "u-lisi",
        "X-Actor-Role": "member",
        "X-Actor-Name": "Li Si",
        "Content-Type": "application/json",
    }

    r = requests.post(
        f"{BASE_URL}/api/v1/bookings",
        headers=headers2,
        data=json.dumps(booking_data2),
    )
    data = pprint_response("创建第二个预订", r)
    assert_success(data, "创建第二个预订失败")
    booking_id2 = data["booking"]["booking_id"]
    booking_version2 = data["booking"]["version"]

    # 李四提交改期请求
    new_start2 = start_time2 + timedelta(hours=1)
    new_end2 = new_start2 + timedelta(hours=1)
    reschedule_data2 = {
        "booking_id": booking_id2,
        "rescheduler_id": "u-lisi",
        "rescheduler_name": "Li Si",
        "new_start_time": new_start2.isoformat(),
        "new_end_time": new_end2.isoformat(),
        "reason": "想换个时间",
        "expected_version": booking_version2,
    }

    r = requests.post(
        f"{BASE_URL}/api/v1/bookings/{booking_id2}/reschedule",
        headers=headers2,
        data=json.dumps(reschedule_data2),
    )
    data = pprint_response("李四提交改期", r)
    assert_success(data, "李四提交改期失败")
    request_id2 = data["request"]["request_id"]
    booking_version2 = data["booking"]["version"]

    # 前台拒绝
    reject_data = {
        "request_id": request_id2,
        "approver_id": "u-recep",
        "approver_name": "Reception Li",
        "reason": "该时段已有安排",
        "expected_version": booking_version2,
    }
    r = requests.post(
        f"{BASE_URL}/api/v1/reschedule-requests/{request_id2}/reject",
        headers=admin_headers,
        data=json.dumps(reject_data),
    )
    data = pprint_response("前台拒绝改期", r)
    assert r.status_code == 200
    assert_success(data, "拒绝改期失败")
    assert data["request"]["status"] == "rejected", f"状态应为 rejected，实际: {data['request']['status']}"
    print(f"✓ 改期已驳回，原因: {data['request']['approve_reason']}")
    print(f"  原时段保持不变: {data['booking']['start_time']}")

    # 11. 验证日志
    print_separator("11. 请检查服务端日志，确认关键状态变化已记录")
    print("  应该看到以下日志:")
    print("  - 改期请求已提交 (reschedule request submitted)")
    print("  - 权限校验：普通成员无审批权限 (permission denied for member)")
    print("  - 改期请求已批准 (reschedule request approved)")
    print("  - 改期请求已驳回 (reschedule request rejected)")

    print_separator("测试完成 ✓")
    print("\n所有 API 链路测试通过！")
    print("请检查服务端日志确认关键状态变化已正确记录。")
    print("\n服务端日志中应包含以下关键信息：")
    print("  1. INFO 级别日志：改期请求已提交")
    print("  2. WARNING 或 ERROR 级别日志：权限校验失败（普通成员尝试审批）")
    print("  3. INFO 级别日志：改期请求已批准")
    print("  4. INFO 级别日志：改期请求已驳回")


if __name__ == "__main__":
    main()
