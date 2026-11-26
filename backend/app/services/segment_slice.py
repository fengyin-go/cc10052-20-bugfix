"""切片分析器领域服务：管理实验差异的维度切片、切片值计算与失败重试。

数据链路位置：下钻分析层，消费显著性检验与归因分析的差异结果，按维度切片
计算分组差异，输出给报告生成器形成下钻章节。

人群切片链路：
- 发起切片只登记作业与发起版本，不复制成员；
- 「领取样本」按人群包当前确认版本，从分组分配明细实际选择可用成员并冻结
  成员来源、领取版本与补采说明；无效条件人群不能领取；
- 「开始计算」基于冻结的成员来源计算，人群补采后的新作业才会看到新人数；
- 已完成切片保留人群编码/领取版本，可随时复查当时采用的人群结果。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.cohort_logic import (
    describe_scope,
    group_stats,
    parse_condition,
    select_members,
)
from app.store import store


class SegmentSliceService(BaseModuleService):
    MODULE = "segment_slice"
    ENTITY = "切片结果"
    OPERATION_TERM = "计算"
    LIST_FIELDS = [
        "切片批次",
        "实验编码",
        "切片维度",
        "切片值",
        "人群编码",
        "领取状态",
        "领取版本",
        "领取人数",
        "成员来源",
        "补采说明",
        "失败原因",
        "分组差异",
    ]
    CREATE_FIELDS = ["切片批次", "实验编码", "切片维度", "切片值", "人群编码", "切片条件"]
    REQUIRED_FIELDS = ["切片批次", "实验编码", "切片维度"]
    STATUS_ORDER = ["待计算", "已计算", "计算失败"]
    ACTION_RULES = {
        "领取样本": "待计算",
        "开始计算": "已计算",
        "重试计算": "计算失败",
    }
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        rows = store.rows(self.MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        }
        for field in self.CREATE_FIELDS:
            entry[field] = values.get(field, "")
        entry.update({
            "分组差异": "",
            "圈选范围": "",
            "发起版本": "",
            "领取状态": "待领取",
            "领取版本": "",
            "成员来源": [],
            "领取人数": "0",
            "补采说明": "",
            "失败原因": "",
            "status": "待计算",
            "pending": True,
            "abnormal": False,
        })
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在或已归档", "entry": None}
        if action == "领取样本":
            result = self.claim_sample(entry_id)
            return {"ok": result["ok"], "message": result["message"], "entry": entry}
        if action == "开始计算":
            result = self.run_slice(entry_id)
            return {"ok": result["ok"], "message": result["message"], "entry": entry}
        if action == "重试计算":
            result = self.retry_failed(entry_id)
            return {"ok": result["ok"], "message": result["message"], "entry": entry}
        return {
            "ok": False,
            "message": f"动作「{action}」不属于{self.MODULE}可执行范围",
            "entry": None,
        }

    # ---- 业务扩展：样本领取与维度切片 ----

    def claim_sample(self, entry_id: int) -> dict[str, Any]:
        """领取人群样本：按人群包当前版本实际选择可用成员并冻结来源。

        取样入口完全依据分组分配明细（样本明细）选择成员；无效条件人群、
        非已生成/不可领取人群一律拒绝，避免产生没有有效来源的切片。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "待计算":
            return {"ok": False, "message": "仅待计算切片可领取样本"}
        if str(entry.get("切片维度", "")) != "人群":
            return {"ok": False, "message": "仅人群维度切片需要领取样本，分组维度可直接开始计算"}
        if entry.get("领取状态") == "已领取":
            return {"ok": False, "message": "样本已领取，成员来源已冻结，请勿重复领取"}

        cohort_code = str(entry.get("人群编码", "")).strip()
        cohort = next(
            (r for r in store.rows("cohort_builder") if str(r.get("人群编码", "")) == cohort_code),
            None,
        )
        if cohort is None:
            entry["status"] = "计算失败"
            entry["abnormal"] = True
            entry["失败原因"] = f"人群包 {cohort_code} 不存在"
            return {"ok": False, "message": f"人群包 {cohort_code} 不存在，无法领取"}

        # 无效条件不能生成可领取切片：再次服务端校验，防止绕过页面
        parsed = parse_condition(cohort.get("圈选条件", ""))
        if not parsed.ok:
            entry["status"] = "计算失败"
            entry["abnormal"] = True
            entry["失败原因"] = parsed.reason
            return {"ok": False, "message": f"领取被拒绝：{parsed.reason}"}
        if cohort.get("status") != "已生成" or cohort.get("可领取") != "是":
            entry["status"] = "计算失败"
            entry["abnormal"] = True
            entry["失败原因"] = f"人群包 {cohort_code} 当前不可领取（状态 {cohort.get('status')}）"
            return {"ok": False, "message": "人群包未生成或不可领取，无法领取样本"}

        members = select_members(parsed.scope or {})
        claimed_version = str(cohort.get("确认版本", ""))
        launch_version = str(entry.get("发起版本", ""))
        # 与发起时快照对比，识别本次领取是否实际采用了补采进来的成员
        launch_snapshot = next(
            (s for s in cohort.get("人群版本历史", []) if s.get("确认版本") == launch_version),
            None,
        )
        launch_users = {
            str(item.get("用户标识", ""))
            for item in (launch_snapshot or {}).get("成员来源", [])
        }
        backfilled = [
            str(row.get("用户标识", "")) for row in members
            if str(row.get("用户标识", "")) not in launch_users
        ]

        entry["实验编码"] = parsed.scope.get("实验编码", "") if parsed.scope else entry.get("实验编码", "")
        entry["圈选范围"] = describe_scope(parsed.scope or {})
        entry["领取版本"] = claimed_version
        entry["领取状态"] = "已领取"
        entry["领取时间"] = datetime.now(timezone.utc).isoformat()
        entry["成员来源"] = [
            {
                "用户标识": str(row.get("用户标识", "")),
                "实验编码": str(row.get("实验编码", "")),
                "分组名称": str(row.get("分组名称", "")),
                "分配批次": str(row.get("分配批次", "")),
                "成员来源": f"variant_assign:{row.get('id', '')}",
            }
            for row in members
        ]
        entry["领取人数"] = str(len(members))
        entry["分组统计"] = group_stats(members)
        if backfilled:
            entry["补采说明"] = (
                f"采用补入成员 {len(backfilled)} 人：{'、'.join(backfilled)}"
                f"（发起版本 {launch_version or '无'} → 领取版本 {claimed_version}）"
            )
        else:
            entry["补采说明"] = (
                f"未采用补入成员，成员与发起版本 {launch_version or claimed_version} 一致"
            )
        # 领取后即使刷新/补采产生新版本，本作业仍使用冻结的成员来源
        entry["pending"] = True

        if not members:
            return {
                "ok": True,
                "message": (
                    f"已领取 {cohort_code}（{claimed_version}）但当前有效成员为 0，"
                    "开始计算将失败，可等待补采后刷新人群并重试"
                ),
            }
        return {
            "ok": True,
            "message": (
                f"已领取 {cohort_code}（{claimed_version}）有效成员 {len(members)} 人；"
                f"{entry['补采说明']}"
            ),
        }

    def run_slice(self, entry_id: int) -> dict[str, Any]:
        """执行切片计算：人群维度用冻结成员，分组维度用实验全量有效成员。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "待计算":
            return {"ok": False, "message": "仅待计算切片可执行"}
        exp_code = str(entry.get("实验编码", ""))
        dimension = str(entry.get("切片维度", ""))

        if dimension not in ("分组", "人群", "归档恢复"):
            entry["status"] = "计算失败"
            entry["abnormal"] = True
            entry["失败原因"] = f"不支持的切片维度 {dimension}"
            return {"ok": False, "message": f"切片维度 {dimension} 不受支持"}

        if dimension == "人群":
            if entry.get("领取状态") != "已领取":
                entry["status"] = "计算失败"
                entry["abnormal"] = True
                entry["失败原因"] = "人群切片尚未领取样本，不能计算"
                return {"ok": False, "message": "请先领取样本再开始计算"}
            # 取样依据：领取时冻结的成员来源，补采/刷新不影响本次计算
            members = entry.get("成员来源", [])
            if not members:
                entry["status"] = "计算失败"
                entry["abnormal"] = True
                entry["失败原因"] = "领取时有效成员为 0（人群零规模），请补采后重试"
                return {"ok": False, "message": "人群无有效成员，切片计算失败；补采刷新后可重试"}
            ctrl = sum(1 for r in members if r.get("分组名称") == "对照组")
            treat = sum(1 for r in members if r.get("分组名称") == "实验组")
            total = len(members)
        else:
            assign_rows = [
                r for r in store.rows("variant_assign")
                if r.get("实验编码") == exp_code and r.get("status") != "已剔除"
            ]
            if not assign_rows:
                entry["status"] = "计算失败"
                entry["abnormal"] = True
                entry["失败原因"] = "无分组数据可切片"
                return {"ok": False, "message": "实验无分组数据，切片计算失败"}
            ctrl = sum(1 for r in assign_rows if r.get("分组名称") == "对照组")
            treat = sum(1 for r in assign_rows if r.get("分组名称") == "实验组")
            total = len(assign_rows)

        diff = round((treat - ctrl) / max(total, 1), 4)
        entry["分组差异"] = str(diff)
        entry["status"] = "已计算"
        entry["pending"] = False
        entry["计算时间"] = datetime.now(timezone.utc).isoformat()
        return {
            "ok": True,
            "message": (
                f"切片 {entry.get('切片值', dimension)} 差异 {diff} 已计算"
                f"（对照 {ctrl}/实验 {treat}，共 {total} 人）"
            ),
        }

    def retry_failed(self, entry_id: int) -> dict[str, Any]:
        """重试失败切片：人群切片回到待领取，允许在补采后重新领取。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "计算失败":
            return {"ok": False, "message": "仅计算失败切片可重试"}
        reason = str(entry.get("失败原因", ""))
        if "维度" in reason:
            entry["切片维度"] = "分组"
        if str(entry.get("切片维度", "")) == "人群":
            # 清除旧的领取冻结，重新走领取→计算，确保看到补采后的新成员
            entry["领取状态"] = "待领取"
            entry["领取版本"] = ""
            entry["成员来源"] = []
            entry["领取人数"] = "0"
            entry["补采说明"] = ""
            entry["失败原因"] = ""
        entry["status"] = "待计算"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["重试次数"] = str(int(entry.get("重试次数", "0") or 0) + 1)
        return {"ok": True, "message": f"切片已修复并回到待计算（第 {entry['重试次数']} 次），请重新领取样本"}

    def batch_run_pending(self) -> dict[str, Any]:
        """批量执行全部待计算切片。"""
        pending = [r for r in store.rows(self.MODULE) if r.get("status") == "待计算"]
        done = sum(1 for r in pending if self.run_slice(int(r.get("id", 0))).get("ok"))
        return {"ok": True, "message": f"批量切片完成 {done}/{len(pending)}"}

    def consume_from_significance_test(self, exp_code: str) -> list[dict[str, Any]]:
        """读取显著性检验的置信区间，作为切片差异对照基线。"""
        return [
            {"指标编码": r.get("指标编码", ""), "置信区间": r.get("置信区间", "")}
            for r in store.rows("significance_test")
            if r.get("实验编码") == exp_code and r.get("status") == "已检验"
        ]

    def consume_from_cohort_builder(self, cohort_code: str) -> dict[str, Any]:
        """读取人群圈选器的人群包信息，限定切片范围。"""
        cohort = next(
            (r for r in store.rows("cohort_builder") if str(r.get("人群编码", "")) == cohort_code),
            None,
        )
        if cohort is None:
            return {"ok": False, "message": f"人群包 {cohort_code} 不存在"}
        return {
            "ok": cohort.get("status") == "已生成" and cohort.get("可领取") == "是",
            "人群规模": cohort.get("人群规模", ""),
            "圈选范围": cohort.get("圈选范围", ""),
            "确认版本": cohort.get("确认版本", ""),
            "条件状态": cohort.get("条件状态", ""),
            "状态": cohort.get("status", ""),
        }

    def review_cohort_snapshot(self, entry_id: int) -> dict[str, Any]:
        """复查已完成切片当时采用的人群结果（版本快照与成员来源）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "切片不存在"}
        cohort_code = str(entry.get("人群编码", ""))
        if not cohort_code:
            return {"ok": False, "message": "该切片不是人群维度，无人群结果可复查"}
        cohort = next(
            (r for r in store.rows("cohort_builder") if str(r.get("人群编码", "")) == cohort_code),
            None,
        )
        if cohort is None:
            return {"ok": False, "message": f"人群包 {cohort_code} 已不存在"}
        version = str(entry.get("领取版本", ""))
        snapshot = next(
            (s for s in cohort.get("人群版本历史", []) if s.get("确认版本") == version),
            None,
        )
        return {
            "ok": True,
            "message": "已返回切片采用的人群版本快照",
            "人群编码": cohort_code,
            "领取版本": version,
            "圈选范围": entry.get("圈选范围", ""),
            "成员来源": entry.get("成员来源", []),
            "补采说明": entry.get("补采说明", ""),
            "版本快照": snapshot,
        }

    def produce_to_report_gen(self, exp_code: str) -> dict[str, Any]:
        """把已计算切片推送给报告生成器，登记下钻章节报告。"""
        computed = [
            r for r in store.rows(self.MODULE)
            if r.get("实验编码") == exp_code and r.get("status") == "已计算"
        ]
        if not computed:
            return {"ok": False, "message": "实验无已计算切片"}
        report_rows = store.rows("report_gen")
        report_rows.append({
            "id": max((int(r.get("id", 0)) for r in report_rows), default=0) + 1,
            "报告编码": f"RPT-SG-{exp_code}-{datetime.now(timezone.utc).timestamp():.0f}",
            "实验编码": exp_code,
            "报告模板": "切片下钻模板",
            "数据窗口": "最近7天",
            "生成状态": "排队中",
            "status": "排队中",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": f"下钻报告已排队（覆盖 {len(computed)} 条切片）"}

    def export_slice_matrix(self, exp_code: str) -> list[dict[str, Any]]:
        """导出实验的切片矩阵，供指标查询服务提供下钻数据。"""
        return [
            {
                "切片维度": r.get("切片维度", ""),
                "切片值": r.get("切片值", ""),
                "人群编码": r.get("人群编码", ""),
                "领取版本": r.get("领取版本", ""),
                "领取人数": r.get("领取人数", ""),
                "分组差异": r.get("分组差异", ""),
            }
            for r in store.rows(self.MODULE)
            if r.get("实验编码") == exp_code and r.get("status") == "已计算"
        ]
