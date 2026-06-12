# 共享会议室预订冲突仲裁 API — 可复制示例手册
# ==========================================

> 规则版本：v1.0.0

## 0. 环境准备

```bash
# 安装依赖
pip install -r requirements.txt

# 初始化数据库和用户
python -m app.seed

# 启动服务
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 打开文档
# http://127.0.0.1:8000/docs
```

### 预初始化用户目录

| user_id     | 角色          | team  | 姓名   | 备注 |
|-------------|---------------|-------|--------|------|
| u-zhangsan  | member        | team-a| 张三   | 普通成员 |
| u-lisi      | member        | team-a| 李四   | 普通成员 |
| u-wangwu    | team_admin    | team-a| 王五   | 团队A管理员 |
| u-zhaoliu   | member        | team-b| 赵六   | 普通成员 |
| u-sunqi     | team_admin    | team-b| 孙七   | 团队B管理员 |
| u-recep     | receptionist  | -     | 前台小李 | 前台 |
| u-admin     | system_admin  | -     | 系统管理员 | 系统管理员（仅可仲裁） |

### 预初始化房间

| room_id   | 名称          | 容量 | 需要审批 | 签到宽限 | 预订窗口 |
|-----------|---------------|------|----------|----------|----------|
| room-101  | 创新空间 A    | 8    | 否       | 15 分钟  | 14 天    |
| room-102  | 创新空间 B    | 12   | 否       | 15 分钟  | 14 天    |
| room-201  | 董事会议室    | 20   | 是       | 20 分钟  | 30 天    |
| room-202  | 头脑风暴室    | 6    | 否       | 10 分钟  | 7 天     |

---

## 1. 公共请求头

所有命令请求必须携带以下 Header：

```
X-Actor-Id: <user_id>
X-Actor-Role: member|team_admin|receptionist|system_admin
X-Actor-Name: <姓名>
```

## 2. 主链路（黄金路径）：创建 → 审批 → 签到 → 释放 → 导出

以下示例使用可直接复制运行。请先在请求中替换为未来的时间。

### 2.1 查询房间

```bash
curl http://127.0.0.1:8000/api/v1/rooms
```

### 2.2 创建预订（无需审批：room-101）

```bash
# 先计算一个合适的未来时间（明天 10:00-11:00）
START="2026-06-14T10:00:00+08:00"
END="2026-06-14T11:00:00+08:00"

curl -X POST http://127.0.0.1:8000/api/v1/bookings \
  -H "Content-Type: application/json" \
  -H "X-Actor-Id: u-zhangsan" \
  -H "X-Actor-Role: member" \
  -H "X-Actor-Name: 张三" \
  -d "{
    \"room_id\": \"room-101\",
    \"owner_id\": \"u-zhangsan\",
    \"owner_name\": \"张三\",
    \"team_id\": \"team-a\",
    \"title\": \"产品周会\",
    \"start_time\": \"$START\",
    \"end_time\": \"$END\",
    \"attendees\": [\"u-lisi\", \"u-wangwu\"],
    \"description\": \"同步本周迭代进度\"
}" | python -m json.tool
```

**返回示例：
```json
{
  "success": true,
  "booking": {
    "booking_id": "bk-xxxx",
    "room_id": "room-101",
    "status": "approved",
    "version": 1
  },
  "events": [...]
}
```

> 记下 booking_id 和 version。

### 2.3 创建需要审批的预订（董事会议室 room-201）

```bash
START2="2026-06-14T14:00:00+08:00"
END2="2026-06-14T16:00:00+08:00"

curl -X POST http://127.0.0.1:8000/api/v1/bookings \
  -H "Content-Type: application/json" \
  -H "X-Actor-Id: u-wangwu" \
  -H "X-Actor-Role: team_admin" \
  -H "X-Actor-Name: 王五" \
  -d "{
    \"room_id\": \"room-201\",
    \"owner_id\": \"u-wangwu\",
    \"owner_name\": \"王五\",
    \"team_id\": \"team-a\",
    \"title\": \"客户路演筹备\",
    \"start_time\": \"$START2\",
    \"end_time\": \"$END2\",
    \"attendees\": [\"u-zhangsan\"]
}" | python -m json.tool
```

状态此时为 pending_approval。

### 2.4 审批预订（team_admin 或 system_admin 或 receptionist）

```bash
# 使用上一步 booking_id 和 version
BK_ID="bk-xxxx"
VER=1

curl -X POST http://127.0.0.1:8000/api/v1/bookings/$BK_ID/approve \
  -H "Content-Type: application/json" \
  -H "X-Actor-Id: u-recep" \
  -H "X-Actor-Role: receptionist" \
  -H "X-Actor-Name: 前台小李" \
  -d "{
    \"booking_id\": \"$BK_ID\",
    \"approver_id\": \"u-recep\",
    \"approver_name\": \"前台小李\",
    \"reason\": \"会议室资源充足，予以通过\",
    \"expected_version\": $VER
}" | python -m json.tool
```

### 2.5 签到（15 分钟宽限期内）

```bash
BK_ID="bk-xxxx"
VER=2  # 审批后版本号

curl -X POST http://127.0.0.1:8000/api/v1/bookings/$BK_ID/check-in \
  -H "Content-Type: application/json" \
  -H "X-Actor-Id: u-zhangsan" \
  -H "X-Actor-Role: member" \
  -H "X-Actor-Name: 张三" \
  -d "{
    \"booking_id\": \"$BK_ID\",
    \"check_in_user_id\": \"u-zhangsan\",
    \"check_in_user_name\": \"张三\",
    \"expected_version\": $VER
}" | python -m json.tool
```

### 2.6 释放未签到房间（过宽限期后自动释放）

```bash
# 先创建一个已经开始但未签到的预订（或用已有的 APPROVED 预订）
curl -X POST http://127.0.0.1:8000/api/v1/maintenance/auto-release \
  -H "Content-Type: application/json" \
  -H "X-Actor-Id: u-admin" \
  -H "X-Actor-Role: system_admin" \
  -H "X-Actor-Name: 系统管理员" | python -m json.tool
```

### 2.7 导出日程（CSV 下载）

```bash
# JSON 格式
curl "http://127.0.0.1:8000/api/v1/export?format=json" | python -m json.tool

# CSV 格式直接保存文件
curl -o schedule.csv "http://127.0.0.1:8000/api/v1/export?format=csv&download=true"

# 指定时间窗
curl "http://127.0.0.1:8000/api/v1/export?format=json&start=2026-06-14T00:00:00%2B08:00&end=2026-06-21T00:00:00%2B08:00" | python -m json.tool
```

### 2.8 查看日程

```bash
curl "http://127.0.0.1:8000/api/v1/schedule?room_id=room-101" | python -m json.tool
```

### 2.9 查询该预订的事件流

```bash
BK_ID="bk-xxxx"
curl "http://127.0.0.1:8000/api/v1/events?stream_id=$BK_ID" | python -m json.tool
```

---

## 3. 改期与取消

### 3.1 改期

```bash
BK_ID="bk-xxxx"
VER=1
NEW_START="2026-06-15T09:00:00+08:00"
NEW_END="2026-06-15T10:00:00+08:00"

curl -X POST http://127.0.0.1:8000/api/v1/bookings/$BK_ID/reschedule \
  -H "Content-Type: application/json" \
  -H "X-Actor-Id: u-wangwu" \
  -H "X-Actor-Role: team_admin" \
  -H "X-Actor-Name: 王五" \
  -d "{
    \"booking_id\": \"$BK_ID\",
    \"rescheduler_id\": \"u-wangwu\",
    \"rescheduler_name\": \"王五\",
    \"new_start_time\": \"$NEW_START\",
    \"new_end_time\": \"$NEW_END\",
    \"reason\": \"时间调整\",
    \"expected_version\": $VER
}" | python -m json.tool
```

### 3.2 取消预订

```bash
BK_ID="bk-xxxx"
VER=1

curl -X POST http://127.0.0.1:8000/api/v1/bookings/$BK_ID/cancel \
  -H "Content-Type: application/json" \
  -H "X-Actor-Id: u-zhangsan" \
  -H "X-Actor-Role: member" \
  -H "X-Actor-Name: 张三" \
  -d "{
    \"booking_id\": \"$BK_ID\",
    \"canceller_id\": \"u-zhangsan\",
    \"canceller_name\": \"张三\",
    \"reason\": \"会议取消\",
    \"expected_version\": $VER
}" | python -m json.tool
```

---

## 4. 冲突裁决（仲裁）

### 4.1 先制造一个重叠预订冲突
由于服务端已拦截重叠检测，正常情况下无法创建重叠预订。

**验证冲突错误**：
```bash
# 先创建第一个
START="2026-06-14T10:00:00+08:00"
END="2026-06-14T11:00:00+08:00"

curl -X POST http://127.0.0.1:8000/api/v1/bookings \
  -H "Content-Type: application/json" \
  -H "X-Actor-Id: u-zhangsan" -H "X-Actor-Role: member" -H "X-Actor-Name: 张三" \
  -d "{\"room_id\":\"room-102\",\"owner_id\":\"u-zhangsan\",\"owner_name\":\"张三\",\"team_id\":\"team-a\",\"title\":\"会议A\",\"start_time\":\"$START\",\"end_time\":\"$END\"}" | python -m json.tool

# 然后创建第二个（重叠），将返回 BOOKING_CONFLICT
curl -X POST http://127.0.0.1:8000/api/v1/bookings \
  -H "Content-Type: application/json" \
  -H "X-Actor-Id: u-zhaoliu" -H "X-Actor-Role: member" -H "X-Actor-Name: 赵六" \
  -d "{\"room_id\":\"room-102\",\"owner_id\":\"u-zhaoliu\",\"owner_name\":\"赵六\",\"team_id\":\"team-b\",\"title\":\"会议B\",\"start_time\":\"$START\",\"end_time\":\"$END\"}" | python -m json.tool
```

### 4.2 冲突分析 API
```bash
curl "http://127.0.0.1:8000/api/v1/conflicts/analyze?room_id=room-101&start=2026-06-14T10:00:00%2B08:00&end=2026-06-14T11:00:00%2B08:00" | python -m json.tool
```

### 4.3 替代时间推荐
```bash
curl "http://127.0.0.1:8000/api/v1/conflicts/suggest?room_id=room-101&desired_start=2026-06-14T10:00:00%2B08:00&desired_end=2026-06-14T11:00:00%2B08:00&search_days=3" | python -m json.tool
```

### 4.4 管理员仲裁（仅 system_admin 可调用）
```bash
BK_ID="bk-xxxx"  # 被仲裁的预订ID
VER=1

curl -X POST http://127.0.0.1:8000/api/v1/bookings/$BK_ID/arbitrate \
  -H "Content-Type: application/json" \
  -H "X-Actor-Id: u-admin" \
  -H "X-Actor-Role: system_admin" \
  -H "X-Actor-Name: 系统管理员" \
  -d "{
    \"booking_id\": \"$BK_ID\",
    \"arbitrator_id\": \"u-admin\",
    \"arbitrator_name\": \"系统管理员\",
    \"decision\": \"SUPERSEDE\",
    \"reason\": \"优先级更高的客户路演，原预订取消\",
    \"affected_booking_ids\": [\"bk-other1\"],
    \"expected_version\": $VER
}" | python -m json.tool
```

---

## 5. 错误场景验证

### 5.1 过期签到（宽限期已过）
```bash
# 创建一个开始时间已经过去 30 分钟的预订，然后签到
# （建议手工模拟：手动设置 check_in_time 为开始后 20 分钟）
# 将返回 CHECK_IN_GRACE_EXPIRED
```

### 5.2 旧版本并发写入（expected_version 太低）
```bash
# 假设 version=2 的预订，传 expected_version=1 去更新
# 将返回 CONCURRENCY_CONFLICT
```

### 5.3 越权仲裁（非系统管理员调用仲裁接口）
```bash
# 使用 member 或 team_admin 调用 /arbitrate
# 返回 PERMISSION_DENIED: 仅系统管理员可执行仲裁
```

### 5.4 成员越权审批其他团队的预订
```bash
# team-b 管理员去审批 team-a 的预订
# 团队管理员在当前设计下可以审批所有预订（看业务需要可收紧）
```

---

## 6. 事件回放验证：服务重启后重建日程

事件溯源架构保证：**所有当前状态 = 事件流按序回放结果**。

```bash
# 1. 停止服务（Ctrl+C）
# 2. 删除内存/重启（SQLite 文件仍在，因为是持久化的）
# 3. 重新启动服务
uvicorn app.main:app --reload

# 4. 查询事件流 — 仍然完整
curl "http://127.0.0.1:8000/api/v1/events?limit=5" | python -m json.tool

# 5. 查询日程 — 从事件重建，结果与重启前一致
curl "http://127.0.0.1:8000/api/v1/schedule" | python -m json.tool
```

一致性检查要点：
- 同一预订的 version 单调递增（ux_stream_version 唯一索引保证）
- 导出内容中 rule_version 与事件表中每条事件的 rule_version 一致
- 排序键：`start_time ASC, room_id ASC, booking_id ASC`，保证稳定排序

---

## 7. 事件历史查询

### 7.1 按事件类型查询
```bash
curl "http://127.0.0.1:8000/api/v1/events?event_type=booking_created" | python -m json.tool
```

### 7.2 按时间范围查询
```bash
curl "http://127.0.0.1:8000/api/v1/events?since=2026-06-13T00:00:00Z&until=2026-06-14T00:00:00Z" | python -m json.tool
```

### 7.3 按用户查询（所有涉及该用户的事件）
```bash
curl "http://127.0.0.1:8000/api/v1/events?user_id=u-zhangsan" | python -m json.tool
```

### 7.4 按房间查询
```bash
curl "http://127.0.0.1:8000/api/v1/events?room_id=room-201" | python -m json.tool
```

---

## 8. 错误码速查

| 错误码 | 含义 |
|--------|------|
| ROOM_NOT_FOUND | 房间不存在 |
| INVALID_TIME_RANGE | 开始时间必须早于结束时间 |
| DURATION_TOO_SHORT | 时长小于房间最小值 |
| DURATION_TOO_LONG | 时长超过房间最大值 |
| INVALID_TIME_SLOT | 时间未按步长对齐 |
| OUTSIDE_AVAILABLE_HOURS | 不在房间可用时间范围内 |
| BEYOND_BOOKING_WINDOW | 超出预订窗口 |
| BOOKING_CONFLICT | 与其他预订时间冲突 |
| PERMISSION_DENIED | 权限不足 |
| INVALID_STATUS | 状态机非法转换 |
| BOOKING_NOT_FOUND | 预订不存在 |
| CONCURRENCY_CONFLICT | 版本冲突（expected_version 不等于当前版本） |
| CHECK_IN_TOO_EARLY | 签到过早（>开始前30分钟） |
| CHECK_IN_GRACE_EXPIRED | 签到超过宽限期 |
| RELEASE_TOO_EARLY | 宽限期内不能释放 |
| CANCEL_WINDOW_EXPIRED | 已过取消窗口 |
| AGGREGATE_CORRUPT | 聚合根数据损坏 |
| ID_MISMATCH | 路径ID与请求体ID不一致 |
| INVALID_ROLE | 角色值非法 |
