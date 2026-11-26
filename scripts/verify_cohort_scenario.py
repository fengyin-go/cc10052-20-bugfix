"""人群圈选 + 切片领取/补采全链路场景验证（直接调用领域服务）。

运行：cd backend && .venv/bin/python /workspace/scripts/verify_cohort_scenario.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.services import service_for
from app.store import store

cb = service_for("cohort_builder")
ss = service_for("segment_slice")
va_rows = store.rows("variant_assign")

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def find_cohort(code: str):
    return next(r for r in store.rows("cohort_builder") if r["人群编码"] == code)


# 1. 空白条件：生成必须被拒绝，且说明是条件缺失
blank = find_cohort("COHO-BLANK")
res = cb.run_action(blank["id"], "生成人群")
check("空白条件拒绝生成", res["ok"] is False, res["message"])
check("空白条件保持草稿", blank["status"] == "草稿")
check("空白条件不可领取", blank["可领取"] == "否")
check("说明为条件缺失", "条件缺失" in blank["校验说明"], blank["校验说明"])

# 2. 无法解析的条件：登记即拒绝，修正后可重新登记
try:
    cb.create_entry({
        "人群编码": "COHO-BAD",
        "人群名称": "坏条件",
        "圈选条件": '{"实验编码": ',
        "刷新周期": "每日",
    })
    check("非法JSON登记拒绝", False)
except ValueError as exc:
    check("非法JSON登记拒绝", "条件无法解析" in str(exc), str(exc))

try:
    cb.create_entry({
        "人群编码": "COHO-NOEXP",
        "人群名称": "缺实验",
        "圈选条件": "分组名称=实验组",
        "刷新周期": "每日",
    })
    check("缺实验编码拒绝", False)
except ValueError as exc:
    check("缺实验编码拒绝", "实验编码" in str(exc), str(exc))

try:
    cb.create_entry({
        "人群编码": "COHO-UNKEXP",
        "人群名称": "未知实验",
        "圈选条件": "实验编码=E9;分组名称=实验组",
        "刷新周期": "每日",
    })
    check("未知实验编码拒绝", False)
except ValueError as exc:
    check("未知实验编码拒绝", "不存在" in str(exc), str(exc))

# 修正后重交成功
entry, missing = cb.create_entry({
    "人群编码": "COHO-FIXED",
    "人群名称": "修正后条件",
    "圈选条件": "实验编码=E2;分组名称=实验组",
    "刷新周期": "每日",
})
check("修正后登记成功", entry is not None and not missing)

# 3. E1 实验组：合法但零命中 -> 保留可查询零规模、可领取
e1 = find_cohort("COHO-E1-TREAT")
res = cb.run_action(e1["id"], "生成人群")
check("E1零规模生成成功", res["ok"], res["message"])
check("E1状态已生成", e1["status"] == "已生成")
check("E1规模为0", e1["人群规模"] == "0", e1["人群规模"])
check("E1零规模仍可领取", e1["可领取"] == "是")
check("E1确认版本V1", e1["确认版本"] == "V1")
check("E1圈选范围可见", e1["圈选范围"] == "实验编码=E1；分组名称=实验组")
check("说明为确实无人", "暂无成员" in e1["校验说明"], e1["校验说明"])

# 4. 发起切片（在途）
res = cb.produce_to_segment_slice(e1["id"])
check("零规模可发起切片", res["ok"], res["message"])
inflight = store.rows("segment_slice")[-1]
check("在途切片待领取", inflight["领取状态"] == "待领取")
check("在途切片带发起版本", inflight["发起版本"] == "V1")

# 5. 在途领取：零成员
res = ss.run_action(inflight["id"], "领取样本")
check("零成员领取返回提示", res["ok"] and "有效成员为 0" in res["message"], res["message"])
check("在途切片已领取冻结", inflight["领取状态"] == "已领取" and inflight["领取人数"] == "0")

# 6. 迟到成员入账（补采）
va_rows.append({
    "id": max(int(r["id"]) for r in va_rows) + 1,
    "分配批次": "VA-E1-B2",
    "用户标识": "U-E1-LATE",
    "实验编码": "E1",
    "分组名称": "实验组",
    "分配时间": "2026-11-26T00:00:00+00:00",
    "status": "已分配",
    "pending": False,
    "abnormal": False,
})
res = cb.run_action(e1["id"], "补采成员")
check("补采产生新版本V2", res["ok"] and e1["确认版本"] == "V2", res["message"])
check("补采后新人数1", e1["人群规模"] == "1", e1["人群规模"])
check("版本历史保留V1/V2", [s["确认版本"] for s in e1["人群版本历史"]] == ["V1", "V2"])

# 7. 已领取的在途切片：冻结成员不受补采影响
check("在途切片仍冻结0人", inflight["领取人数"] == "0" and inflight["领取版本"] == "V1")
res = ss.run_action(inflight["id"], "领取样本")
check("重复领取被拒绝", res["ok"] is False, res["message"])
res = ss.run_action(inflight["id"], "开始计算")
check("零成员切片计算失败", res["ok"] is False and inflight["status"] == "计算失败", res["message"])
res = ss.run_action(inflight["id"], "重试计算")
check("失败后重试回待领取", res["ok"] and inflight["领取状态"] == "待领取", res["message"])
res = ss.run_action(inflight["id"], "领取样本")
check("重试后领取看到补入唯一成员", res["ok"] and inflight["领取人数"] == "1", res["message"])
check("领取记录解释采用补入成员", "U-E1-LATE" in inflight["补采说明"], inflight["补采说明"])
check("领取版本升级V2", inflight["领取版本"] == "V2")
res = ss.run_action(inflight["id"], "开始计算")
check("补采后切片计算成功", res["ok"] and inflight["status"] == "已计算", res["message"])
check("差异基于1名实验组成员", inflight["分组差异"] == "1.0", inflight["分组差异"])

# 8. 复查所用人群结果
review = ss.review_cohort_snapshot(inflight["id"])
check("复查返回V2快照", review["ok"] and review["版本快照"]["确认版本"] == "V2", review["message"])
check("复查含实际成员来源", review["成员来源"][0]["成员来源"] == "variant_assign:204",
      str(review["成员来源"]))

# 9. 同名组隔离：E2 实验组只取 E2 成员
e2 = find_cohort("COHO-E2-TREAT")
res = cb.produce_to_segment_slice(e2["id"])
check("E2发起切片", res["ok"], res["message"])
e2_slice = store.rows("segment_slice")[-1]
res = ss.run_action(e2_slice["id"], "领取样本")
users = {m["用户标识"] for m in e2_slice["成员来源"]}
check("E2领取不含E1成员", users == {"U-E2-01", "U-E2-02"}, str(users))
check("E2领取人数2", e2_slice["领取人数"] == "2")
check("E2成员来源可查看", all(m["成员来源"].startswith("variant_assign:") for m in e2_slice["成员来源"]))
res = ss.run_action(e2_slice["id"], "开始计算")
check("E2切片计算完成", res["ok"] and e2_slice["status"] == "已计算", res["message"])

# 10. 已剔除成员不会重新进入
e1_members = {m["用户标识"] for s in e1["人群版本历史"] for m in s["成员来源"]}
check("剔除成员未进入人群", "U-E1-EXCLUDED" not in e1_members, str(e1_members))

# 11. 无效条件人群不能发起切片
res = cb.produce_to_segment_slice(blank["id"])
check("无效条件不能生成可领取切片", res["ok"] is False, res["message"])

# 12. 刷新动作：新版本且已领取切片不动
res = cb.run_action(e2["id"], "刷新人群")
check("E2刷新成功", res["ok"], res["message"])
check("E2刷新到V2", e2["确认版本"] == "V2")
check("已领取E2切片仍冻结V1", e2_slice["领取版本"] == "V1" and e2_slice["领取人数"] == "2")

# 13. 过期联动：只影响未领取切片
fresh = cb.produce_to_segment_slice(e2["id"])
new_slice = store.rows("segment_slice")[-1]
res = cb.run_action(e2["id"], "标记过期")
check("过期成功", res["ok"], res["message"])
check("未领取切片联动失败", new_slice["status"] == "计算失败", new_slice.get("失败原因", ""))
check("已计算切片不受过期影响", e2_slice["status"] == "已计算")

print()
if failures:
    print(f"共 {len(failures)} 项失败：{failures}")
    sys.exit(1)
print("全部场景验证通过")
