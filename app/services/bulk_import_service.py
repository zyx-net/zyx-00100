from __future__ import annotations
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from sqlalchemy.orm import Session
import csv
import io
import json
import uuid
import logging

from ..db import (
    BulkImportBatch, BulkImportDraft, BulkImportOperationLog,
)
from ..config import settings, get_room_config
from ..domain.permissions import (
    UserRole, Permission, has_permission,
    BulkImportBatchStatus, BulkImportDraftStatus, BulkImportPrecheckStatus,
)
from ..services.command_handler import DomainError, now_utc
from ..services.event_store import EventStoreService
from ..services.commands import CreateBookingCmd

logger = logging.getLogger(__name__)


def _generate_batch_id() -> str:
    return f"batch-{uuid.uuid4().hex[:12]}"


def _generate_log_id() -> str:
    return f"bilog-{uuid.uuid4().hex[:12]}"


def _parse_attendees(val: Any) -> List[str]:
    if val is None or val == "":
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        parts = [p.strip() for p in val.replace("，", ",").split(",")]
        return [p for p in parts if p]
    return []


def _parse_datetime(val: Any) -> Optional[datetime]:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        s = val.strip()
        formats = [
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    return dt
                return dt
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None
    return None


class BulkImportService:
    def __init__(self, db: Session):
        self.db = db
        self.store = EventStoreService(db)

    def _log_operation(
        self,
        batch_id: str,
        operation: str,
        old_status: Optional[str],
        new_status: Optional[str],
        actor_id: str,
        actor_name: str,
        actor_role: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        log = BulkImportOperationLog(
            log_id=_generate_log_id(),
            batch_id=batch_id,
            operation=operation,
            old_status=old_status,
            new_status=new_status,
            details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role.value if isinstance(actor_role, UserRole) else actor_role,
            rule_version=settings.rule_version,
        )
        self.db.add(log)
        self.db.flush()

    # ---------- CSV / JSON 解析 ----------

    def parse_csv(self, csv_content: str) -> List[Dict[str, Any]]:
        buf = io.StringIO(csv_content)
        reader = csv.DictReader(buf)
        rows = []
        for row in reader:
            rows.append({k: (v if v != "" else None) for k, v in row.items()})
        return rows

    def normalize_row(self, raw: Dict[str, Any], row_number: int) -> Dict[str, Any]:
        def get(*keys: str) -> Any:
            for k in keys:
                if k in raw and raw[k] is not None:
                    return raw[k]
            return None

        room_id = get("room_id", "roomId", "房间ID", "房间", "会议室", "会议室ID")
        owner_id = get("owner_id", "ownerId", "申请人ID", "申请人id", "申请人")
        owner_name = get("owner_name", "ownerName", "申请人姓名", "申请人名称", "主持人", "申请人名字")
        team_id = get("team_id", "teamId", "团队ID", "团队", "部门", "部门ID")
        title = get("title", "主题", "会议主题", "标题", "会议标题")
        start_time = get("start_time", "startTime", "开始时间", "起始时间")
        end_time = get("end_time", "endTime", "结束时间", "截止时间")
        attendees = get("attendees", "参会人", "与会人", "参会人员", "参与者")
        description = get("description", "备注", "说明", "描述", "会议说明")

        return {
            "room_id": str(room_id).strip() if room_id else None,
            "owner_id": str(owner_id).strip() if owner_id else None,
            "owner_name": str(owner_name).strip() if owner_name else None,
            "team_id": str(team_id).strip() if team_id else None,
            "title": str(title).strip() if title else None,
            "start_time_raw": start_time,
            "end_time_raw": end_time,
            "start_time": _parse_datetime(start_time),
            "end_time": _parse_datetime(end_time),
            "attendees_raw": attendees,
            "attendees": _parse_attendees(attendees),
            "description": str(description).strip() if description else None,
            "_raw": raw,
            "_row_number": row_number,
        }

    # ---------- 上传草稿 ----------

    def upload_drafts(
        self,
        format: str,
        rows: List[Dict[str, Any]],
        csv_content: Optional[str],
        filename: Optional[str],
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.BULK_IMPORT_DRAFT):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无批量导入权限")

        if csv_content:
            parsed_rows = self.parse_csv(csv_content)
        else:
            parsed_rows = rows

        if not parsed_rows:
            raise DomainError("EMPTY_IMPORT", "导入内容为空")

        batch_id = _generate_batch_id()

        batch = BulkImportBatch(
            batch_id=batch_id,
            submitter_id=actor_id,
            submitter_name=actor_name,
            submitter_role=actor_role.value,
            source_format=format,
            source_filename=filename,
            total_count=len(parsed_rows),
            status=BulkImportBatchStatus.DRAFT.value,
            rule_version=settings.rule_version,
        )
        self.db.add(batch)
        self.db.flush()

        normalized_rows = [self.normalize_row(r, i + 1) for i, r in enumerate(parsed_rows)]

        for idx, nr in enumerate(normalized_rows):
            draft = BulkImportDraft(
                batch_id=batch_id,
                draft_index=idx,
                row_number=nr["_row_number"],
                room_id=nr["room_id"],
                owner_id=nr["owner_id"],
                owner_name=nr["owner_name"],
                team_id=nr["team_id"],
                title=nr["title"],
                start_time=nr["start_time"],
                end_time=nr["end_time"],
                attendees=json.dumps(nr["attendees"], ensure_ascii=False) if nr["attendees"] else None,
                description=nr["description"],
                raw_row_data=json.dumps(nr["_raw"], ensure_ascii=False, default=str),
                precheck_status=BulkImportPrecheckStatus.PENDING.value,
                result_status=BulkImportDraftStatus.PENDING.value,
                rule_version=settings.rule_version,
            )
            self.db.add(draft)

        self.db.flush()

        self._log_operation(
            batch_id=batch_id,
            operation="UPLOAD",
            old_status=None,
            new_status=BulkImportBatchStatus.DRAFT.value,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            details={"count": len(normalized_rows), "format": format, "filename": filename},
        )
        self.db.commit()
        return self._batch_to_dict(batch, include_drafts=True)

    # ---------- 预检 ----------

    def _precheck_single(
        self,
        draft: BulkImportDraft,
        actor_id: str,
        actor_role: UserRole,
        existing_slots: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        errors: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []

        row_ref = f"第{draft.row_number}行"

        if not draft.room_id:
            errors.append({"field": "room_id", "code": "REQUIRED", "message": f"{row_ref}: 缺少房间ID"})
        else:
            room_cfg = get_room_config(draft.room_id)
            if not room_cfg:
                errors.append({"field": "room_id", "code": "ROOM_NOT_FOUND", "message": f"{row_ref}: 房间 {draft.room_id} 不存在"})

        if not draft.owner_id:
            errors.append({"field": "owner_id", "code": "REQUIRED", "message": f"{row_ref}: 缺少申请人ID"})
        else:
            if actor_role == UserRole.MEMBER and draft.owner_id != actor_id:
                errors.append({
                    "field": "owner_id",
                    "code": "PERMISSION_DENIED",
                    "message": f"{row_ref}: 普通成员只能为自己导入（申请人ID必须是自己）",
                })

        if not draft.owner_name:
            errors.append({"field": "owner_name", "code": "REQUIRED", "message": f"{row_ref}: 缺少申请人姓名"})

        if not draft.title:
            errors.append({"field": "title", "code": "REQUIRED", "message": f"{row_ref}: 缺少会议标题"})

        if draft.start_time is None:
            errors.append({"field": "start_time", "code": "INVALID_TIME_FORMAT", "message": f"{row_ref}: 开始时间格式无效"})
        if draft.end_time is None:
            errors.append({"field": "end_time", "code": "INVALID_TIME_FORMAT", "message": f"{row_ref}: 结束时间格式无效"})

        room_cfg = get_room_config(draft.room_id) if draft.room_id else None
        if draft.start_time and draft.end_time and room_cfg:
            if draft.start_time >= draft.end_time:
                errors.append({"field": "start_time", "code": "INVALID_TIME_RANGE", "message": f"{row_ref}: 开始时间必须早于结束时间"})
            else:
                duration_min = (draft.end_time - draft.start_time).total_seconds() / 60
                if duration_min < room_cfg.min_duration_minutes:
                    errors.append({
                        "field": "duration",
                        "code": "DURATION_TOO_SHORT",
                        "message": f"{row_ref}: 时长 {duration_min} 分钟小于最小值 {room_cfg.min_duration_minutes} 分钟",
                    })
                if duration_min > room_cfg.max_duration_minutes:
                    errors.append({
                        "field": "duration",
                        "code": "DURATION_TOO_LONG",
                        "message": f"{row_ref}: 时长 {duration_min} 分钟超过最大值 {room_cfg.max_duration_minutes} 分钟",
                    })

                step = room_cfg.time_slot_step_minutes
                if (draft.start_time.minute % step) != 0 or (draft.end_time.minute % step) != 0:
                    warnings.append({
                        "field": "time_slot",
                        "code": "INVALID_TIME_SLOT",
                        "message": f"{row_ref}: 时间未按 {step} 分钟步长对齐",
                    })

                start_time_only = draft.start_time.time()
                end_time_only = draft.end_time.time()
                if start_time_only < room_cfg.available_from or end_time_only > room_cfg.available_to:
                    errors.append({
                        "field": "available_hours",
                        "code": "OUTSIDE_AVAILABLE_HOURS",
                        "message": f"{row_ref}: 超出房间可用时间 {room_cfg.available_from} - {room_cfg.available_to}",
                    })

                booking_window_start = now_utc().date()
                max_booking_date = booking_window_start.fromordinal(
                    booking_window_start.toordinal() + room_cfg.booking_window_days
                )
                if draft.start_time.date() > max_booking_date:
                    errors.append({
                        "field": "booking_window",
                        "code": "BEYOND_BOOKING_WINDOW",
                        "message": f"{row_ref}: 只能在 {room_cfg.booking_window_days} 天内预订",
                    })

                if draft.start_time.date() < booking_window_start:
                    warnings.append({
                        "field": "past_time",
                        "code": "PAST_TIME_SLOT",
                        "message": f"{row_ref}: 时间为过去时段",
                    })

        if errors:
            return {
                "precheck_status": BulkImportPrecheckStatus.ERROR.value,
                "errors": errors,
                "warnings": warnings,
            }

        # 检查与现有预约的冲突
        if draft.room_id and draft.start_time and draft.end_time:
            conflicts = self.store.find_conflicting_bookings(
                draft.room_id, draft.start_time, draft.end_time
            )
            if conflicts:
                errors.append({
                    "field": "time_range",
                    "code": "BOOKING_CONFLICT",
                    "message": f"{row_ref}: 与 {len(conflicts)} 个现有预约冲突",
                    "details": {
                        "conflicts": [
                            {
                                "booking_id": c.booking_id,
                                "title": c.title,
                                "start_time": c.start_time.isoformat() if c.start_time else None,
                                "end_time": c.end_time.isoformat() if c.end_time else None,
                                "owner_name": c.owner_name,
                                "status": c.status.value,
                            }
                            for c in conflicts
                        ]
                    },
                })

            # 检查批次内部冲突
            internal_conflicts = []
            for slot in existing_slots:
                if slot["room_id"] != draft.room_id:
                    continue
                if slot["start_time"] and slot["end_time"]:
                    from ..domain.aggregate import overlaps
                    if overlaps(draft.start_time, draft.end_time, slot["start_time"], slot["end_time"]):
                        internal_conflicts.append({
                            "draft_index": slot["draft_index"],
                            "row_number": slot["row_number"],
                            "title": slot.get("title"),
                        })
            if internal_conflicts:
                errors.append({
                    "field": "time_range",
                    "code": "INTERNAL_CONFLICT",
                    "message": f"{row_ref}: 与本批次内 {len(internal_conflicts)} 条草稿冲突",
                    "details": {"conflicts": internal_conflicts},
                })

        if errors:
            return {
                "precheck_status": BulkImportPrecheckStatus.ERROR.value,
                "errors": errors,
                "warnings": warnings,
            }
        if warnings:
            return {
                "precheck_status": BulkImportPrecheckStatus.WARNING.value,
                "errors": [],
                "warnings": warnings,
            }
        return {
            "precheck_status": BulkImportPrecheckStatus.PASSED.value,
            "errors": [],
            "warnings": [],
        }

    def run_precheck(
        self,
        batch_id: str,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
    ) -> Dict[str, Any]:
        batch = self.db.query(BulkImportBatch).filter(BulkImportBatch.batch_id == batch_id).first()
        if not batch:
            raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")

        if batch.status not in (
            BulkImportBatchStatus.DRAFT.value,
            BulkImportBatchStatus.PRECHECK_FAILED.value,
            BulkImportBatchStatus.PRECHECKED.value,
        ):
            raise DomainError("INVALID_STATUS", f"批次当前状态 {batch.status} 不允许预检")

        if batch.submitter_id != actor_id:
            if not has_permission(actor_role, Permission.BULK_IMPORT_VIEW_ALL):
                raise DomainError("PERMISSION_DENIED", "无权查看他人的导入批次")

        old_status = batch.status
        batch.status = BulkImportBatchStatus.PRECHECKING.value
        self.db.flush()

        drafts = self.db.query(BulkImportDraft).filter(
            BulkImportDraft.batch_id == batch_id
        ).order_by(BulkImportDraft.draft_index.asc()).all()

        existing_slots: List[Dict[str, Any]] = []
        passed = 0
        error_count = 0
        warning_count = 0

        for draft in drafts:
            result = self._precheck_single(draft, actor_id, actor_role, existing_slots)
            draft.precheck_status = result["precheck_status"]
            draft.precheck_errors = json.dumps(result["errors"], ensure_ascii=False, default=str) if result["errors"] else None
            draft.precheck_warnings = json.dumps(result["warnings"], ensure_ascii=False, default=str) if result["warnings"] else None

            if result["precheck_status"] == BulkImportPrecheckStatus.ERROR.value:
                error_count += 1
            elif result["precheck_status"] == BulkImportPrecheckStatus.WARNING.value:
                warning_count += 1
                passed += 1
            else:
                passed += 1

            if draft.room_id and draft.start_time and draft.end_time and result["precheck_status"] != BulkImportPrecheckStatus.ERROR.value:
                existing_slots.append({
                    "draft_index": draft.draft_index,
                    "row_number": draft.row_number,
                    "room_id": draft.room_id,
                    "start_time": draft.start_time,
                    "end_time": draft.end_time,
                    "title": draft.title,
                })

        batch.precheck_passed = error_count == 0
        batch.precheck_at = now_utc()
        batch.status = (
            BulkImportBatchStatus.PRECHECKED.value
            if batch.precheck_passed
            else BulkImportBatchStatus.PRECHECK_FAILED.value
        )
        self.db.flush()

        self._log_operation(
            batch_id=batch_id,
            operation="PRECHECK",
            old_status=old_status,
            new_status=batch.status,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            details={
                "passed": passed,
                "error_count": error_count,
                "warning_count": warning_count,
            },
        )
        self.db.commit()
        return self._batch_to_dict(batch, include_drafts=True)

    # ---------- 确认提交 ----------

    def confirm_batch(
        self,
        batch_id: str,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        from ..services.command_handler import CommandHandler
        from ..services.commands import CreateBookingCmd

        if not has_permission(actor_role, Permission.BULK_IMPORT_CONFIRM):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无确认导入权限")

        batch = self.db.query(BulkImportBatch).filter(BulkImportBatch.batch_id == batch_id).first()
        if not batch:
            raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")

        if batch.submitter_id != actor_id:
            if not has_permission(actor_role, Permission.BULK_IMPORT_VIEW_ALL):
                raise DomainError("PERMISSION_DENIED", "无权操作他人的导入批次")

        if batch.status in (
            BulkImportBatchStatus.CONFIRMED.value,
            BulkImportBatchStatus.CANCELLED.value,
        ):
            raise DomainError("ALREADY_PROCESSED", f"批次已处于 {batch.status} 状态，不可重复确认")

        if not batch.precheck_passed:
            # 预检未通过，自动先跑一次预检
            self.run_precheck(batch_id, actor_id, actor_role, actor_name)
            batch = self.db.query(BulkImportBatch).filter(BulkImportBatch.batch_id == batch_id).first()
            if not batch.precheck_passed:
                raise DomainError("PRECHECK_REQUIRED", "预检未通过，请先修正错误后重试")

        old_status = batch.status
        batch.status = BulkImportBatchStatus.CONFIRMING.value
        self.db.flush()

        drafts = self.db.query(BulkImportDraft).filter(
            BulkImportDraft.batch_id == batch_id
        ).order_by(BulkImportDraft.draft_index.asc()).all()

        # 二次预检：重新检查冲突
        # 策略：区分"致命错误（不可重试）"和"冲突错误（可交给创建流程标记retryable）"
        NON_RETRYABLE_CODES = {
            "REQUIRED", "ROOM_NOT_FOUND", "INVALID_TIME_FORMAT",
            "INVALID_TIME_RANGE", "DURATION_TOO_SHORT", "DURATION_TOO_LONG",
            "OUTSIDE_AVAILABLE_HOURS", "BEYOND_BOOKING_WINDOW", "PERMISSION_DENIED",
        }
        for draft in drafts:
            if draft.precheck_status == BulkImportPrecheckStatus.ERROR.value:
                # 检查原始预检错误是否都是不可重试的。如果有任何一个是可重试的，不跳过。
                prev_errs = json.loads(draft.precheck_errors) if draft.precheck_errors else []
                non_retryable_found = any(
                    e.get("code") in NON_RETRYABLE_CODES for e in prev_errs
                )
                if non_retryable_found:
                    continue  # 确实有不可重试的错误，跳过
            # 跳过原始错误中没有不可重试的，或者没有错误的，进行重检
            slots_from_batch = []
            for d2 in drafts:
                if d2.draft_index != draft.draft_index and d2.room_id and d2.start_time and d2.end_time:
                    slots_from_batch.append({
                        "draft_index": d2.draft_index,
                        "row_number": d2.row_number,
                        "room_id": d2.room_id,
                        "start_time": d2.start_time,
                        "end_time": d2.end_time,
                        "title": d2.title,
                    })
            recheck = self._precheck_single(draft, actor_id, actor_role, slots_from_batch)
            if recheck["precheck_status"] == BulkImportPrecheckStatus.ERROR.value:
                # 检查重检结果是否有不可重试错误
                has_non_retryable = any(
                    e.get("code") in NON_RETRYABLE_CODES for e in recheck["errors"]
                )
                if has_non_retryable:
                    # 有不可重试错误 -> 标记跳过
                    draft.precheck_status = BulkImportPrecheckStatus.ERROR.value
                    prev_errors = json.loads(draft.precheck_errors) if draft.precheck_errors else []
                    codes = {e.get("code") for e in prev_errors}
                    for e in recheck["errors"]:
                        if e.get("code") not in codes:
                            prev_errors.append(e)
                    draft.precheck_errors = json.dumps(prev_errors, ensure_ascii=False, default=str)
                # 如果只有 BOOKING_CONFLICT / INTERNAL_CONFLICT 等可重试错误 -> 不标记，交给创建流程处理

        # 过滤出可以尝试创建的草稿（只有明确标记 error 且包含不可重试码的才跳过）
        def _is_fatal(d: BulkImportDraft) -> bool:
            if d.precheck_status != BulkImportPrecheckStatus.ERROR.value:
                return False
            errs = json.loads(d.precheck_errors) if d.precheck_errors else []
            return any(e.get("code") in NON_RETRYABLE_CODES for e in errs)

        creatable = [d for d in drafts if not _is_fatal(d)]
        handler = CommandHandler(self.db)

        success_count = 0
        failed_count = 0
        retryable_count = 0
        results: List[Dict[str, Any]] = []
        all_events: List[Dict[str, Any]] = []

        for draft in creatable:
            try:
                cmd = CreateBookingCmd(
                    room_id=draft.room_id,
                    owner_id=draft.owner_id,
                    owner_name=draft.owner_name,
                    team_id=draft.team_id,
                    title=draft.title,
                    start_time=draft.start_time,
                    end_time=draft.end_time,
                    attendees=json.loads(draft.attendees) if draft.attendees else [],
                    description=draft.description,
                )
                result = handler.create_booking(cmd, draft.owner_id, actor_role, draft.owner_name or actor_name)
                booking_id = result["booking"]["booking_id"]
                draft.result_status = BulkImportDraftStatus.CREATED.value
                draft.result_booking_id = booking_id
                draft.result_error = None
                draft.retryable = False
                success_count += 1
                results.append({
                    "draft_index": draft.draft_index,
                    "row_number": draft.row_number,
                    "status": "created",
                    "booking_id": booking_id,
                })
                all_events.extend(result["events"])
            except DomainError as e:
                failed_count += 1
                draft.result_status = BulkImportDraftStatus.CREATE_FAILED.value
                draft.result_error = json.dumps(e.to_dict(), ensure_ascii=False, default=str)
                retryable = e.code in ("BOOKING_CONFLICT", "CONCURRENCY_CONFLICT")
                draft.retryable = retryable
                if retryable:
                    retryable_count += 1
                results.append({
                    "draft_index": draft.draft_index,
                    "row_number": draft.row_number,
                    "status": "failed",
                    "error": e.to_dict(),
                    "retryable": retryable,
                })
            except Exception as e:
                logger.exception(f"创建预约失败 draft_index={draft.draft_index}")
                failed_count += 1
                draft.result_status = BulkImportDraftStatus.CREATE_FAILED.value
                draft.result_error = json.dumps({"code": "UNKNOWN_ERROR", "message": str(e)}, ensure_ascii=False, default=str)
                draft.retryable = True
                retryable_count += 1
                results.append({
                    "draft_index": draft.draft_index,
                    "row_number": draft.row_number,
                    "status": "failed",
                    "error": {"code": "UNKNOWN_ERROR", "message": str(e)},
                    "retryable": True,
                })

        # 统计预检查失败的也算在 failed_count
        precheck_failed = [d for d in drafts if d.precheck_status == BulkImportPrecheckStatus.ERROR.value]
        total_failed = failed_count + len(precheck_failed)

        batch.success_count = success_count
        batch.failed_count = total_failed
        batch.confirmed_at = now_utc()
        batch.confirmed_by_id = actor_id
        batch.confirmed_by_name = actor_name

        if total_failed == 0:
            batch.status = BulkImportBatchStatus.CONFIRMED.value
        elif success_count > 0:
            batch.status = BulkImportBatchStatus.PARTIALLY_FAILED.value
        else:
            batch.status = BulkImportBatchStatus.PRECHECK_FAILED.value

        self.db.flush()

        self._log_operation(
            batch_id=batch_id,
            operation="CONFIRM",
            old_status=old_status,
            new_status=batch.status,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            details={
                "success_count": success_count,
                "failed_count": total_failed,
                "retryable_count": retryable_count,
                "note": note,
            },
        )
        self.db.commit()
        return {
            "success": True,
            "batch_id": batch_id,
            "total_count": batch.total_count,
            "success_count": success_count,
            "failed_count": total_failed,
            "retryable_count": retryable_count,
            "status": batch.status,
            "results": results,
            "events": all_events,
            "rule_version": settings.rule_version,
        }

    # ---------- 撤销 ----------

    def cancel_batch(
        self,
        batch_id: str,
        actor_id: str,
        actor_role: UserRole,
        actor_name: str,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not has_permission(actor_role, Permission.BULK_IMPORT_CANCEL):
            raise DomainError("PERMISSION_DENIED", f"角色 {actor_role.value} 无撤销权限")

        batch = self.db.query(BulkImportBatch).filter(BulkImportBatch.batch_id == batch_id).first()
        if not batch:
            raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")

        if batch.submitter_id != actor_id:
            if not has_permission(actor_role, Permission.BULK_IMPORT_VIEW_ALL):
                raise DomainError("PERMISSION_DENIED", "无权撤销他人的导入批次")

        if batch.status in (BulkImportBatchStatus.CONFIRMED.value, BulkImportBatchStatus.CANCELLED.value):
            raise DomainError("INVALID_STATUS", f"批次状态 {batch.status} 不允许撤销")

        old_status = batch.status
        batch.status = BulkImportBatchStatus.CANCELLED.value
        batch.cancelled_at = now_utc()
        batch.cancelled_by_id = actor_id
        batch.cancelled_by_name = actor_name
        self.db.flush()

        self._log_operation(
            batch_id=batch_id,
            operation="CANCEL",
            old_status=old_status,
            new_status=BulkImportBatchStatus.CANCELLED.value,
            actor_id=actor_id,
            actor_name=actor_name,
            actor_role=actor_role,
            details={"reason": reason},
        )
        self.db.commit()
        return self._batch_to_dict(batch, include_drafts=True)

    # ---------- 查询 ----------

    def get_batch(
        self,
        batch_id: str,
        actor_id: str,
        actor_role: UserRole,
        include_drafts: bool = True,
    ) -> Dict[str, Any]:
        batch = self.db.query(BulkImportBatch).filter(BulkImportBatch.batch_id == batch_id).first()
        if not batch:
            raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")

        if batch.submitter_id != actor_id:
            if not has_permission(actor_role, Permission.BULK_IMPORT_VIEW_ALL):
                raise DomainError("PERMISSION_DENIED", "无权查看他人的导入批次")

        return self._batch_to_dict(batch, include_drafts=include_drafts)

    def list_batches(
        self,
        actor_id: str,
        actor_role: UserRole,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        q = self.db.query(BulkImportBatch)
        if not has_permission(actor_role, Permission.BULK_IMPORT_VIEW_ALL):
            q = q.filter(BulkImportBatch.submitter_id == actor_id)
        if status:
            status_list = [s.strip() for s in status.split(",") if s.strip()]
            if status_list:
                q = q.filter(BulkImportBatch.status.in_(status_list))
        total = q.count()
        items = q.order_by(BulkImportBatch.created_at.desc()).offset(offset).limit(limit).all()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "rule_version": settings.rule_version,
            "items": [self._batch_to_dict(b, include_drafts=False) for b in items],
        }

    def list_operation_logs(
        self,
        batch_id: str,
        actor_id: str,
        actor_role: UserRole,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        batch = self.db.query(BulkImportBatch).filter(BulkImportBatch.batch_id == batch_id).first()
        if not batch:
            raise DomainError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在")

        if batch.submitter_id != actor_id:
            if not has_permission(actor_role, Permission.BULK_IMPORT_VIEW_ALL):
                raise DomainError("PERMISSION_DENIED", "无权查看他人的导入批次")

        q = self.db.query(BulkImportOperationLog).filter(BulkImportOperationLog.batch_id == batch_id)
        logs = q.order_by(BulkImportOperationLog.created_at.asc()).offset(offset).limit(limit).all()
        return {
            "total": len(logs),
            "limit": limit,
            "offset": offset,
            "rule_version": settings.rule_version,
            "items": [self._log_to_dict(l) for l in logs],
        }

    # ---------- 辅助：序列化 ----------

    def _batch_to_dict(self, batch: BulkImportBatch, include_drafts: bool = False) -> Dict[str, Any]:
        data = {
            "batch_id": batch.batch_id,
            "submitter_id": batch.submitter_id,
            "submitter_name": batch.submitter_name,
            "submitter_role": batch.submitter_role,
            "source_format": batch.source_format,
            "source_filename": batch.source_filename,
            "total_count": batch.total_count,
            "status": batch.status,
            "precheck_passed": batch.precheck_passed,
            "precheck_at": batch.precheck_at.isoformat() if batch.precheck_at else None,
            "confirmed_at": batch.confirmed_at.isoformat() if batch.confirmed_at else None,
            "confirmed_by_id": batch.confirmed_by_id,
            "confirmed_by_name": batch.confirmed_by_name,
            "cancelled_at": batch.cancelled_at.isoformat() if batch.cancelled_at else None,
            "cancelled_by_id": batch.cancelled_by_id,
            "cancelled_by_name": batch.cancelled_by_name,
            "success_count": batch.success_count,
            "failed_count": batch.failed_count,
            "rule_version": settings.rule_version,
            "created_at": batch.created_at.isoformat() if batch.created_at else None,
            "updated_at": batch.updated_at.isoformat() if batch.updated_at else None,
        }
        if include_drafts:
            drafts = self.db.query(BulkImportDraft).filter(
                BulkImportDraft.batch_id == batch.batch_id
            ).order_by(BulkImportDraft.draft_index.asc()).all()
            precheck_summary = {
                "passed": 0,
                "error": 0,
                "warning": 0,
                "pending": 0,
                "created": 0,
                "create_failed": 0,
            }
            draft_dicts = []
            for d in drafts:
                dd = self._draft_to_dict(d)
                draft_dicts.append(dd)
                ps = dd["precheck_status"]
                if ps in precheck_summary:
                    precheck_summary[ps] += 1
                rs = dd["result_status"]
                if rs in ("created",):
                    precheck_summary["created"] += 1
                elif rs in ("create_failed",):
                    precheck_summary["create_failed"] += 1
            data["precheck_summary"] = precheck_summary
            data["drafts"] = draft_dicts
        return data

    def _draft_to_dict(self, draft: BulkImportDraft) -> Dict[str, Any]:
        return {
            "draft_index": draft.draft_index,
            "row_number": draft.row_number,
            "room_id": draft.room_id,
            "owner_id": draft.owner_id,
            "owner_name": draft.owner_name,
            "team_id": draft.team_id,
            "title": draft.title,
            "start_time": draft.start_time.isoformat() if draft.start_time else None,
            "end_time": draft.end_time.isoformat() if draft.end_time else None,
            "attendees": json.loads(draft.attendees) if draft.attendees else [],
            "description": draft.description,
            "precheck_status": draft.precheck_status,
            "precheck_errors": json.loads(draft.precheck_errors) if draft.precheck_errors else [],
            "precheck_warnings": json.loads(draft.precheck_warnings) if draft.precheck_warnings else [],
            "result_status": draft.result_status,
            "result_booking_id": draft.result_booking_id,
            "result_error": json.loads(draft.result_error) if draft.result_error else None,
            "retryable": draft.retryable,
        }

    def _log_to_dict(self, log: BulkImportOperationLog) -> Dict[str, Any]:
        return {
            "log_id": log.log_id,
            "batch_id": log.batch_id,
            "operation": log.operation,
            "old_status": log.old_status,
            "new_status": log.new_status,
            "details": json.loads(log.details) if log.details else None,
            "actor_id": log.actor_id,
            "actor_name": log.actor_name,
            "actor_role": log.actor_role,
            "created_at": log.created_at.isoformat() if log.created_at else None,
            "rule_version": settings.rule_version,
        }
