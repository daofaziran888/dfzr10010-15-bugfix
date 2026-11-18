"""治理任务调度接口：维护治理任务，并暴露依赖窗口、调度执行批次与审计台账。

接口分组：
- 任务清单与动作：启动/暂停/归档，状态机由服务层保证不错位。
- 依赖与唤醒：上游完成事件、下游依赖唤醒（幂等）、超时重试。
- 调度执行批次：查询、状态推进、失败留痕。
- 调度审计台账：按批次版本核对所有联动结果。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.schedule import ScheduleService
from app.services.schedule_dispatch import dispatch_service

router = APIRouter(prefix="/api/schedule", tags=["治理任务调度"])

service = ScheduleService()

LIST_FIELDS = ["任务编号", "任务类型", "调度表达式", "依赖任务", "重试策略", "超时分钟", "告警渠道", "任务状态"]
STATUSES = ["草稿", "运行中", "已暂停", "已完成"]


@router.get("/dependencies", response_model=PageResult[dict])
def list_dependencies(
    task_id: int | None = Query(default=None, description="按下游任务 id 过滤依赖记录"),
) -> PageResult[dict]:
    """读取治理任务依赖数据记录：依赖窗口、依赖完成顺序与窗口状态。"""
    items = dispatch_service.list_dependencies(task_id)
    return PageResult(items=items, total=len(items), page=1, size=max(len(items), 1))


@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    task_id: int | None = Query(default=None, description="按治理任务 id 过滤调度执行批次"),
) -> PageResult[dict]:
    """读取调度执行批次：同一依赖窗口只保留一个有效批次，列表按版本倒序。"""
    items = sorted(dispatch_service.list_batches(task_id), key=lambda row: int(row["批次版本"]), reverse=True)
    return PageResult(items=items, total=len(items), page=1, size=max(len(items), 1))


@router.get("/audit", response_model=PageResult[dict])
def list_audit(
    batch_no: str | None = Query(default=None, description="按批次编号过滤审计台账"),
) -> PageResult[dict]:
    """读取调度审计台账：批次推进与各业务链路联动结果都挂同一批次版本。"""
    items = dispatch_service.list_audit(batch_no)
    return PageResult(items=items, total=len(items), page=1, size=max(len(items), 1))


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出治理任务调度清单：返回当前全量任务及各自唯一有效批次。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "schedule", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已完成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤治理任务调度列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict[str, Any]:
    """读取单条治理任务明细，含依赖记录、唯一有效批次与审计台账。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"治理任务 {entry_id} 不存在或已归档")
    active_batch = dispatch_service.active_batch_of_task(entry_id)
    return {
        "task": entry,
        "dependencies": dispatch_service.list_dependencies(entry_id),
        "batches": sorted(
            dispatch_service.list_batches(entry_id), key=lambda row: int(row["批次版本"]), reverse=True
        ),
        "active_batch": active_batch,
        "audit": dispatch_service.list_audit(active_batch["批次编号"] if active_batch else None),
        "allowed_actions": service.allowed_actions(str(entry.get("status") or "")),
    }


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条治理任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"字段校验未通过：{'、'.join(missing)}")
    return ActionResult(ok=True, message="治理任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条治理任务执行启动任务、暂停任务、归档任务；状态错位会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/rerun", response_model=ActionResult)
def rerun_upstream(entry_id: int) -> ActionResult:
    """上游重跑：回到运行中重新执行，重跑完成后下游在新依赖窗口内新建批次。"""
    entry, message = dispatch_service.rerun_upstream(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/upstream-complete", response_model=ActionResult)
def mark_upstream_completed(entry_id: int) -> ActionResult:
    """登记上游依赖完成事件并唤醒下游；重复提交只生成一个有效下游批次。"""
    entry, message = dispatch_service.mark_upstream_completed(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/wake", response_model=ActionResult)
def wake_downstream(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """依赖唤醒下游：上游未完成会拒绝并保留原因；重复唤醒命中同一窗口只生效一次。"""
    trigger = "手动唤醒"
    if payload is not None:
        trigger = str(payload.values.get("trigger") or trigger).strip() or trigger
    entry, message = dispatch_service.wake_downstream(entry_id, trigger=trigger)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/timeout-retry", response_model=ActionResult)
def timeout_retry(entry_id: int) -> ActionResult:
    """下游超时重试：复用原批次接续失败步骤，达到重试策略上限后拒绝重复执行。"""
    entry, message = dispatch_service.timeout_retry(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/linkage/audit", response_model=ActionResult)
def link_module_result(payload: EntryPayload) -> ActionResult:
    """其余业务链路按调度执行批次联动处理，并把处理结果同步到调度审计台账。

    入参 values：module（业务模块表名）、entry_id、批次编号、事件、处理结果、明细。
    历史记录（无批次版本）先兼容；有效批次与版本边界由调度服务统一核对。
    """
    values = payload.values
    module = str(values.get("module") or "").strip()
    if not module:
        return ActionResult(ok=False, message="缺少 module，无法定位业务链路数据")
    try:
        entry_id = int(values.get("entry_id"))
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="entry_id 必须是整数")
    batch_no = str(values.get("批次编号") or "").strip()
    event = str(values.get("事件") or "业务链路联动").strip()
    result = str(values.get("处理结果") or "成功").strip()
    detail = str(values.get("明细") or "").strip()
    entry, message = dispatch_service.link_module_result(
        module=module,
        entry_id=entry_id,
        batch_no=batch_no,
        event=event,
        result=result,
        detail=detail,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/batches/{batch_id}/advance", response_model=ActionResult)
def advance_batch(batch_id: int, payload: EntryPayload) -> ActionResult:
    """按确认顺序推进调度执行批次状态，禁止回退已发布版本。"""
    target_state = str(payload.values.get("target_state") or "").strip()
    reason = str(payload.values.get("reason") or "").strip()
    entry, message = dispatch_service.advance_batch(batch_id, target_state, reason=reason)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/batches/{batch_id}/fail", response_model=ActionResult)
def fail_batch(batch_id: int, payload: EntryPayload) -> ActionResult:
    """登记批次失败并保留失败原因，失败任务在任务清单中可核对原因。"""
    reason = str(payload.values.get("reason") or "").strip()
    entry, message = dispatch_service.fail_batch(batch_id, reason)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
