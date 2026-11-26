"""人群圈选器领域服务：管理实验分析人群包的圈选条件、规模计算与刷新周期。

数据链路位置：人群供给层，消费分组分配器与曝光日志的用户数据，按圈选条件
生成人群包，供切片分析与指标计算限定分析范围。

关键规则：
- 没有条件或条件无法解析一律拒绝生成，不产生可领取样本包；
- 条件合法但无人命中属于「零规模」，保留可查询、可领取（等待补采）的人群包；
- 成员选择严格按实验编码隔离，不同实验的同名分组不会合并；
- 已剔除成员不会因为条件相符重新进入人群；
- 每次生成/刷新沉淀一个人群版本与成员快照，切片领取后可复查实际采用的来源。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.cohort_logic import (
    ParsedCondition,
    describe_scope,
    group_stats,
    member_summary,
    parse_condition,
    select_members,
)
from app.store import store


class CohortBuilderService(BaseModuleService):
    MODULE = "cohort_builder"
    ENTITY = "人群包"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "人群编码",
        "人群名称",
        "圈选条件",
        "条件状态",
        "校验说明",
        "人群规模",
        "圈选范围",
        "确认版本",
        "可领取",
        "刷新周期",
    ]
    # 人群规模、条件状态等由系统生成，登记时只收集业务输入
    CREATE_FIELDS = ["人群编码", "人群名称", "圈选条件", "刷新周期"]
    REQUIRED_FIELDS = ["人群编码", "人群名称", "圈选条件"]
    STATUS_ORDER = ["草稿", "已生成", "已过期"]
    # 动作在 run_action 里分派到领域方法，状态由领域方法负责流转
    ACTION_RULES = {
        "生成人群": "已生成",
        "刷新人群": "已生成",
        "标记过期": "已过期",
        "补采成员": "已生成",
        "发起切片": "已生成",
    }
    NEGATIVE_ACTIONS = []

    # ---- 登记入口：先做输入校验，拒绝条件缺失/无法解析的请求 ----

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing

        parsed = parse_condition(values.get("圈选条件"))
        if not parsed.ok:
            # 与必填字段缺失区分：这是条件内容本身的问题，页面可修正后重交
            raise ValueError(parsed.reason)

        rows = store.rows(self.MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        }
        for field in self.CREATE_FIELDS:
            entry[field] = values.get(field, "")
        entry.update({
            "条件状态": "待生成",
            "校验说明": "条件已通过校验，尚未生成人群",
            "人群规模": "0",
            "圈选范围": describe_scope(parsed.scope or {}),
            "确认版本": "",
            "可领取": "否",
            "人群版本历史": [],
            "status": "草稿",
            "pending": True,
            "abnormal": False,
        })
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在或已归档", "entry": None}
        if action == "生成人群":
            result = self.generate_cohort(entry_id)
            return {"ok": result["ok"], "message": result["message"], "entry": entry}
        if action == "刷新人群":
            result = self.refresh_cohort(entry_id)
            return {"ok": result["ok"], "message": result["message"], "entry": entry}
        if action == "补采成员":
            result = self.backfill_members(entry_id)
            return {"ok": result["ok"], "message": result["message"], "entry": entry}
        if action == "标记过期":
            result = self.expire_cohort(entry_id)
            return {"ok": result["ok"], "message": result["message"], "entry": entry}
        if action == "发起切片":
            result = self.produce_to_segment_slice(entry_id)
            return {"ok": result["ok"], "message": result["message"], "entry": entry}
        return {
            "ok": False,
            "message": f"动作「{action}」不属于{self.MODULE}可执行范围",
            "entry": None,
        }

    # ---- 业务扩展：人群圈选与规模计算 ----

    def _snapshot(self, entry: dict[str, Any], parsed: ParsedCondition, trigger: str) -> dict[str, Any]:
        """按当前分组明细选择成员并沉淀一个不可变的人群版本快照。"""
        members = select_members(parsed.scope or {})
        version_no = len(entry.get("人群版本历史", [])) + 1
        version = f"V{version_no}"
        snapshot = {
            "确认版本": version,
            "生成时间": datetime.now(timezone.utc).isoformat(),
            "触发方式": trigger,
            "圈选范围": describe_scope(parsed.scope or {}),
            "圈选条件": parsed.raw,
            "人群规模": len(members),
            "成员来源": member_summary(members),
            "分组统计": group_stats(members),
        }
        history = list(entry.get("人群版本历史", []))
        history.append(snapshot)
        entry["人群版本历史"] = history
        entry["确认版本"] = version
        entry["圈选范围"] = snapshot["圈选范围"]
        entry["人群规模"] = str(len(members))
        entry["生成时间"] = snapshot["生成时间"]
        # 条件合法：无论是否命中都生成可领取样本包（零规模等待补采）
        entry["条件状态"] = "条件有效"
        entry["校验说明"] = "条件解析成功" if members else "条件解析成功，当前范围暂无成员（零规模，可等待补采）"
        entry["可领取"] = "是"
        return snapshot

    def generate_cohort(self, entry_id: int) -> dict[str, Any]:
        """按圈选条件从分组分配结果生成人群包并计算规模。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "草稿":
            return {"ok": False, "message": "仅草稿人群包可生成"}
        parsed = parse_condition(entry.get("圈选条件", ""))
        if not parsed.ok:
            # 无效条件拒绝生成：保持草稿、不可领取，说明缺失还是无法解析
            entry["条件状态"] = "条件无效"
            entry["校验说明"] = parsed.reason
            entry["可领取"] = "否"
            entry["abnormal"] = True
            return {"ok": False, "message": f"已拒绝生成：{parsed.reason}（可修正条件后重新提交）"}

        snapshot = self._snapshot(entry, parsed, trigger="生成人群")
        entry["status"] = "已生成"
        entry["pending"] = False
        entry["abnormal"] = False
        size = snapshot["人群规模"]
        tail = "当前范围暂无成员，已保留零规模人群包，补采后可刷新" if size == 0 else f"命中 {size} 个用户"
        return {"ok": True, "message": f"人群包已生成（{snapshot['确认版本']}），{tail}"}

    def expire_cohort(self, entry_id: int) -> dict[str, Any]:
        """标记人群包过期并通知切片分析器停止引用。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已生成":
            return {"ok": False, "message": "仅已生成人群包可标记过期"}
        entry["status"] = "已过期"
        entry["pending"] = False
        entry["abnormal"] = True
        entry["可领取"] = "否"
        cohort_code = str(entry.get("人群编码", ""))
        affected = [
            s for s in store.rows("segment_slice")
            if str(s.get("人群编码", "")) == cohort_code
            and s.get("status") == "待计算"
            and s.get("领取状态") != "已领取"
        ]
        for row in affected:
            row["status"] = "计算失败"
            row["abnormal"] = True
            row["失败原因"] = f"人群包 {cohort_code} 已过期"
        return {"ok": True, "message": f"人群包已过期，联动失败 {len(affected)} 条未领取切片"}

    def refresh_cohort(self, entry_id: int) -> dict[str, Any]:
        """按刷新周期重算人群规模并沉淀新版本，已领取切片不受影响。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已生成":
            return {"ok": False, "message": "仅已生成人群包可刷新"}
        parsed = parse_condition(entry.get("圈选条件", ""))
        if not parsed.ok:
            entry["条件状态"] = "条件无效"
            entry["校验说明"] = parsed.reason
            entry["可领取"] = "否"
            return {"ok": False, "message": f"刷新被拒绝：{parsed.reason}"}

        old_size = int(entry.get("人群规模", "0") or 0)
        old_version = str(entry.get("确认版本", ""))
        snapshot = self._snapshot(entry, parsed, trigger="刷新人群")
        new_size = snapshot["人群规模"]
        return {
            "ok": True,
            "message": (
                f"人群规模由 {old_size}（{old_version}）刷新为 {new_size}（{snapshot['确认版本']}）；"
                "已领取切片继续使用领取时冻结的成员来源"
            ),
        }

    def backfill_members(self, entry_id: int) -> dict[str, Any]:
        """人群补采：重新选择成员并沉淀新版本，供新作业看到新人数。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已生成":
            return {"ok": False, "message": "仅已生成人群包可补采成员"}
        parsed = parse_condition(entry.get("圈选条件", ""))
        if not parsed.ok:
            return {"ok": False, "message": f"补采被拒绝：{parsed.reason}"}
        old_size = int(entry.get("人群规模", "0") or 0)
        snapshot = self._snapshot(entry, parsed, trigger="补采成员")
        new_size = snapshot["人群规模"]
        if new_size <= old_size:
            return {
                "ok": True,
                "message": (
                    f"补采完成（{snapshot['确认版本']}）：规模仍为 {new_size}，"
                    "迟到成员尚未入账或不满足圈选范围"
                ),
            }
        return {
            "ok": True,
            "message": (
                f"补采完成（{snapshot['确认版本']}）：规模由 {old_size} 增至 {new_size}，"
                "新切片作业将看到新人数，已领取切片仍使用冻结快照"
            ),
        }

    def batch_expire_by_cycle(self, cycle: str) -> dict[str, Any]:
        """按刷新周期批量过期人群包。"""
        rows = [
            r for r in store.rows(self.MODULE)
            if r.get("刷新周期") == cycle and r.get("status") == "已生成"
        ]
        for row in rows:
            row["status"] = "已过期"
            row["pending"] = False
            row["abnormal"] = True
            row["可领取"] = "否"
        return {"ok": True, "message": f"周期 {cycle} 过期 {len(rows)} 个人群包"}

    def consume_from_exposure_log(self, exp_code: str) -> list[str]:
        """读取曝光日志中实验的活跃用户，作为圈选候选池。"""
        users = []
        seen = set()
        for r in store.rows("exposure_log"):
            if r.get("实验编码") != exp_code or r.get("status") == "异常丢弃":
                continue
            uid = str(r.get("用户标识", ""))
            if uid and uid not in seen:
                seen.add(uid)
                users.append(uid)
        return users

    def produce_to_segment_slice(self, entry_id: int) -> dict[str, Any]:
        """把人群包推送给切片分析器，登记基于人群的切片批次（发起切片）。

        只有条件有效、已生成且可领取的人群包才能发起切片；零规模人群允许
        发起（领取前可补采），条件无效的人群不产生可领取切片。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "人群包不存在"}
        if entry.get("status") != "已生成" or entry.get("可领取") != "是":
            return {"ok": False, "message": "仅条件有效、已生成的人群包可发起切片，无效条件不能生成可领取切片"}
        parsed = parse_condition(entry.get("圈选条件", ""))
        if not parsed.ok:
            return {"ok": False, "message": f"发起切片被拒绝：{parsed.reason}"}
        slice_rows = store.rows("segment_slice")
        slice_rows.append({
            "id": max((int(r.get("id", 0)) for r in slice_rows), default=0) + 1,
            "切片批次": f"SG-CO-{entry_id}-{datetime.now(timezone.utc).timestamp():.0f}",
            "实验编码": (parsed.scope or {}).get("实验编码", ""),
            "切片维度": "人群",
            "切片值": entry.get("人群名称", ""),
            "分组差异": "",
            "人群编码": entry.get("人群编码", ""),
            "切片条件": entry.get("圈选条件", ""),
            "圈选范围": entry.get("圈选范围", ""),
            "发起版本": entry.get("确认版本", ""),
            "领取状态": "待领取",
            "领取版本": "",
            "成员来源": [],
            "领取人数": "0",
            "补采说明": "",
            "status": "待计算",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": f"人群切片批次已登记（当前规模 {entry.get('人群规模', '0')}），待领取样本"}

    def list_active_cohorts(self) -> list[dict[str, Any]]:
        """列出全部已生成人群包，供报告生成器引用分析范围。"""
        return [
            {
                "人群编码": r.get("人群编码", ""),
                "人群名称": r.get("人群名称", ""),
                "人群规模": r.get("人群规模", ""),
                "圈选范围": r.get("圈选范围", ""),
                "确认版本": r.get("确认版本", ""),
                "可领取": r.get("可领取", ""),
                "刷新周期": r.get("刷新周期", ""),
            }
            for r in store.rows(self.MODULE) if r.get("status") == "已生成"
        ]

    def get_version(self, entry: dict[str, Any], version: str) -> dict[str, Any] | None:
        """按版本号取人群快照，供已完成切片复查所用的人群结果。"""
        for snapshot in entry.get("人群版本历史", []):
            if snapshot.get("确认版本") == version:
                return snapshot
        return None
