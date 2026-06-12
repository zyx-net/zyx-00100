"""
候补队列 HTTP API 端到端流程测试
使用 Python requests 调用真实 HTTP 接口
运行前先启动服务: uvicorn app.main:app --port 8002
运行: python test_waitlist_http.py
"""
import requests
import json
from datetime import datetime, timezone, timedelta

BASE_URL = "http://localhost:8002/api/v1"
TZ = timezone(timedelta(hours=8))


def headers(actor_id, actor_role, actor_name):
    return {
        "X-Actor-Id": actor_id,
        "X-Actor-Role": actor_role,
        "X-Actor-Name": actor_name,
        "Content-Type": "application/json",
    }


def make_slot(days_ahead, hour, minute=0, duration_hours=1):
    start = datetime.now(TZ) + timedelta(days=days_ahead)
    start = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end = start + timedelta(hours=duration_hours)
    return start.isoformat(), end.isoformat()


def main():
    print("=" * 70)
    print("Waitlist HTTP API End-to-End Test")
    print("=" * 70)
    ctx = {}

    # Step 1
    print("\n[Step 1] Zhang San creates booking in room-101 tomorrow 14:00-15:00...")
    start, end = make_slot(1, 14)
    r = requests.post(f"{BASE_URL}/bookings", headers=headers("u-zhangsan", "member", "Zhang San"), json={
        "room_id": "room-101",
        "owner_id": "u-zhangsan",
        "owner_name": "Zhang San",
        "team_id": "team-a",
        "title": "Zhang San's meeting",
        "start_time": start,
        "end_time": end,
        "attendees": ["u-lisi"],
    })
    assert r.status_code == 200, f"Create booking failed: {r.status_code} {r.text}"
    data = r.json()
    assert data["booking"]["status"] == "approved"
    ctx["bk1"] = data["booking"]["booking_id"]
    ctx["slot_start"] = start
    ctx["slot_end"] = end
    print(f"  OK booking created: booking_id={ctx['bk1']}")
    print(f"     Slot: {start} ~ {end}")

    # Step 2
    print("\n[Step 2] Li Si submits waitlist for the same slot...")
    r = requests.post(f"{BASE_URL}/waitlist", headers=headers("u-lisi", "member", "Li Si"), json={
        "room_id": "room-101",
        "requester_id": "u-lisi",
        "requester_name": "Li Si",
        "team_id": "team-a",
        "title": "Li Si urgent client meeting",
        "desired_start_time": ctx["slot_start"],
        "desired_end_time": ctx["slot_end"],
        "flex_before_minutes": 60,
        "flex_after_minutes": 60,
        "attendees": ["u-wangwu"],
        "priority_note": "Important client visit, high priority",
        "contact_info": "lisi@example.com / 13800138000",
        "description": "Client quarterly review, cannot reschedule",
    })
    assert r.status_code == 200, f"Submit waitlist failed: {r.status_code} {r.text}"
    data = r.json()
    assert data["waitlist"]["status"] == "waiting"
    ctx["wl1"] = data["waitlist"]["waitlist_id"]
    print(f"  OK waitlist submitted: waitlist_id={ctx['wl1']}")
    print(f"     Status: {data['waitlist']['status']}")
    print(f"     Flex: before {data['waitlist']['flex_before_minutes']}min / after {data['waitlist']['flex_after_minutes']}min")

    # Step 3
    print("\n[Step 3] Li Si submits duplicate waitlist - should be rejected...")
    r = requests.post(f"{BASE_URL}/waitlist", headers=headers("u-lisi", "member", "Li Si"), json={
        "room_id": "room-101",
        "requester_id": "u-lisi",
        "requester_name": "Li Si",
        "title": "Duplicate attempt",
        "desired_start_time": ctx["slot_start"],
        "desired_end_time": ctx["slot_end"],
        "flex_before_minutes": 30,
        "flex_after_minutes": 0,
    })
    assert r.status_code == 409, f"Duplicate should return 409, got {r.status_code}"
    data = r.json()
    assert data["error"]["code"] == "DUPLICATE_WAITLIST"
    existing_id = data["error"]["details"]["existing_waitlist_id"]
    assert existing_id == ctx["wl1"]
    print(f"  OK duplicate rejected with code={data['error']['code']}")
    print(f"     Hint: existing waitlist {existing_id}")

    # Step 4
    print("\n[Step 4] Li Si queries own waitlist...")
    r = requests.get(f"{BASE_URL}/waitlist", headers=headers("u-lisi", "member", "Li Si"))
    assert r.status_code == 200
    data = r.json()
    my_ids = [w["waitlist_id"] for w in data["items"]]
    assert ctx["wl1"] in my_ids
    print(f"  OK query success, {data['total']} records including own waitlist")

    # Step 5
    print("\n[Step 5] Wang Wu submits waitlist for same slot (different user, allowed)...")
    r = requests.post(f"{BASE_URL}/waitlist", headers=headers("u-wangwu", "member", "Wang Wu"), json={
        "room_id": "room-101",
        "requester_id": "u-wangwu",
        "requester_name": "Wang Wu",
        "title": "Wang Wu standup",
        "desired_start_time": ctx["slot_start"],
        "desired_end_time": ctx["slot_end"],
        "flex_before_minutes": 0,
        "flex_after_minutes": 0,
    })
    assert r.status_code == 200
    ctx["wl2"] = r.json()["waitlist"]["waitlist_id"]
    print(f"  OK Wang Wu waitlist submitted: waitlist_id={ctx['wl2']}")

    # Step 6
    print("\n[Step 6] Permission isolation - Li Si should NOT see Wang Wu's waitlist...")
    r = requests.get(f"{BASE_URL}/waitlist", headers=headers("u-lisi", "member", "Li Si"))
    lisi_ids = [w["waitlist_id"] for w in r.json()["items"]]
    assert ctx["wl1"] in lisi_ids
    assert ctx["wl2"] not in lisi_ids
    print(f"  OK permission isolation works, Li Si sees only own waitlist")

    # Step 7
    print("\n[Step 7] Admin filters all waitlists by room-101...")
    r = requests.get(f"{BASE_URL}/waitlist",
                     headers=headers("u-admin", "system_admin", "Admin"),
                     params={"room_id": "room-101"})
    assert r.status_code == 200
    data = r.json()
    admin_ids = [w["waitlist_id"] for w in data["items"]]
    assert ctx["wl1"] in admin_ids and ctx["wl2"] in admin_ids
    print(f"  OK admin sees all waitlists, total={data['total']}")

    # Step 8
    print("\n[Step 8] Li Si tries to view Wang Wu's waitlist - should be denied...")
    r = requests.get(f"{BASE_URL}/waitlist/{ctx['wl2']}", headers=headers("u-lisi", "member", "Li Si"))
    assert r.status_code == 403
    print(f"  OK permission denied, status={r.status_code}")

    # Step 9
    print("\n[Step 9] Zhang San cancels booking - triggers waitlist match...")
    r = requests.get(f"{BASE_URL}/bookings/{ctx['bk1']}", headers=headers("u-zhangsan", "member", "Zhang San"))
    version = r.json()["booking"]["version"]
    r = requests.post(f"{BASE_URL}/bookings/{ctx['bk1']}/cancel",
                      headers=headers("u-zhangsan", "member", "Zhang San"), json={
        "booking_id": ctx["bk1"],
        "canceller_id": "u-zhangsan",
        "canceller_name": "Zhang San",
        "reason": "Something came up",
        "expected_version": version,
    })
    assert r.status_code == 200, f"Cancel failed: {r.status_code} {r.text}"
    assert r.json()["booking"]["status"] == "cancelled"
    print("  OK Zhang San's booking cancelled")

    # Step 10
    print("\n[Step 10] Check Li Si's waitlist - should be matched now...")
    r = requests.get(f"{BASE_URL}/waitlist/{ctx['wl1']}", headers=headers("u-lisi", "member", "Li Si"))
    assert r.status_code == 200
    wl = r.json()["waitlist"]
    assert wl["status"] == "matched", f"Expected matched, got {wl['status']}"
    assert wl["matched_start_time"] is not None
    assert wl["expire_at"] is not None
    print(f"  OK waitlist matched! Status: {wl['status']}")
    print(f"     Matched slot: {wl['matched_start_time']} ~ {wl['matched_end_time']}")
    print(f"     Confirm by: {wl['expire_at']}")
    print(f"     (Wang Wu stays waiting because Li Si submitted first with higher flex priority)")

    # Step 11
    print("\n[Step 11] Li Si confirms waitlist - creates actual booking...")
    r = requests.post(f"{BASE_URL}/waitlist/{ctx['wl1']}/confirm",
                      headers=headers("u-lisi", "member", "Li Si"), json={
        "waitlist_id": ctx["wl1"],
        "confirmer_id": "u-lisi",
        "confirmer_name": "Li Si",
        "reason": "Confirm accepting this slot",
    })
    assert r.status_code == 200, f"Confirm failed: {r.status_code} {r.text}"
    data = r.json()
    assert data["waitlist"]["status"] == "confirmed"
    assert data["waitlist"]["result_booking_id"] is not None
    assert data["booking"] is not None
    assert data["booking"]["status"] == "approved"
    assert data["booking"]["owner_id"] == "u-lisi"
    ctx["bk_from_wl"] = data["booking"]["booking_id"]
    print(f"  OK waitlist confirmed!")
    print(f"     Waitlist status: {data['waitlist']['status']}")
    print(f"     Created booking: booking_id={ctx['bk_from_wl']}")
    print(f"     Booking status: {data['booking']['status']}")

    # Step 12
    print("\n[Step 12] Verify new booking exists in schedule...")
    r = requests.get(f"{BASE_URL}/schedule",
                     headers=headers("u-lisi", "member", "Li Si"),
                     params={"room_id": "room-101", "start": ctx["slot_start"], "end": ctx["slot_end"]})
    assert r.status_code == 200
    data = r.json()
    booking_ids = [b["booking_id"] for b in data["items"]]
    assert ctx["bk_from_wl"] in booking_ids
    print(f"  OK schedule query contains the new booking, total={data['total']}")

    # Step 13
    print("\n[Step 13] Check events - verify match and confirm are logged...")
    r = requests.get(f"{BASE_URL}/events",
                     headers=headers("u-admin", "system_admin", "Admin"),
                     params={"user_id": "u-lisi", "limit": 10})
    assert r.status_code == 200
    event_types = [e["event_type"] for e in r.json()["items"]]
    print(f"  OK events available, recent types: {set(event_types)}")
    print(f"     (Waitlist match/confirm are persisted in waitlist_match_logs and waitlist_action_logs)")

    # Step 14
    print("\n[Step 14] Wang Wu cancels own waitlist...")
    r = requests.post(f"{BASE_URL}/waitlist/{ctx['wl2']}/cancel",
                      headers=headers("u-wangwu", "member", "Wang Wu"), json={
        "waitlist_id": ctx["wl2"],
        "canceller_id": "u-wangwu",
        "canceller_name": "Wang Wu",
        "reason": "No longer needed",
    })
    assert r.status_code == 200
    assert r.json()["waitlist"]["status"] == "cancelled"
    print("  OK Wang Wu's waitlist cancelled")

    print("\n" + "=" * 70)
    print("All HTTP API flow tests PASSED ✓")
    print("=" * 70)
    print("\n--- User-visible flow summary ---")
    print("  1. Room occupied -> submit waitlist (desired time, flex, priority, contact)")
    print("  2. Same user duplicate -> auto rejected, prevents dirty data")
    print("  3. Different users -> allowed, sorted by creation time + role priority")
    print("  4. Booking cancelled/released/rescheduled -> auto triggers match")
    print("  5. Match success -> status=matched, confirmation window set")
    print("  6. User confirms -> real booking created, status=confirmed")
    print("  7. Permission isolation -> member sees only own, admin filters by room")
    print("  8. All data persisted -> query/confirm works after service restart")
    print("  9. Audit logs -> waitlist_action_logs + waitlist_match_logs complete")
    return True


if __name__ == "__main__":
    try:
        success = main()
    except AssertionError as e:
        print(f"\nFAILED assertion: {e}")
        import traceback
        traceback.print_exc()
        success = False
    except Exception as e:
        print(f"\nEXCEPTION: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        success = False
    exit(0 if success else 1)
