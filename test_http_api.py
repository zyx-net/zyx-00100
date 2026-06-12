import urllib.request, urllib.error, urllib.parse, json
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
BASE = "http://127.0.0.1:8000"

def req(method, path, data=None, headers=None):
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8") if data else None
    r = urllib.request.Request(BASE + path, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = str(e)
        return e.code, body

ACTOR_ZHANGSAN = {
    "X-Actor-Id": "u-zhangsan", "X-Actor-Role": "member", "X-Actor-Name": "Zhang San",
}
ACTOR_WANGWU_ADMIN = {
    "X-Actor-Id": "u-wangwu", "X-Actor-Role": "team_admin", "X-Actor-Name": "Wang Wu",
}
ACTOR_RECEPTION = {
    "X-Actor-Id": "u-recep", "X-Actor-Role": "receptionist", "X-Actor-Name": "Reception",
}
ACTOR_ZHAOLIU = {
    "X-Actor-Id": "u-zhaoliu", "X-Actor-Role": "member", "X-Actor-Name": "Zhao Liu",
}

print("=" * 60)
print("HTTP API Verification")
print("=" * 60)

# 1. Root
code, d = req("GET", "/")
assert code == 200, f"GET / failed {code}"
print(f"[OK 200] / -> {d['name']} ver={d['version']}")

# 2. Health
code, d = req("GET", "/api/v1/health")
assert code == 200
print(f"[OK 200] /api/v1/health -> status={d['status']} rule={d['rule_version']}")

# 3. Rooms
code, d = req("GET", "/api/v1/rooms")
assert code == 200
print(f"[OK 200] /api/v1/rooms -> {len(d['items'])} rooms rule={d['rule_version']}")

# 4. Create booking (auto-approved) - use day+3 to avoid conflict with prior test data
start = (datetime.now(TZ) + timedelta(days=3)).replace(hour=9, minute=0, second=0, microsecond=0)
end = start + timedelta(hours=1)
code, d = req("POST", "/api/v1/bookings", {
    "room_id": "room-101",
    "owner_id": "u-zhangsan",
    "owner_name": "Zhang San",
    "team_id": "team-a",
    "title": "HTTP Test Meeting",
    "start_time": start.isoformat(),
    "end_time": end.isoformat(),
    "attendees": ["u-lisi"],
}, headers=ACTOR_ZHANGSAN)
assert code == 200, f"create booking got {code}: {d}"
assert d["success"] == True
bk = d["booking"]
bk_id = bk["booking_id"]
bk_ver = bk["version"]
print(f"[OK 200] POST /api/v1/bookings -> id={bk_id} status={bk['status']} ver={bk_ver}")

# 5. Create approval-needed booking
code, d = req("POST", "/api/v1/bookings", {
    "room_id": "room-201",
    "owner_id": "u-wangwu",
    "owner_name": "Wang Wu",
    "team_id": "team-a",
    "title": "Board Meeting (Needs Approval)",
    "start_time": (start + timedelta(hours=2)).isoformat(),
    "end_time": (start + timedelta(hours=4)).isoformat(),
}, headers=ACTOR_WANGWU_ADMIN)
assert code == 200 and d["success"]
bk2 = d["booking"]
bk2_id = bk2["booking_id"]
bk2_ver = bk2["version"]
assert bk2["status"] == "pending_approval"
print(f"[OK 200] create approval-needed -> id={bk2_id} status=pending ver={bk2_ver}")

# 6. Approve
code, d = req("POST", f"/api/v1/bookings/{bk2_id}/approve", {
    "booking_id": bk2_id,
    "approver_id": "u-recep",
    "approver_name": "Reception",
    "reason": "Available resources",
    "expected_version": bk2_ver,
}, headers=ACTOR_RECEPTION)
assert code == 200 and d["success"], f"approve failed {code}: {d}"
print(f"[OK 200] POST approve -> status={d['booking']['status']} ver={d['booking']['version']}")
bk2_ver = d["booking"]["version"]

# 7. REGRESSION: Member reschedules OWN booking (triggers fallback permission path) -> must NOT 500
# Create dedicated booking for Zhang San (member) then have him reschedule
resched_start = (datetime.now(TZ) + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
resched_end = resched_start + timedelta(hours=1)
code, d = req("POST", "/api/v1/bookings", {
    "room_id": "room-102",
    "owner_id": "u-zhangsan",
    "owner_name": "Zhang San",
    "team_id": "team-a",
    "title": "Reschedule test (HTTP) - own",
    "start_time": resched_start.isoformat(),
    "end_time": resched_end.isoformat(),
}, headers=ACTOR_ZHANGSAN)
assert code == 200 and d["success"], f"create for reschedule failed {code}: {d}"
rs_id = d["booking"]["booking_id"]
rs_ver = d["booking"]["version"]
rs_new_start = resched_start + timedelta(hours=2)
rs_new_end = rs_new_start + timedelta(hours=1)
code, d = req("POST", f"/api/v1/bookings/{rs_id}/reschedule", {
    "booking_id": rs_id,
    "rescheduler_id": "u-zhangsan",
    "rescheduler_name": "Zhang San",
    "new_start_time": rs_new_start.isoformat(),
    "new_end_time": rs_new_end.isoformat(),
    "reason": "HTTP reschedule member own",
    "expected_version": rs_ver,
}, headers=ACTOR_ZHANGSAN)
assert code != 500, f"REGRESSION 500: member rescheduling own booking! got {code}: {d}"
assert code == 200 and d["success"], f"member reschedule own failed {code}: {d}"
assert d["booking"]["status"] == "approved"
assert d["booking"]["version"] == rs_ver + 1
assert d["booking"]["start_time"] == rs_new_start.isoformat()
print(f"[OK 200] POST reschedule (member own) -> ver {rs_ver}->{d['booking']['version']} NO 500")

# 8. REGRESSION: Member Zhao Liu tries to reschedule Zhang San's booking -> PERMISSION_DENIED
rs2_id = rs_id
rs2_ver = d["booking"]["version"]
code, d = req("POST", f"/api/v1/bookings/{rs2_id}/reschedule", {
    "booking_id": rs2_id,
    "rescheduler_id": "u-zhaoliu",
    "rescheduler_name": "Zhao Liu",
    "new_start_time": (rs_new_start + timedelta(hours=3)).isoformat(),
    "new_end_time": (rs_new_end + timedelta(hours=3)).isoformat(),
    "expected_version": rs2_ver,
}, headers=ACTOR_ZHAOLIU)
assert code != 500, f"500 on permission check! got {code}: {d}"
assert code == 400 or code == 403, f"expected permission denied, got {code}: {d}"
assert d["error"]["code"] == "PERMISSION_DENIED"
print(f"[OK {code}] POST reschedule (member other) -> PERMISSION_DENIED: {d['error']['code']}")

# 9. Check-in
check_time = start + timedelta(minutes=5)
code, d = req("POST", f"/api/v1/bookings/{bk_id}/check-in", {
    "booking_id": bk_id,
    "check_in_user_id": "u-zhangsan",
    "check_in_user_name": "Zhang San",
    "check_in_time": check_time.isoformat(),
    "expected_version": bk_ver,
}, headers=ACTOR_ZHANGSAN)
assert code == 200 and d["success"], f"checkin failed {code}: {d}"
print(f"[OK 200] POST check-in -> status={d['booking']['status']} ver={d['booking']['version']}")
bk_ver = d["booking"]["version"]

# 10. Conflict
code, d = req("POST", "/api/v1/bookings", {
    "room_id": "room-101",
    "owner_id": "u-zhaoliu",
    "owner_name": "Zhao Liu",
    "title": "Conflict Booking (should fail)",
    "start_time": start.isoformat(),
    "end_time": end.isoformat(),
}, headers=ACTOR_ZHAOLIU)
assert code == 409, f"expected 409 conflict, got {code}: {d}"
assert d["error"]["code"] == "BOOKING_CONFLICT"
print(f"[OK 409] Overlap BOOKING_CONFLICT -> conflicts={len(d['error']['details']['conflicts'])}")

# 11. Unauthorized arbitration
code, d = req("POST", f"/api/v1/bookings/{bk_id}/arbitrate", {
    "booking_id": bk_id,
    "arbitrator_id": "u-wangwu",
    "arbitrator_name": "Wang Wu",
    "decision": "TEST",
    "reason": "unauthorized test",
    "expected_version": bk_ver,
}, headers=ACTOR_WANGWU_ADMIN)
assert code == 403, f"expected 403 permission denied, got {code}: {d}"
assert d["error"]["code"] == "PERMISSION_DENIED"
print(f"[OK 403] Unauthorized arbitration PERMISSION_DENIED")

# 12. Export
code, d = req("GET", "/api/v1/export?format=json")
assert code == 200
print(f"[OK 200] /api/v1/export json -> rows={d['row_count']} rule={d['rule_version']}")

# 13. Events query
code, d = req("GET", f"/api/v1/events?stream_id={bk_id}")
assert code == 200
print(f"[OK 200] /api/v1/events stream={bk_id} -> events={d['total']} rule={d['rule_version']}")

# 14. Conflict analysis (has conflict) - regression from round 2, must have rule_version
qs = urllib.parse.urlencode({
    "room_id": "room-101",
    "start": start.isoformat(),
    "end": end.isoformat(),
})
code, d = req("GET", f"/api/v1/conflicts/analyze?{qs}")
assert code == 200, f"conflict analyze got {code}: {d}"
assert d["has_conflict"] == True
assert d["rule_version"] == "v1.0.0"
assert d["incumbent"] is not None
assert isinstance(d["affected"], list) and len(d["affected"]) >= 1
print(f"[OK 200] conflicts/analyze (conflict) -> has_conflict=True rule={d['rule_version']} rec={d['recommendation']}")

# 15. REGRESSION (round 2): Conflict analysis (NO conflict / free window) - must NOT 500
free_start = (datetime.now(TZ) + timedelta(days=7)).replace(hour=8, minute=0, second=0, microsecond=0)
free_end = free_start + timedelta(hours=1)
qs_free = urllib.parse.urlencode({
    "room_id": "room-101",
    "start": free_start.isoformat(),
    "end": free_end.isoformat(),
})
code, d_free = req("GET", f"/api/v1/conflicts/analyze?{qs_free}")
assert code != 500, f"REGRESSION: free window returned 500! got {code}: {d_free}"
assert code == 200, f"free window analyze got {code}: {d_free}"
assert d_free["has_conflict"] == False
assert d_free["conflict_count"] == 0
assert d_free["recommendation"] == "ALLOW"
assert d_free["rule_version"] == "v1.0.0", f"rule_version missing or wrong: {d_free.get('rule_version')}"
assert d_free["incumbent"] is None
assert d_free["affected"] == []
print(f"[OK 200] conflicts/analyze (FREE) -> has_conflict=False rule={d_free['rule_version']} rec={d_free['recommendation']} NO 500")

# 16. rule_version consistency across endpoints (round 2 regression check)
code_rooms, rooms_d = req("GET", "/api/v1/rooms")
code_sched, sched_d = req("GET", "/api/v1/schedule")
code_export, export_d = req("GET", "/api/v1/export?format=json")
rv = rooms_d["rule_version"]
assert sched_d["rule_version"] == rv
assert export_d["rule_version"] == rv
assert d_free["rule_version"] == rv
print(f"[OK] rule_version consistent across all endpoints: {rv}")

# 17. Get single booking
code, d = req("GET", f"/api/v1/bookings/{bk_id}")
assert code == 200
print(f"[OK 200] GET booking -> status={d['booking']['status']} ver={d['booking']['version']}")

print("=" * 60)
print("All HTTP API Verifications PASSED!")
print("=" * 60)
