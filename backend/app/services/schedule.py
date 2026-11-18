"""治理任务调度业务规则：状态流转、字段校验与筛选口径都收在这里。

状态机与依赖唤醒、调度执行批次的完整规则在 ``schedule_dispatch`` 中，
本服务只保留任务清单本身的登记/筛选能力，动作全部委托给调度领域服务，
避免路由层各自实现导致“启动后停草稿、暂停显示运行中”这类状态错位。
"""
from __future__ import annotations

from typing import Any

from app.services.schedule_dispatch import TASK_TRANSITIONS, dispatch_service
from app.store import store

MODULE = "schedule"
REQUIRED_FIELDS = ["任务编号", "任务类型", "调度表达式"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已完成"]


class ScheduleService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = dispatch_service.list_tasks(keyword=keyword, status=status)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return dispatch_service.get_task(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        task_code = str(values.get("任务编号") or "").strip()
        if any(str(row.get("任务编号", "")).strip() == task_code for row in rows):
            return None, [f"任务编号 {task_code} 已存在"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in ["任务编号", "任务类型", "调度表达式", "依赖任务", "重试策略", "超时分钟", "告警渠道"]:
            entry[field] = values.get(field, "")
        entry["status"] = STATUS_ORDER[0]
        entry["任务状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["失败原因"] = ""
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        return dispatch_service.run_task_action(entry_id, action)

    def allowed_actions(self, status: str) -> list[str]:
        """供前端按当前状态渲染可执行动作，避免下发必然被拒的操作。"""
        return [action for action, (sources, _) in TASK_TRANSITIONS.items() if status in sources]
