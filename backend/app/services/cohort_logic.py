"""人群圈选共享逻辑：条件解析、服务端成员选择与圈选范围描述。

切片分析器领取样本时必须与人群圈选器走同一套解析和选择逻辑，避免出现
页面一份口径、服务端另一份口径。所有选择都以「实验编码 + 分组名称」精确
匹配，同名分组在不同实验中天然隔离，绝不按分组名称跨实验合并。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.store import store

# 圈选条件允许携带的键
_EXPERIMENT_KEY = "实验编码"
_GROUP_KEY = "分组名称"
_USER_KEY = "用户标识"


@dataclass
class ParsedCondition:
    """圈选条件解析结果。

    ok 为 False 时 reason 说明是「条件缺失」还是「条件无法解析」；
    ok 为 True 时 scope 内是结构化后的精确圈选范围。
    """

    ok: bool
    reason: str = ""
    scope: dict[str, str] | None = None
    raw: str = ""


def parse_condition(raw_condition: Any) -> ParsedCondition:
    """解析圈选条件，支持 JSON 对象与 ``key=value;key=value`` 两种写法。

    「实验编码」是必填项：没有它无法限定实验范围，服务端会拒绝，避免
    同名分组跨实验合并。
    """
    text = str(raw_condition or "").strip()
    if not text:
        return ParsedCondition(ok=False, reason="条件缺失：圈选条件为空，无法生成人群", raw=text)

    scope: dict[str, str] | None = None
    if text.startswith("{"):
        try:
            loaded = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            return ParsedCondition(
                ok=False, reason=f"条件无法解析：JSON 格式错误（{exc.msg if isinstance(exc, json.JSONDecodeError) else exc}）", raw=text
            )
        if not isinstance(loaded, dict):
            return ParsedCondition(ok=False, reason="条件无法解析：条件必须是键值对象", raw=text)
        scope = {str(k).strip(): str(v).strip() for k, v in loaded.items() if str(v).strip()}
    else:
        scope = {}
        for part in text.replace("，", ";").replace(",", ";").split(";"):
            token = part.strip()
            if not token:
                continue
            if "=" not in token:
                return ParsedCondition(
                    ok=False,
                    reason="条件无法解析：片段需写成 键=值，例如 实验编码=E1;分组名称=实验组",
                    raw=text,
                )
            key, value = token.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key or not value:
                return ParsedCondition(ok=False, reason="条件无法解析：存在空的键或值", raw=text)
            scope[key] = value

    if not scope:
        return ParsedCondition(ok=False, reason="条件缺失：圈选条件为空，无法生成人群", raw=text)

    experiment_code = scope.get(_EXPERIMENT_KEY, "").strip()
    if not experiment_code:
        return ParsedCondition(
            ok=False, reason="条件无法解析：缺少必填的 实验编码，无法限定圈选范围", raw=text
        )

    known_experiments = {
        str(r.get(_EXPERIMENT_KEY, ""))
        for r in store.rows("variant_assign")
        if str(r.get(_EXPERIMENT_KEY, "")).strip()
    }
    if experiment_code not in known_experiments:
        return ParsedCondition(
            ok=False,
            reason=f"条件无法解析：实验编码 {experiment_code} 在分组分配器中不存在",
            raw=text,
        )

    group_name = scope.get(_GROUP_KEY, "").strip()
    if group_name and group_name not in ("对照组", "实验组"):
        return ParsedCondition(
            ok=False,
            reason=f"条件无法解析：分组名称 {group_name} 非法，仅支持 对照组 / 实验组",
            raw=text,
        )

    return ParsedCondition(ok=True, scope=scope, raw=text)


def describe_scope(scope: dict[str, str]) -> str:
    """生成可读的圈选范围，明确实验边界，避免同名组混淆。"""
    parts = [f"实验编码={scope.get(_EXPERIMENT_KEY, '')}"]
    if scope.get(_GROUP_KEY):
        parts.append(f"分组名称={scope[_GROUP_KEY]}")
    if scope.get(_USER_KEY):
        parts.append(f"用户标识={scope[_USER_KEY]}")
    return "；".join(parts)


def select_members(scope: dict[str, str]) -> list[dict[str, Any]]:
    """按解析后的范围从分组分配明细中精确选择有效成员。

    - 实验编码精确匹配：同名分组在不同实验中不会合并；
    - 已剔除（status == 已剔除）成员永远不会因为条件相符而重新进入人群；
    - 分组名称、用户标识存在时继续精确收窄。
    """
    experiment_code = str(scope.get(_EXPERIMENT_KEY, "")).strip()
    group_name = str(scope.get(_GROUP_KEY, "")).strip()
    user_id = str(scope.get(_USER_KEY, "")).strip()

    matched: list[dict[str, Any]] = []
    for row in store.rows("variant_assign"):
        if str(row.get(_EXPERIMENT_KEY, "")) != experiment_code:
            continue
        if row.get("status") == "已剔除":
            continue
        if group_name and str(row.get(_GROUP_KEY, "")) != group_name:
            continue
        if user_id and str(row.get(_USER_KEY, "")) != user_id:
            continue
        matched.append(row)
    return matched


def member_summary(members: list[dict[str, Any]]) -> list[dict[str, str]]:
    """输出成员来源明细，供切片作业查看每个成员实际来自哪个分配批次。"""
    return [
        {
            "用户标识": str(row.get(_USER_KEY, "")),
            "实验编码": str(row.get(_EXPERIMENT_KEY, "")),
            "分组名称": str(row.get(_GROUP_KEY, "")),
            "分配批次": str(row.get("分配批次", "")),
            "成员来源": f"variant_assign:{row.get('id', '')}",
        }
        for row in members
    ]


def group_stats(members: list[dict[str, Any]]) -> dict[str, int]:
    """统计成员中的对照组/实验组人数。"""
    return {
        "对照组": sum(1 for row in members if row.get(_GROUP_KEY) == "对照组"),
        "实验组": sum(1 for row in members if row.get(_GROUP_KEY) == "实验组"),
    }
