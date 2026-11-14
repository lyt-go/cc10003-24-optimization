"""报表续做共用能力：重试与分页判断使用同一套判定结果。

这里只做纯判定，不读写数据仓库：
- 重试判定回答“这个任务/日期/指标集合该生成、该复用，还是只补未确认片段”；
- 分页判定回答“这次页段请求应该使用哪个版本，是否必须钉住首次请求的版本”。

两个入口都通过 `_decision` 产出同一种 `ResumeDecision`，便于调用方用统一写法
记录原因、版本和冲突信息；版本、产物与断点边界仍由各自的服务负责落地。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

STATUS_COMPLETED = "已完成"
STATUS_PENDING = "待重试"
STATUS_REUSED = "复用"
STATUS_GENERATING = "生成中"
STATUS_GENERATED = "已生成"
STATUS_CONFIRMED = "已确认"

REUSABLE_STATUSES = (STATUS_COMPLETED, STATUS_REUSED)
SUCCESS_STATUSES = (STATUS_GENERATED, STATUS_CONFIRMED)


@dataclass(frozen=True)
class ResumeDecision:
    """重试与分页共用的判定结果。"""

    kind: str
    action: str
    reason: str
    version: str = ""
    artifact_id: int | None = None
    source_task: str | None = None
    conflict_reason: str | None = None
    page: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "action": self.action,
            "reason": self.reason,
            "version": self.version,
            "artifact_id": self.artifact_id,
            "source_task": self.source_task,
            "conflict_reason": self.conflict_reason,
            "page": self.page,
        }


def ordered_metrics(value: Any) -> list[str]:
    """按录入顺序去重保留指标集合，用于返回字段与片段展示。"""
    if isinstance(value, (list, tuple, set)):
        parts: Iterable[Any] = value
    else:
        parts = re.split(r"[,，、;；|]", str(value or ""))
    seen: list[str] = []
    for part in parts:
        metric = str(part or "").strip()
        if metric and metric not in seen:
            seen.append(metric)
    return seen


def canonical_metrics(value: Any) -> list[str]:
    """把指标集合统一成排序去重后的列表。"""
    return sorted(ordered_metrics(value))


def metric_key(value: Any) -> str:
    """指标集合的稳定键：同一集合无论录入顺序都得到相同结果。"""
    return ",".join(canonical_metrics(value))


def is_confirmed_complete(row: dict[str, Any] | None) -> bool:
    """产物是否已经是“已确认的完整产物”。"""
    if not row:
        return False
    return bool(row.get("是否确认")) and row.get("状态") in REUSABLE_STATUSES


def _decision(
    kind: str,
    action: str,
    reason: str,
    *,
    version: str = "",
    artifact_id: int | None = None,
    source_task: str | None = None,
    conflict_reason: str | None = None,
    page: int | None = None,
) -> ResumeDecision:
    return ResumeDecision(
        kind=kind,
        action=action,
        reason=reason,
        version=version,
        artifact_id=artifact_id,
        source_task=source_task,
        conflict_reason=conflict_reason,
        page=page,
    )


def decide_artifact(
    *,
    data_date: str,
    metric_set: str,
    version: str,
    existing: dict[str, Any] | None = None,
    other_confirmed: dict[str, Any] | None = None,
) -> ResumeDecision:
    """判定某个任务/数据日期/指标集合的续做方式。

    优先级：
    1. 本任务已有同键、已确认的完整产物 -> 复用，不重复生成；
    2. 其他任务已有同日期、同指标集合的已确认完整产物 -> 转复用并记录冲突原因；
    3. 本任务有未确认产物 -> 只补缺失或未确认的交付片段；
    4. 没有产物 -> 生成缺失日期。
    """
    if existing is not None and is_confirmed_complete(existing):
        return _decision(
            "retry",
            "reuse",
            f"{data_date} 的 {metric_set} 已有本任务已确认完整产物，直接复用",
            version=version,
            artifact_id=int(existing.get("id") or 0) or None,
            source_task=str(existing.get("任务编号") or ""),
        )

    if other_confirmed is not None:
        source_task = str(other_confirmed.get("任务编号") or "")
        reason = (
            f"并发冲突：{data_date} 的 {metric_set} 已由任务 {source_task} "
            "生成并确认完整产物，本任务转为复用"
        )
        return _decision(
            "retry",
            "reuse",
            reason,
            version=version,
            artifact_id=int(other_confirmed.get("id") or 0) or None,
            source_task=source_task,
            conflict_reason=reason,
        )

    if existing is not None:
        return _decision(
            "retry",
            "retry",
            f"{data_date} 存在未确认产物，仅重试缺失日期或未确认的交付片段",
            version=version,
            artifact_id=int(existing.get("id") or 0) or None,
            source_task=str(existing.get("任务编号") or ""),
        )

    return _decision(
        "retry",
        "generate",
        f"{data_date} 缺少 {metric_set} 产物，生成缺失日期的交付片段",
        version=version,
    )


def decide_page(
    *,
    requested_version: str | None,
    current_version: str,
    available_versions: Iterable[str],
    page: int,
) -> ResumeDecision:
    """判定页段读取应使用的版本。

    首次请求没有版本时取当前已确认版本；后续页显式带上首次返回的版本后，
    即使期间完成了新版本，也必须继续读旧版本，禁止跨版本拼接。
    """
    versions = list(dict.fromkeys(str(item) for item in available_versions if item))
    if requested_version:
        if requested_version not in versions:
            return _decision(
                "page",
                "reject",
                f"版本 {requested_version} 不存在，不能用其他版本续读当前页段",
                version=requested_version,
                page=page,
            )
        if requested_version != current_version:
            return _decision(
                "page",
                "pin",
                f"读取期间当前版本已更新为 {current_version}，后续页仍按首次请求的版本 {requested_version} 续读",
                version=requested_version,
                page=page,
            )
        return _decision(
            "page",
            "pin",
            f"沿用首次请求的版本 {requested_version} 续读页段",
            version=requested_version,
            page=page,
        )

    return _decision(
        "page",
        "current",
        f"未指定版本，读取当前已确认版本 {current_version}",
        version=current_version,
        page=page,
    )
