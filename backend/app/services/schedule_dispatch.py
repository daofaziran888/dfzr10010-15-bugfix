"""调度执行批次领域服务。

把治理任务调度的状态机、依赖唤醒、批次唯一性、版本边界与审计台账集中在这里，
路由层只做参数搬运，其他业务链路（数据目录同步等）也通过这里联动同一版本的批次。

核心约束：
1. 下游任务只能在上游依赖完成后启动；依赖未完成的唤醒请求会被拒绝并保留原因。
2. 同一次依赖窗口（依赖记录）最多只产生一个有效批次，重复唤醒只生效一次。
3. 批次版本号单调递增，状态只能按 待调度→运行中→已完成/失败 推进，禁止回退。
4. 目录同步与其余业务链路联动批次时沿用同一版本，处理结果全部写入调度审计台账。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "schedule"
DEP_MODULE = "schedule_dependency"
BATCH_MODULE = "schedule_batch"
AUDIT_MODULE = "schedule_audit"

# 任务状态机：动作 -> (允许的源状态, 目标状态)。修复原先“启动后停草稿、暂停显示运行中”的错位。
TASK_TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "启动任务": (("草稿", "已暂停"), "运行中"),
    "暂停任务": (("运行中",), "已暂停"),
    "归档任务": (("已暂停", "已完成"), "已完成"),
}
# 批次有效状态：同一窗口内出现这些状态的批次时，不允许再新建第二个有效批次。
BATCH_VALID_STATES = ("待调度", "运行中", "已完成")
BATCH_FLOW = ["待调度", "运行中", "已完成", "失败"]
# 批次状态推进顺序，任何动作都不能把批次推到序号更小的状态。
BATCH_STATE_ORDER = {name: index for index, name in enumerate(BATCH_FLOW)}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _retry_limit(task: dict[str, Any]) -> int:
    try:
        return max(int(str(task.get("重试策略") or "0").strip()), 0)
    except ValueError:
        return 0


def _timeout_minutes(task: dict[str, Any]) -> int:
    try:
        return max(int(str(task.get("超时分钟") or "0").strip()), 0)
    except ValueError:
        return 0


class ScheduleDispatchService:
    """治理任务、依赖记录、执行批次与审计台账的统一读写入口。"""

    # ---- 基础读取：治理任务与依赖数据记录 ---------------------------------
    def list_tasks(self, *, keyword: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, task_id)

    def find_task_by_code(self, task_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("任务编号", "")).strip() == task_code:
                return row
        return None

    def list_dependencies(self, task_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(DEP_MODULE)
        if task_id is not None:
            rows = [row for row in rows if int(row.get("下游任务id", 0)) == task_id]
        return rows

    def list_batches(self, task_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(BATCH_MODULE)
        if task_id is not None:
            rows = [row for row in rows if int(row.get("任务id", 0)) == task_id]
        return rows

    def list_audit(self, batch_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(AUDIT_MODULE)
        if batch_no:
            rows = [row for row in rows if row.get("批次编号") == batch_no]
        return rows

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_MODULE, batch_id)

    def find_batch_by_no(self, batch_no: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if row.get("批次编号") == batch_no:
                return row
        return None

    def active_batch_of_window(self, window_id: int) -> dict[str, Any] | None:
        """返回该依赖窗口当前唯一的有效批次；没有有效批次时返回 None。"""
        for row in store.rows(BATCH_MODULE):
            if int(row.get("依赖窗口id", 0)) == window_id and row.get("批次状态") in BATCH_VALID_STATES:
                return row
        return None

    def latest_batch_of_window(self, window_id: int) -> dict[str, Any] | None:
        """窗口内最近一个批次（含失败批次），用于重试接续与唤醒幂等判断。"""
        rows = [
            row for row in store.rows(BATCH_MODULE)
            if int(row.get("依赖窗口id", 0)) == window_id
        ]
        return max(rows, key=lambda row: int(row.get("批次版本", 0)), default=None)

    def active_batch_of_task(self, task_id: int) -> dict[str, Any] | None:
        """任务维度的唯一有效批次：定位最新依赖窗口，只返回该窗口内的批次。

        旧窗口的批次即使已完成也属于历史执行；目录清单与调度详情因此始终只有
        一个有效下游执行，不会把两个窗口的批次同时展示为当前执行。
        """
        window = self._latest_window(task_id)
        if window is None:
            return None
        window_batches = [
            row for row in store.rows(BATCH_MODULE)
            if int(row.get("依赖窗口id", 0)) == int(window["id"])
        ]
        if not window_batches:
            return None
        valid = [row for row in window_batches if row.get("批次状态") in BATCH_VALID_STATES]
        candidates = valid or window_batches
        return max(candidates, key=lambda row: int(row.get("批次版本", 0)))

    # ---- 审计台账 -----------------------------------------------------------
    def _audit(self, batch: dict[str, Any], event: str, result: str, detail: str, *, source: str = "调度服务") -> dict[str, Any]:
        entry = {
            "id": _next_id(store.rows(AUDIT_MODULE)),
            "批次编号": batch["批次编号"],
            "批次版本": batch["批次版本"],
            "任务编号": batch["任务编号"],
            "来源模块": source,
            "事件": event,
            "处理结果": result,
            "明细": detail,
            "记录时间": _now(),
        }
        store.rows(AUDIT_MODULE).append(entry)
        return entry

    # ---- 任务状态机 ---------------------------------------------------------
    def run_task_action(self, task_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        task = self.get_task(task_id)
        if task is None:
            return None, f"治理任务 {task_id} 不存在或已归档"
        rule = TASK_TRANSITIONS.get(action)
        if rule is None:
            return None, f"动作「{action}」不属于治理任务调度可执行范围"
        allowed_sources, target = rule
        current = str(task.get("status") or "")
        if current not in allowed_sources:
            return None, f"任务当前为{current}，不允许执行{action}（仅 {'/'.join(allowed_sources)} 状态可执行）"
        task["status"] = target
        task["任务状态"] = target
        task["pending"] = target != "已完成"
        task["abnormal"] = False
        # 暂停时若有运行中的批次，批次随任务一起挂起并留痕，恢复时复用原运行而不是新建批次。
        if action == "暂停任务":
            active = self.active_batch_of_task(task_id)
            if active is not None and active["批次状态"] == "运行中":
                self._advance_batch(active, "待调度", "任务暂停，批次挂起等待恢复", reason="", allow_suspend=True)
        if action == "启动任务":
            task["失败原因"] = ""
            active = self.active_batch_of_task(task_id)
            if active is not None and active["批次状态"] == "待调度":
                self._advance_batch(active, "运行中", "任务恢复运行，复用原批次接续执行", reason="")
        if action == "归档任务":
            # 归档收口未完成批次，避免任务已完成而批次仍显示为有效执行。
            active = self.active_batch_of_task(task_id)
            if active is not None and active["批次状态"] != "已完成":
                self._advance_batch(active, "已完成", "任务归档，批次随任务收口", reason="")
        return task, f"治理任务已{action}，当前状态：{target}"

    # ---- 上游完成事件与依赖唤醒 ---------------------------------------------
    def rerun_upstream(self, upstream_task_id: int) -> tuple[dict[str, Any] | None, str]:
        """上游重跑：任务回到运行中重新执行；重跑完成后由完成事件为下游打开新窗口。

        旧窗口与旧批次全部保留为历史记录，新窗口只会生成一个新的有效下游批次，
        目录清单因此始终只有一个当前有效下游执行。
        """
        upstream = self.get_task(upstream_task_id)
        if upstream is None:
            return None, f"上游治理任务 {upstream_task_id} 不存在或已归档"
        # 关闭该上游尚未关闭的旧依赖窗口，保证重跑完成事件一定打开新窗口。
        upstream_code = str(upstream["任务编号"])
        for window in store.rows(DEP_MODULE):
            if str(window.get("上游任务编号", "")) == upstream_code and not window.get("窗口关闭"):
                self._close_window(int(window["id"]), f"上游 {upstream_code} 重跑，旧窗口关闭")
        upstream["status"] = "运行中"
        upstream["任务状态"] = "运行中"
        upstream["pending"] = True
        upstream["失败原因"] = ""
        return upstream, f"上游 {upstream_code} 已重跑，重新执行完成后将为下游打开新依赖窗口"

    def mark_upstream_completed(self, upstream_task_id: int) -> tuple[dict[str, Any] | None, str]:
        """上游依赖完成事件：登记依赖完成记录并唤醒所有等待该上游的下游任务。

        重复提交完成事件时，同一上游只登记一次完成顺序；下游唤醒走窗口幂等逻辑，
        因此“上游完成事件”和“下游超时重试”同时到达也只会留下一个有效批次。
        """
        upstream = self.get_task(upstream_task_id)
        if upstream is None:
            return None, f"上游治理任务 {upstream_task_id} 不存在或已归档"
        # 完成事件先收口该任务自身的运行中批次，保证任务状态与批次状态一致、窗口正常关闭。
        active = self.active_batch_of_task(upstream_task_id)
        if active is not None and active.get("批次状态") == "运行中":
            self._advance_batch(active, "已完成", f"上游 {upstream.get('任务编号')} 完成，批次收口", reason="")
        if upstream.get("status") != "已完成":
            upstream["status"] = "已完成"
            upstream["任务状态"] = "已完成"
            upstream["pending"] = False
        upstream_code = str(upstream["任务编号"])
        downstream_rows = [
            row for row in store.rows(MODULE)
            if str(row.get("依赖任务") or "").strip() == upstream_code
            and int(row.get("id", 0)) != upstream_task_id
        ]
        messages = [f"上游 {upstream_code} 完成事件已登记"]
        for downstream in downstream_rows:
            _, message = self.wake_downstream(int(downstream["id"]), trigger="上游依赖完成事件")
            messages.append(message)
        return upstream, "；".join(messages)

    def wake_downstream(self, downstream_task_id: int, *, trigger: str = "手动唤醒") -> tuple[dict[str, Any] | None, str]:
        """唤醒下游：校验依赖、定位/新建依赖窗口，并在窗口内保证唯一有效批次。"""
        task = self.get_task(downstream_task_id)
        if task is None:
            return None, f"下游治理任务 {downstream_task_id} 不存在或已归档"
        upstream_code = str(task.get("依赖任务") or "").strip()
        if not upstream_code:
            return None, f"任务 {task['任务编号']} 没有配置上游依赖，无需依赖唤醒"
        upstream = self.find_task_by_code(upstream_code)
        if upstream is None:
            reason = f"上游依赖 {upstream_code} 在治理任务清单中不存在"
            task["失败原因"] = reason
            return None, reason
        # 下游只能在上游依赖完成后启动。
        if upstream.get("status") != "已完成":
            reason = f"上游依赖 {upstream_code} 尚未完成（当前 {upstream.get('status')}），下游 {task['任务编号']} 禁止唤醒"
            task["失败原因"] = reason
            self._audit_placeholder(task, "依赖唤醒拦截", "拒绝", reason, trigger)
            return None, reason

        window = self._open_or_get_window(upstream, task, trigger)
        existing = self.active_batch_of_window(int(window["id"]))
        if existing is not None:
            # 同一依赖窗口的重复唤醒（含超时重试撞上游完成事件）只生效一次。
            return existing, (
                f"依赖窗口 {window['窗口编号']} 已存在有效批次 {existing['批次编号']}"
                f"（{existing['批次状态']}），重复唤醒不重复执行"
            )
        latest = self.latest_batch_of_window(int(window["id"]))
        if latest is not None and latest.get("批次状态") == "失败":
            # 窗口仍开着说明重试次数未耗尽：失败批次只能由超时重试接续，唤醒不另建批次。
            return latest, (
                f"依赖窗口 {window['窗口编号']} 的批次 {latest['批次编号']} 处于失败态，"
                "请通过超时重试接续失败步骤，唤醒不新建批次"
            )
        batch = self._create_batch(task, window, trigger)
        return batch, f"依赖窗口 {window['窗口编号']} 唤醒成功，生成调度执行批次 {batch['批次编号']}"

    def timeout_retry(self, task_id: int) -> tuple[dict[str, Any] | None, str]:
        """下游超时重试：有有效批次就复用原运行接续失败步骤，重试耗尽才允许新建批次。"""
        task = self.get_task(task_id)
        if task is None:
            return None, f"治理任务 {task_id} 不存在或已归档"
        limit = _retry_limit(task)
        window = self._latest_window(task_id)
        if window is None:
            return None, f"任务 {task['任务编号']} 还没有依赖窗口，请先由上游完成事件唤醒"
        active = self.active_batch_of_window(int(window["id"])) or self.latest_batch_of_window(int(window["id"]))
        if active is not None:
            used = int(active.get("已重试次数", 0))
            if used >= limit:
                reason = f"批次 {active['批次编号']} 已重试 {used} 次，达到重试策略上限 {limit} 次，不再重复执行"
                self._audit(active, "超时重试拦截", "拒绝", reason)
                task["失败原因"] = reason
                if active.get("批次状态") == "失败" and not window.get("窗口关闭"):
                    self._close_window(int(window["id"]), "重试次数耗尽，窗口关闭，需上游重跑完成后才能新建批次")
                return None, reason
            active["已重试次数"] = used + 1
            self._audit(
                active,
                "超时重试",
                "接续",
                f"复用原批次接续失败步骤，第 {used + 1}/{limit} 次重试，超时 {_timeout_minutes(task)} 分钟",
            )
            if active.get("批次状态") == "失败":
                # 仅超时重试允许把失败批次接续回运行中，版本号不变；外部接口仍禁止回退。
                self._advance_batch(active, "运行中", "超时重试接续失败步骤", reason="", allow_fail_resume=True)
            elif active.get("批次状态") != "运行中":
                self._advance_batch(active, "运行中", "超时重试后接续运行", reason="")
            return active, f"批次 {active['批次编号']} 复用原运行接续执行（第 {used + 1}/{limit} 次重试）"
        # 上一批次已结束且窗口已关闭：上游重跑完成会打开新窗口，新窗口内才允许新建批次。
        batch = self._create_batch(task, window, "超时重试新建批次")
        return batch, f"原批次已结束，新建调度执行批次 {batch['批次编号']}"

    # ---- 批次推进与失败留痕 -------------------------------------------------
    def advance_batch(self, batch_id: int, target_state: str, *, reason: str = "") -> tuple[dict[str, Any] | None, str]:
        batch = self.get_batch(batch_id)
        if batch is None:
            return None, f"调度批次 {batch_id} 不存在"
        # 外部推进只允许运行中/已完成；失败只能走 fail_batch 并强制保留原因。
        if target_state not in ("待调度", "运行中", "已完成"):
            return None, "批次推进仅支持 运行中、已完成；登记失败请使用失败接口并填写原因"
        result, message = self._advance_batch(batch, target_state, f"手动推进至{target_state}", reason=reason)
        if result is None:
            return None, message
        return result, message

    def fail_batch(self, batch_id: int, reason: str) -> tuple[dict[str, Any] | None, str]:
        batch = self.get_batch(batch_id)
        if batch is None:
            return None, f"调度批次 {batch_id} 不存在"
        if not reason.strip():
            return None, "失败批次必须保留失败原因"
        result, message = self._advance_batch(batch, "失败", "批次执行失败", reason=reason.strip())
        if result is None:
            return None, message
        task = self.find_task_by_code(str(batch["任务编号"]))
        if task is not None:
            task["失败原因"] = reason.strip()
            task["abnormal"] = True
        return result, message

    def _advance_batch(
        self,
        batch: dict[str, Any],
        target_state: str,
        detail: str,
        *,
        reason: str,
        source: str = "调度服务",
        allow_fail_resume: bool = False,
        allow_suspend: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        current = str(batch.get("批次状态") or "")
        if target_state not in BATCH_STATE_ORDER:
            return None, f"目标状态「{target_state}」非法"
        is_fail_resume = allow_fail_resume and current == "失败" and target_state == "运行中"
        is_suspend = allow_suspend and current == "运行中" and target_state == "待调度"
        # 已发布（已完成）版本是终态：任何模块都不得再改动批次状态，禁止回退也禁止再置失败。
        if current == "已完成":
            return None, f"批次 {batch['批次编号']} 已发布（已完成），版本锁定，禁止再变更状态"
        if BATCH_STATE_ORDER[target_state] < BATCH_STATE_ORDER[current] and not (is_fail_resume or is_suspend):
            return None, f"批次 {batch['批次编号']} 当前 {current}，禁止回退到 {target_state}"
        if target_state == current:
            return batch, f"批次 {batch['批次编号']} 已是 {current}，状态未重复推进"
        batch["批次状态"] = target_state
        batch["更新时间"] = _now()
        if reason:
            batch["失败原因"] = reason
        if target_state == "已完成":
            batch["失败原因"] = ""
            batch["发布时间"] = _now()
            self._close_window(int(batch["依赖窗口id"]), f"批次 {batch['批次编号']} 已完成")
            task = self.find_task_by_code(str(batch["任务编号"]))
            if task is not None:
                task["status"] = "已完成"
                task["任务状态"] = "已完成"
                task["pending"] = False
                task["失败原因"] = ""
        elif target_state == "失败":
            # 失败不关窗：重试次数未耗尽时，超时重试在同一窗口复用原批次接续。
            task = self.find_task_by_code(str(batch["任务编号"]))
            if task is not None:
                task["abnormal"] = True
        result = "成功" if target_state == "已完成" else ("失败" if target_state == "失败" else "接续")
        self._audit(batch, f"批次推进：{current}→{target_state}", result, reason or detail, source=source)
        return batch, f"批次 {batch['批次编号']} 已推进至 {target_state}"

    def _close_window(self, window_id: int, note: str) -> None:
        for window in store.rows(DEP_MODULE):
            if int(window.get("id", 0)) == window_id:
                window["窗口关闭"] = True
                window["窗口状态"] = f"已关闭（{note}）"
                window["关闭时间"] = _now()
                return

    # ---- 业务链路联动（目录同步及其他模块） ---------------------------------
    def link_module_result(
        self,
        *,
        module: str,
        entry_id: int,
        batch_no: str,
        event: str,
        result: str,
        detail: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """其余业务链路按调度执行批次联动处理，并把结果同步到调度审计台账。

        同步模块先兼容历史记录（无批次版本的老作业直接放行），再核对批次版本边界：
        只允许联动有效批次，且已发布版本禁止回退。
        """
        module_rows = store.rows(module) if module in store.module_names() else None
        if module_rows is None:
            return None, f"业务模块 {module} 不存在，无法联动调度执行批次"
        entry = next((row for row in module_rows if int(row.get("id", 0)) == entry_id), None)
        if entry is None:
            return None, f"{module} 记录 {entry_id} 不存在"
        # 历史记录兼容：没有批次版本的老作业不做版本边界校验。
        if not str(entry.get("调度批次版本") or "").strip() and not batch_no:
            entry.setdefault("调度批次版本", "")
            return entry, f"{module} 历史记录 {entry_id} 无调度批次，按历史口径兼容处理"
        batch = self.find_batch_by_no(batch_no) if batch_no else None
        if batch is None:
            return None, f"调度执行批次 {batch_no or '（空）'} 不存在，禁止联动"
        if batch["批次状态"] not in BATCH_VALID_STATES:
            return None, f"批次 {batch_no} 已{batch['批次状态']}，不是有效批次，联动请求拒绝"
        current_version = str(entry.get("调度批次版本") or "").strip()
        if current_version and current_version != str(batch["批次版本"]):
            return None, (
                f"批次版本边界不一致：目录清单记录 v{current_version}，请求联动 v{batch['批次版本']}，禁止跨版本处理"
            )
        self._audit(batch, event, result, detail, source=module)
        return batch, f"{module} 已按批次 {batch_no}（v{batch['批次版本']}）联动并写入审计台账"

    # ---- 内部：依赖窗口与批次生成 -------------------------------------------
    def _open_or_get_window(
        self,
        upstream: dict[str, Any],
        downstream: dict[str, Any],
        trigger: str,
    ) -> dict[str, Any]:
        """同一对上下游在下游存在有效窗口时复用窗口；上游重跑后打开新窗口。"""
        rows = store.rows(DEP_MODULE)
        for row in reversed(rows):
            if (
                int(row.get("上游任务id", 0)) == int(upstream["id"])
                and int(row.get("下游任务id", 0)) == int(downstream["id"])
                and not row.get("窗口关闭")
            ):
                return row
        completed_count = sum(
            1 for row in rows
            if int(row.get("上游任务id", 0)) == int(upstream["id"])
            and int(row.get("下游任务id", 0)) == int(downstream["id"])
        )
        window = {
            "id": _next_id(rows),
            "窗口编号": f"WIN-{upstream['任务编号']}-{completed_count + 1:02d}",
            "上游任务id": int(upstream["id"]),
            "上游任务编号": upstream["任务编号"],
            "下游任务id": int(downstream["id"]),
            "下游任务编号": downstream["任务编号"],
            "触发来源": trigger,
            "依赖完成顺序": f"1.{upstream['任务编号']}@{_now()}",
            "窗口状态": "依赖已完成",
            "窗口关闭": False,
            "创建时间": _now(),
        }
        rows.append(window)
        return window

    def _latest_window(self, downstream_task_id: int) -> dict[str, Any] | None:
        rows = [row for row in store.rows(DEP_MODULE) if int(row.get("下游任务id", 0)) == downstream_task_id]
        return rows[-1] if rows else None

    def _create_batch(self, task: dict[str, Any], window: dict[str, Any], trigger: str) -> dict[str, Any]:
        rows = store.rows(BATCH_MODULE)
        version = max((int(row.get("批次版本", 0)) for row in rows), default=0) + 1
        batch = {
            "id": _next_id(rows),
            "批次编号": f"BATCH-{task['任务编号']}-{version:03d}",
            "批次版本": version,
            "任务id": int(task["id"]),
            "任务编号": task["任务编号"],
            "依赖窗口id": int(window["id"]),
            "依赖窗口编号": window["窗口编号"],
            "依赖完成顺序": window["依赖完成顺序"],
            "触发来源": trigger,
            "批次状态": "待调度",
            "已重试次数": 0,
            "重试上限": _retry_limit(task),
            "超时分钟": _timeout_minutes(task),
            "失败原因": "",
            "创建时间": _now(),
            "更新时间": _now(),
            "发布时间": "",
        }
        rows.append(batch)
        self._audit(batch, "生成调度执行批次", "成功", f"由{trigger}生成，超时 {batch['超时分钟']} 分钟，重试上限 {batch['重试上限']} 次")
        # 任务处于可运行状态时批次立即进入运行中；暂停态保留待调度，恢复时复用。
        if task.get("status") == "运行中":
            self._advance_batch(batch, "运行中", "任务运行中，批次自动启动", reason="")
        return batch

    def _audit_placeholder(self, task: dict[str, Any], event: str, result: str, detail: str, trigger: str) -> None:
        entry = {
            "id": _next_id(store.rows(AUDIT_MODULE)),
            "批次编号": "",
            "批次版本": "",
            "任务编号": task["任务编号"],
            "来源模块": "调度服务",
            "事件": event,
            "处理结果": result,
            "明细": f"{detail}（触发来源：{trigger}）",
            "记录时间": _now(),
        }
        store.rows(AUDIT_MODULE).append(entry)


dispatch_service = ScheduleDispatchService()
