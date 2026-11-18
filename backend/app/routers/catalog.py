"""数据目录同步接口：维护同步作业，并按调度执行批次版本执行同步与回写审计台账。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.catalog import CatalogService

router = APIRouter(prefix="/api/catalog", tags=["数据目录同步"])

service = CatalogService()

LIST_FIELDS = ["作业编号", "源路径", "目标目录", "同步模式", "调度批次版本", "依赖完成顺序", "同步频率", "作业状态"]
STATUSES = ["草稿", "同步中", "已完成", "已暂停"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据目录同步清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "catalog", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按作业编号检索"),
    status: str | None = Query(default=None, description="草稿、同步中、已完成、已暂停"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按作业编号与状态过滤数据目录同步列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条同步作业明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"同步作业 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条同步作业，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="同步作业已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条同步作业执行启动同步、暂停同步、归档作业；状态错位会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/sync", response_model=ActionResult)
def sync_with_batch(entry_id: int, payload: EntryPayload) -> ActionResult:
    """沿用同一版本调度执行批次启动目录同步，历史记录先兼容再核对版本边界。"""
    batch_no = str(payload.values.get("批次编号") or "").strip()
    entry, message = service.sync_with_batch(entry_id, batch_no)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/complete", response_model=ActionResult)
def complete_sync(entry_id: int, payload: EntryPayload) -> ActionResult:
    """完成目录同步并把处理结果同步到调度审计台账，禁止回退已发布版本。"""
    batch_no = str(payload.values.get("批次编号") or "").strip()
    entry, message = service.complete_sync(entry_id, batch_no)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
