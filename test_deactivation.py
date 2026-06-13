import unittest
import json
from datetime import datetime, timedelta

from app.db import (
    SessionLocal, init_db, Base, engine,
    DeactivationPlan, DeactivationConflictSnapshot, DeactivationActionLog,
    EventStore,
)
from app.domain.permissions import (
    UserRole, Permission, BookingStatus,
    DeactivationPlanStatus, DeactivationRecurrenceType, ConflictResolutionAction,
    has_permission,
)
from app.services.deactivation_service import DeactivationService
from app.services.command_handler import CommandHandler, DomainError
from app.services.commands import CreateBookingCmd


class _TestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        self.db.close()

    def _cleanup(self):
        self.db.query(DeactivationActionLog).delete()
        self.db.query(DeactivationConflictSnapshot).delete()
        self.db.query(DeactivationPlan).delete()
        self.db.query(EventStore).delete()
        self.db.commit()

    def _near_future(self, day_offset=1, hour=10, minute=0):
        from app.services.command_handler import now_utc
        base = now_utc() + timedelta(days=day_offset)
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _create_booking(self, room_id="room-101", owner_id="u-zhangsan", owner_name="张三",
                        start=None, end=None, title="测试会议"):
        if start is None:
            start = self._near_future(1, 10, 0)
        if end is None:
            end = self._near_future(1, 11, 0)
        handler = CommandHandler(self.db)
        cmd = CreateBookingCmd(
            room_id=room_id, owner_id=owner_id, owner_name=owner_name,
            title=title, start_time=start, end_time=end,
        )
        result = handler.create_booking(cmd, owner_id, UserRole.MEMBER, owner_name)
        return result["booking"]


class TestPeriodicExpansion(_TestBase):
    def test_once_no_expansion(self):
        svc = DeactivationService(self.db)
        windows = svc._expand_windows(
            "room-101", "once",
            datetime(2026, 7, 1, 10, 0), datetime(2026, 7, 1, 12, 0),
            None, None,
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["start"], "2026-07-01T10:00:00")
        self.assertEqual(windows[0]["end"], "2026-07-01T12:00:00")

    def test_daily_expansion(self):
        svc = DeactivationService(self.db)
        windows = svc._expand_windows(
            "room-101", "daily",
            datetime(2026, 7, 1, 10, 0), datetime(2026, 7, 1, 12, 0),
            datetime(2026, 7, 5, 12, 0), {"interval": 1},
        )
        self.assertEqual(len(windows), 5)
        for i, w in enumerate(windows):
            expected_start = datetime(2026, 7, 1 + i, 10, 0).isoformat()
            self.assertEqual(w["start"], expected_start)

    def test_weekly_expansion(self):
        svc = DeactivationService(self.db)
        windows = svc._expand_windows(
            "room-101", "weekly",
            datetime(2026, 7, 1, 10, 0), datetime(2026, 7, 1, 12, 0),
            datetime(2026, 7, 22, 12, 0), {"interval": 1},
        )
        self.assertTrue(len(windows) >= 3)

    def test_monthly_expansion(self):
        svc = DeactivationService(self.db)
        windows = svc._expand_windows(
            "room-101", "monthly",
            datetime(2026, 7, 1, 10, 0), datetime(2026, 7, 1, 12, 0),
            datetime(2026, 10, 1, 12, 0), {"interval": 1},
        )
        self.assertTrue(len(windows) >= 3)

    def test_daily_with_interval(self):
        svc = DeactivationService(self.db)
        windows = svc._expand_windows(
            "room-101", "daily",
            datetime(2026, 7, 1, 10, 0), datetime(2026, 7, 1, 12, 0),
            datetime(2026, 7, 7, 12, 0), {"interval": 2},
        )
        self.assertTrue(len(windows) >= 3)


class TestPermissionIsolation(_TestBase):
    def test_member_cannot_create_plan(self):
        svc = DeactivationService(self.db)
        with self.assertRaises(DomainError) as ctx:
            svc.create_plan(
                room_id="room-101", reason="维护", impact_scope="全部",
                allow_auto_reschedule=False, recurrence_type="once",
                recurrence_rule=None,
                window_start=self._near_future(1, 10, 0),
                window_end=self._near_future(1, 12, 0),
                until_date=None,
                actor_id="u-zhangsan", actor_role=UserRole.MEMBER, actor_name="张三",
            )
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")

    def test_team_admin_cannot_create_plan(self):
        svc = DeactivationService(self.db)
        with self.assertRaises(DomainError) as ctx:
            svc.create_plan(
                room_id="room-101", reason="维护", impact_scope="全部",
                allow_auto_reschedule=False, recurrence_type="once",
                recurrence_rule=None,
                window_start=self._near_future(1, 10, 0),
                window_end=self._near_future(1, 12, 0),
                until_date=None,
                actor_id="u-wangwu", actor_role=UserRole.TEAM_ADMIN, actor_name="王五",
            )
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")

    def test_receptionist_can_create_plan(self):
        svc = DeactivationService(self.db)
        result = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 10, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-recep", actor_role=UserRole.RECEPTIONIST, actor_name="前台小李",
        )
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["room_id"], "room-101")

    def test_system_admin_can_create_plan(self):
        svc = DeactivationService(self.db)
        result = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 10, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        self.assertEqual(result["status"], "draft")

    def test_member_can_view_plans(self):
        svc = DeactivationService(self.db)
        svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 10, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        result = svc.list_plans(
            actor_id="u-zhangsan", actor_role=UserRole.MEMBER,
        )
        self.assertEqual(result["total"], 1)


class TestConflictSnapshot(_TestBase):
    def test_precheck_finds_booking_conflict(self):
        booking = self._create_booking()
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="空调维修", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 9, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        plan_id = plan["plan_id"]

        result = svc.precheck_plan(plan_id, "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        self.assertGreater(result["conflict_count"], 0)
        self.assertGreater(len(result["booking_conflicts"]), 0)
        self.assertEqual(result["booking_conflicts"][0]["booking_id"], booking["booking_id"])

    def test_confirm_creates_snapshots(self):
        booking = self._create_booking()
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="空调维修", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 9, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        plan_id = plan["plan_id"]
        svc.precheck_plan(plan_id, "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")

        result = svc.confirm_plan(plan_id, "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        self.assertEqual(result["status"], "processing")

        snapshots = self.db.query(DeactivationConflictSnapshot).filter(
            DeactivationConflictSnapshot.plan_id == plan_id,
        ).all()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].booking_id, booking["booking_id"])
        self.assertEqual(snapshots[0].resolution, ConflictResolutionAction.PENDING.value)
        self.assertEqual(snapshots[0].conflict_type, "booking")

    def test_no_conflict_when_booking_outside_window(self):
        self._create_booking(
            start=self._near_future(1, 14, 0),
            end=self._near_future(1, 15, 0),
        )
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="空调维修", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 9, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        result = svc.precheck_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        self.assertEqual(len(result["booking_conflicts"]), 0)


class TestConfirmAndResolve(_TestBase):
    def _setup_plan_with_conflict(self):
        booking = self._create_booking()
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="空调维修", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 9, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        svc.precheck_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        svc.confirm_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        return plan, booking

    def test_batch_cancel_conflict(self):
        plan, booking = self._setup_plan_with_conflict()
        svc = DeactivationService(self.db)
        result = svc.batch_resolve(
            plan_id=plan["plan_id"],
            resolutions=[{"booking_id": booking["booking_id"], "action": "cancel", "reason": "停用维护"}],
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(result["failed"], 0)

        agg = self.db.query(EventStore).filter(EventStore.stream_id == booking["booking_id"]).all()
        cancel_events = [e for e in agg if e.event_type == "booking_cancelled"]
        self.assertEqual(len(cancel_events), 1)

    def test_batch_reschedule_conflict(self):
        plan, booking = self._setup_plan_with_conflict()
        svc = DeactivationService(self.db)
        suggested_start = self._near_future(1, 14, 0).isoformat()
        suggested_end = self._near_future(1, 15, 0).isoformat()
        result = svc.batch_resolve(
            plan_id=plan["plan_id"],
            resolutions=[{
                "booking_id": booking["booking_id"],
                "action": "reschedule",
                "reason": "建议改期",
                "suggested_start": suggested_start,
                "suggested_end": suggested_end,
            }],
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        self.assertEqual(result["resolved"], 1)

        snap = self.db.query(DeactivationConflictSnapshot).filter(
            DeactivationConflictSnapshot.booking_id == booking["booking_id"],
        ).first()
        self.assertEqual(snap.resolution, "reschedule")
        suggestion = json.loads(snap.reschedule_suggestion)
        self.assertEqual(suggestion["suggested_start"], suggested_start)

    def test_batch_skip_conflict(self):
        plan, booking = self._setup_plan_with_conflict()
        svc = DeactivationService(self.db)
        result = svc.batch_resolve(
            plan_id=plan["plan_id"],
            resolutions=[{"booking_id": booking["booking_id"], "action": "skip", "reason": "跳过此预约"}],
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        self.assertEqual(result["skipped"], 1)

    def test_plan_becomes_processed_when_all_resolved(self):
        plan, booking = self._setup_plan_with_conflict()
        svc = DeactivationService(self.db)
        svc.batch_resolve(
            plan_id=plan["plan_id"],
            resolutions=[{"booking_id": booking["booking_id"], "action": "cancel", "reason": "停用维护"}],
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        plan_row = self.db.query(DeactivationPlan).filter(
            DeactivationPlan.plan_id == plan["plan_id"],
        ).first()
        self.assertEqual(plan_row.status, DeactivationPlanStatus.PROCESSED.value)


class TestRevoke(_TestBase):
    def test_revoke_plan_success(self):
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 10, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        result = svc.revoke_plan(
            plan_id=plan["plan_id"], reason="不需要了",
            expected_version=1,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        self.assertEqual(result["status"], "revoked")

    def test_revoke_version_conflict(self):
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 10, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        svc.modify_plan(
            plan_id=plan["plan_id"],
            reason="修改原因", impact_scope=None, allow_auto_reschedule=None,
            recurrence_type=None, recurrence_rule=None,
            window_start=None, window_end=None, until_date=None,
            expected_version=1,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        with self.assertRaises(DomainError) as ctx:
            svc.revoke_plan(
                plan_id=plan["plan_id"], reason="撤销",
                expected_version=1,
                actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
            )
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")

    def test_revoke_blocked_if_booking_version_changed(self):
        booking = self._create_booking()
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 9, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        svc.precheck_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        svc.confirm_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")

        svc.batch_resolve(
            plan_id=plan["plan_id"],
            resolutions=[{"booking_id": booking["booking_id"], "action": "cancel", "reason": "停用"}],
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )

        snap = self.db.query(DeactivationConflictSnapshot).filter(
            DeactivationConflictSnapshot.booking_id == booking["booking_id"],
        ).first()
        snap.booking_version = booking["version"] - 1
        self.db.flush()

        with self.assertRaises(DomainError) as ctx:
            svc.revoke_plan(
                plan_id=plan["plan_id"], reason="撤销",
                expected_version=plan["version"],
                actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
            )
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")


class TestRecoveryAfterRestart(_TestBase):
    def test_recover_completed_plan(self):
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 9, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        svc.precheck_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        svc.confirm_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")

        plan_row = self.db.query(DeactivationPlan).filter(
            DeactivationPlan.plan_id == plan["plan_id"],
        ).first()
        self.assertEqual(plan_row.status, "processing")

        recovered = svc.recover_incomplete_plans()
        self.assertEqual(recovered, 1)

        plan_row = self.db.query(DeactivationPlan).filter(
            DeactivationPlan.plan_id == plan["plan_id"],
        ).first()
        self.assertEqual(plan_row.status, DeactivationPlanStatus.PROCESSED.value)

    def test_recover_plan_with_pending_conflicts(self):
        booking = self._create_booking()
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 9, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        svc.precheck_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        svc.confirm_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")

        recovered = svc.recover_incomplete_plans()
        self.assertEqual(recovered, 1)

        plan_row = self.db.query(DeactivationPlan).filter(
            DeactivationPlan.plan_id == plan["plan_id"],
        ).first()
        self.assertEqual(plan_row.status, DeactivationPlanStatus.PROCESSING.value)

        result = svc.batch_resolve(
            plan_id=plan["plan_id"],
            resolutions=[{"booking_id": booking["booking_id"], "action": "cancel", "reason": "恢复后处理"}],
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        self.assertEqual(result["resolved"], 1)


class TestLogQuery(_TestBase):
    def test_logs_written_on_create(self):
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 10, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        result = svc.list_logs(plan["plan_id"])
        self.assertGreater(result["total"], 0)
        self.assertEqual(result["items"][0]["action"], "CREATE")
        self.assertEqual(result["items"][0]["actor_name"], "系统管理员")

    def test_logs_written_on_full_lifecycle(self):
        booking = self._create_booking()
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 9, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        svc.precheck_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        svc.confirm_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        svc.batch_resolve(
            plan_id=plan["plan_id"],
            resolutions=[{"booking_id": booking["booking_id"], "action": "cancel", "reason": "维护"}],
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )

        result = svc.list_logs(plan["plan_id"])
        actions = [item["action"] for item in result["items"]]
        self.assertIn("CREATE", actions)
        self.assertIn("PRECHECK", actions)
        self.assertIn("CONFIRM", actions)
        self.assertIn("RESOLVE_CANCEL", actions)
        self.assertIn("ALL_RESOLVED", actions)


class TestExport(_TestBase):
    def test_export_csv(self):
        booking = self._create_booking()
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 9, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        svc.precheck_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        svc.confirm_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")

        result = svc.export_affected(plan["plan_id"], format="csv")
        self.assertIn("content", result)
        self.assertEqual(result["row_count"], 1)
        self.assertIn(booking["booking_id"], result["content"])

    def test_export_json(self):
        booking = self._create_booking()
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 9, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        svc.precheck_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")
        svc.confirm_plan(plan["plan_id"], "u-admin", UserRole.SYSTEM_ADMIN, "系统管理员")

        result = svc.export_affected(plan["plan_id"], format="json")
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["items"][0]["预约ID"], booking["booking_id"])


class TestOverlappingPlan(_TestBase):
    def test_cannot_create_overlapping_plan(self):
        svc = DeactivationService(self.db)
        svc.create_plan(
            room_id="room-101", reason="维护1", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 10, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        with self.assertRaises(DomainError) as ctx:
            svc.create_plan(
                room_id="room-101", reason="维护2", impact_scope="全部",
                allow_auto_reschedule=False, recurrence_type="once",
                recurrence_rule=None,
                window_start=self._near_future(1, 11, 0),
                window_end=self._near_future(1, 13, 0),
                until_date=None,
                actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
            )
        self.assertEqual(ctx.exception.code, "OVERLAPPING_PLAN")


class TestModifyPlan(_TestBase):
    def test_modify_reason(self):
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 10, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        result = svc.modify_plan(
            plan_id=plan["plan_id"],
            reason="紧急维修", impact_scope=None, allow_auto_reschedule=None,
            recurrence_type=None, recurrence_rule=None,
            window_start=None, window_end=None, until_date=None,
            expected_version=1,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        self.assertEqual(result["reason"], "紧急维修")
        self.assertEqual(result["version"], 2)

    def test_modify_version_conflict(self):
        svc = DeactivationService(self.db)
        plan = svc.create_plan(
            room_id="room-101", reason="维护", impact_scope="全部",
            allow_auto_reschedule=False, recurrence_type="once",
            recurrence_rule=None,
            window_start=self._near_future(1, 10, 0),
            window_end=self._near_future(1, 12, 0),
            until_date=None,
            actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
        )
        with self.assertRaises(DomainError) as ctx:
            svc.modify_plan(
                plan_id=plan["plan_id"],
                reason="修改", impact_scope=None, allow_auto_reschedule=None,
                recurrence_type=None, recurrence_rule=None,
                window_start=None, window_end=None, until_date=None,
                expected_version=99,
                actor_id="u-admin", actor_role=UserRole.SYSTEM_ADMIN, actor_name="系统管理员",
            )
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")


if __name__ == "__main__":
    unittest.main()
