"""数据目录同步业务规则：状态流转、字段校验与调度执行批次版本边界。

目录同步沿用调度服务生成的同一版本调度执行批次，并在目录清单记录依赖完成顺序；
同步结果回写调度审计台账。历史记录（没有批次版本的老作业）先兼容、不做版本拦截。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.schedule_dispatch import BATCH_VALID_STATES, dispatch_service
from app.store import store

MODULE = "catalog"
REQUIRED_FIELDS = ["作业编号", "源路径", "目标目录"]
# 修复与调度模块相同的状态错位：启动应进入同步中，暂停应进入已暂停。
TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "启动同步": (("草稿", "已暂停"), "同步中"),
    "暂停同步": (("同步中",), "已暂停"),
    "归档作业": (("已暂停", "已完成"), "已完成"),
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class CatalogService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("作业编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = "草稿"
        entry["作业状态"] = "草稿"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["调度批次版本"] = ""
        entry["依赖完成顺序"] = ""
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同步作业 {entry_id} 不存在或已归档"
        rule = TRANSITIONS.get(action)
        if rule is None:
            return None, f"动作「{action}」不属于数据目录同步可执行范围"
        allowed_sources, target = rule
        current = str(entry.get("status") or "")
        if current not in allowed_sources:
            return None, f"作业当前为{current}，不允许执行{action}（仅 {'/'.join(allowed_sources)} 状态可执行）"
        entry["status"] = target
        entry["作业状态"] = target
        entry["pending"] = target != "已完成"
        return entry, f"同步作业已{action}，当前状态：{target}"

    def sync_with_batch(self, entry_id: int, batch_no: str) -> tuple[dict[str, Any] | None, str]:
        """按调度执行批次执行目录同步。

        - 历史记录（无批次版本）：兼容放行，不强制批次。
        - 新作业：必须绑定有效批次，版本边界一致才允许同步，已发布版本禁止回退。
        - 同步完成后目录清单记录依赖完成顺序，结果写入调度审计台账。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同步作业 {entry_id} 不存在或已归档"
        # 先兼容历史记录。
        if not str(entry.get("调度批次版本") or "").strip() and not batch_no:
            entry["status"] = "同步中"
            entry["作业状态"] = "同步中"
            return entry, f"历史作业 {entry['作业编号']} 无调度批次，按历史口径兼容启动同步"

        batch = dispatch_service.find_batch_by_no(batch_no)
        if batch is None:
            return None, f"调度执行批次 {batch_no} 不存在，目录同步拒绝启动"
        if batch["批次状态"] not in BATCH_VALID_STATES:
            return None, f"批次 {batch_no} 已{batch['批次状态']}，不是有效批次，目录同步拒绝启动"
        current_version = str(entry.get("调度批次版本") or "").strip()
        if current_version:
            if current_version != str(batch["批次版本"]):
                return None, (
                    f"批次版本边界不一致：目录清单为 v{current_version}，请求同步 v{batch['批次版本']}，"
                    "禁止跨版本/回退同步"
                )
        else:
            entry["调度批次版本"] = str(batch["批次版本"])
        entry["依赖完成顺序"] = batch.get("依赖完成顺序", "")
        entry["status"] = "同步中"
        entry["作业状态"] = "同步中"
        entry["pending"] = True
        dispatch_service.link_module_result(
            module=MODULE,
            entry_id=entry_id,
            batch_no=batch_no,
            event="数据目录同步启动",
            result="接续",
            detail=f"作业 {entry['作业编号']} 沿用批次 {batch_no}（v{batch['批次版本']}）启动同步",
        )
        return entry, f"目录同步已沿用批次 {batch_no}（v{batch['批次版本']}）启动"

    def complete_sync(self, entry_id: int, batch_no: str) -> tuple[dict[str, Any] | None, str]:
        """完成目录同步并把结果同步到调度审计台账；禁止回退已发布批次版本。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"同步作业 {entry_id} 不存在或已归档"
        batch = dispatch_service.find_batch_by_no(batch_no)
        if batch is None:
            return None, f"调度执行批次 {batch_no} 不存在"
        current_version = str(entry.get("调度批次版本") or "").strip()
        if current_version and current_version != str(batch["批次版本"]):
            return None, f"版本边界不一致：目录清单 v{current_version} 与批次 v{batch['批次版本']} 不匹配，禁止回退"
        entry["status"] = "已完成"
        entry["作业状态"] = "已完成"
        entry["pending"] = False
        entry["完成时间"] = _now()
        dispatch_service.link_module_result(
            module=MODULE,
            entry_id=entry_id,
            batch_no=batch_no,
            event="数据目录同步完成",
            result="成功",
            detail=f"作业 {entry['作业编号']} 完成同步，依赖完成顺序：{entry.get('依赖完成顺序') or '无'}",
        )
        return entry, f"目录同步完成，结果已按批次 {batch_no} 写入调度审计台账"
