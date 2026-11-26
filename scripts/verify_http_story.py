"""用户故事全链路 HTTP 验证：E1 空态 → 在途领取 → 迟到成员补采 → 重领采用唯一成员。"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8022/api"


def call(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get(module: str, entry_id: int):
    return call("GET", f"/{module}/{entry_id}")


def find(module: str, **pred):
    items = call("GET", f"/{module}")["items"]
    return next(i for i in items if all(i.get(k) == v for k, v in pred.items()))


failures: list[str] = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# A. 页面核对：空白条件的失败 vs E1 的正常空态
blank = find("cohort_builder", 人群编码="COHO-BLANK")
r = call("POST", f"/cohort_builder/{blank['id']}/actions", {"action": "生成人群"})
check("空白条件生成失败", r["ok"] is False and "条件缺失" in r["message"], r["message"])

e1_id = find("cohort_builder", 人群编码="COHO-E1-TREAT")["id"]
r = call("POST", f"/cohort_builder/{e1_id}/actions", {"action": "生成人群"})
e1 = get("cohort_builder", e1_id)
check("E1 合法条件生成成功（零规模空态）", r["ok"] and e1["人群规模"] == "0", r["message"])
check("E1 零规模保留可查询且可领取", e1["可领取"] == "是" and e1["条件状态"] == "条件有效")

# B. 发起切片，领取在途（零成员）；取新登记的那条（id 最大）
r = call("POST", f"/cohort_builder/{e1_id}/actions", {"action": "发起切片"})
check("E1 发起切片", r["ok"], r["message"])
candidates = [
    i for i in call("GET", "/segment_slice")["items"]
    if i.get("人群编码") == "COHO-E1-TREAT" and i["领取状态"] == "待领取"
]
slice_id = max(int(i["id"]) for i in candidates)
slice_row = get("segment_slice", slice_id)
check("新切片携带发起版本", slice_row["发起版本"] == "V1", slice_row["发起版本"])

r = call("POST", f"/segment_slice/{slice_id}/actions", {"action": "领取样本"})
check("在途领取零成员有明确提示", r["ok"] and "有效成员为 0" in r["message"], r["message"])
slice_row = get("segment_slice", slice_id)
check("在途领取冻结 V1 零成员", slice_row["领取版本"] == "V1" and slice_row["领取人数"] == "0")

# C. 迟到成员入账 + 人群补采
call("POST", "/variant_assign", {"values": {
    "分配批次": "VA-E1-B9", "用户标识": "U-E1-LATE-HTTP",
    "实验编码": "E1", "分组名称": "实验组", "分配时间": "2026-11-26",
}})
r = call("POST", f"/cohort_builder/{e1_id}/actions", {"action": "补采成员"})
e1 = get("cohort_builder", e1_id)
check("补采后新人数为1（新作业看新人数）", r["ok"] and e1["人群规模"] == "1", r["message"])
check("补采沉淀新版本", e1["确认版本"] == "V2")
slice_row = get("segment_slice", slice_id)
check("在途切片仍冻结旧快照", slice_row["领取版本"] == "V1" and slice_row["领取人数"] == "0")

# D. 在途作业重试并重新领取：记录解释是否采用补入的唯一成员
call("POST", f"/segment_slice/{slice_id}/actions", {"action": "开始计算"})
call("POST", f"/segment_slice/{slice_id}/actions", {"action": "重试计算"})
r = call("POST", f"/segment_slice/{slice_id}/actions", {"action": "领取样本"})
slice_row = get("segment_slice", slice_id)
check("重新领取采用补入的唯一成员", r["ok"] and slice_row["领取人数"] == "1", r["message"])
check("领取记录解释采用补入成员", "U-E1-LATE-HTTP" in slice_row["补采说明"]
      and "V1 → 领取版本 V2" in slice_row["补采说明"], slice_row["补采说明"])
r = call("POST", f"/segment_slice/{slice_id}/actions", {"action": "开始计算"})
slice_row = get("segment_slice", slice_id)
check("切片计算成功并可复查", r["ok"] and slice_row["status"] == "已计算", r["message"])
check("成员来源指向样本明细", slice_row["成员来源"][0]["成员来源"].startswith("variant_assign:"),
      str(slice_row["成员来源"]))

# E. 同一 E1 条件不接纳 E2 同名组
e1_sources = {m["实验编码"] for m in slice_row["成员来源"]}
check("E1 切片成员全部来自 E1", e1_sources == {"E1"}, str(e1_sources))
check("E1 切片不含 E2 成员", all(m["用户标识"] != "U-E2-01" for m in slice_row["成员来源"]))

# F. JSON 写法条件同样可用
created = call("POST", "/cohort_builder", {"values": {
    "人群编码": "COHO-JSON", "人群名称": "JSON条件",
    "圈选条件": '{"实验编码":"E2","分组名称":"对照组"}', "刷新周期": "每日",
}})
check("JSON 条件登记成功", created["ok"], created["message"])
json_id = created["entry"]["id"]
call("POST", f"/cohort_builder/{json_id}/actions", {"action": "生成人群"})
created_row = get("cohort_builder", json_id)
check("JSON 条件精确命中 E2 对照组 1 人", created_row["人群规模"] == "1",
      created_row["人群规模"])

# G. 剔除成员不回流：剔除 E1 迟到成员后补采，人群回到 0
late = find("variant_assign", 用户标识="U-E1-LATE-HTTP", 实验编码="E1")
call("POST", f"/variant_assign/{late['id']}/actions", {"action": "剔除用户"})
r = call("POST", f"/cohort_builder/{e1_id}/actions", {"action": "补采成员"})
e1 = get("cohort_builder", e1_id)
check("剔除后补采不再包含该成员", e1["人群规模"] == "0", r["message"])

print()
print("全部通过" if not failures else f"失败 {failures}")
sys.exit(1 if failures else 0)
