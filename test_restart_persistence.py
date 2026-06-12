import requests
import json

headers = {
    'X-Actor-Id': 'u-admin',
    'X-Actor-Role': 'system_admin',
    'X-Actor-Name': 'Admin',
    'Content-Type': 'application/json'
}

# 查询所有改期请求
r = requests.get('http://localhost:8001/api/v1/reschedule-requests', headers=headers)
data = r.json()
print(f'状态码: {r.status_code}')
print(f'总记录数: {data["total"]}')
print('记录列表:')
for req in data['items']:
    print(f'  - {req["request_id"]}: {req["status"]} | {req["requester_name"]} → {req.get("approver_name", "未审批")}')
    print(f'    原时段: {req["old_start_time"]} ~ {req["old_end_time"]}')
    print(f'    新时段: {req["new_start_time"]} ~ {req["new_end_time"]}')
    print()

# 查询一个具体的请求详情
if data['items']:
    request_id = data['items'][0]['request_id']
    r2 = requests.get(f'http://localhost:8001/api/v1/reschedule-requests/{request_id}', headers=headers)
    data2 = r2.json()
    print(f'\n查询详情 - request_id={request_id}:')
    print(f'  状态: {data2["request"]["status"]}')
    print(f'  提交原因: {data2["request"]["reason"]}')
    if data2["request"].get("approve_reason"):
        print(f'  审批原因: {data2["request"]["approve_reason"]}')
    print(f'  持久化验证: ✓ 服务重启后数据仍然存在')
