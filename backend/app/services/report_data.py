"""报表数据服务：失败续做、产物复用、版本推进与页段读取。

重试与分页的“该不该复用、该用哪个版本”统一走 `report_resume` 的共用判定；
本模块只负责把判定结果落成产物、批次、服务版本和下载记录。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import date, timedelta
from typing import Any

from app.store import store
from app.services.report_resume import (
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_REUSED,
    canonical_metrics,
    decide_artifact,
    decide_page,
    is_confirmed_complete,
    metric_key,
    ordered_metrics,
)

ARTIFACT = "report_artifact"
BATCH = "report_retry_batch"
VERSION = "delivery_version"
DOWNLOAD = "download_record"
FRAGMENT_CONFIRMED = "已确认"
FRAGMENT_FAILED = "失败"
BATCH_COMPLETED = "已完成"
BATCH_PENDING = "待重试"

# 批次执行串行化：同一时刻只允许一个续做批次推进产物和版本，
# 两个任务“并发生成同一日期”时后到的批次会读到先完成方并转复用。
_BATCH_LOCK = threading.RLock()


def _pick(values: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in values and values[key] not in (None, ""):
            return values[key]
    return default


def _parse_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "").strip()


def _parse_dates(value: Any, reference: date | None = None, offset: int = 0) -> list[str]:
    """把数据日期解析成排序去重后的 ISO 日期列表；支持单日、列表和闭区间。"""
    if value is None or value == "":
        if reference is None:
            return []
        return [(reference - timedelta(days=max(offset, 0))).isoformat()]

    if isinstance(value, (list, tuple, set)):
        dates = [_parse_date(item) for item in value]
        return sorted({item for item in dates if item})

    text = str(value).strip()
    for separator in ("~", "～", "至", ".."):
        if separator in text:
            start_text, end_text = (part.strip() for part in text.split(separator, 1))
            try:
                start_day = date.fromisoformat(start_text)
                end_day = date.fromisoformat(end_text)
            except ValueError:
                break
            if start_day > end_day:
                start_day, end_day = end_day, start_day
            span = (end_day - start_day).days
            return [(start_day + timedelta(days=index)).isoformat() for index in range(span + 1)]

    dates = [_parse_date(item) for item in text.replace("，", ",").replace("、", ",").split(",")]
    return sorted({item for item in dates if item})


def _reference_date(values: dict[str, Any]) -> date:
    raw = _pick(values, "运行日期", "调度日期", "reference_date", "run_date")
    if raw:
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            pass
    return date.today()


def _offset_days(task: dict[str, Any], values: dict[str, Any] | None = None) -> int:
    raw = _pick(values or {}, "数据日期偏移", "date_offset", default=task.get("数据日期偏移"))
    text = str(raw if raw is not None else "0").strip().replace("天", "")
    try:
        return max(int(text), 0)
    except ValueError:
        return 0


def _retry_limit(task: dict[str, Any]) -> int:
    text = str(task.get("重试次数上限") or "3").strip().replace("次", "")
    try:
        return max(int(text), 0)
    except ValueError:
        return 3


def _task_metrics(task: dict[str, Any]) -> list[str]:
    return canonical_metrics(task.get("包含指标"))


def _task_fields(task: dict[str, Any]) -> list[str]:
    # 服务返回字段必须与排期结果同序，避免前端展示顺序漂移。
    fields = ordered_metrics(task.get("返回字段"))
    return fields or ordered_metrics(task.get("包含指标"))


def _task_code(task: dict[str, Any]) -> str:
    return str(task.get("任务编号") or "")


def _service_code(service: dict[str, Any] | None, task: dict[str, Any]) -> str:
    if service:
        return str(service.get("服务编号") or "")
    return _task_code(task)


def _flatten_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """兼容 {values:{...}} 与扁平 JSON 两种提交方式。"""
    nested = payload.get("values")
    if isinstance(nested, dict):
        merged = dict(nested)
        merged.update({key: value for key, value in payload.items() if key not in {"values", "remark"}})
        return merged
    return dict(payload)


def _empty_stats() -> dict[str, Any]:
    return _stats_payload({"已完成": 0, "待重试": 0, "复用": 0})


def _stats_payload(stats: dict[str, Any]) -> dict[str, Any]:
    """中英并列的统计字段，避免调用方因命名差异读不到验收数字。"""
    completed = int(stats.get("已完成") or 0)
    pending_retry = int(stats.get("待重试") or 0)
    reused = int(stats.get("复用") or 0)
    total = int(stats.get("产物总数") or 0)
    payload = dict(stats)
    payload.update({
        "已完成数量": completed,
        "待重试数量": pending_retry,
        "复用数量": reused,
        "completed": completed,
        "pending_retry": pending_retry,
        "reused": reused,
        "completed_count": completed,
        "retry_count": pending_retry,
        "reused_count": reused,
        "total": total,
    })
    return payload


_EMPTY_STATS = _empty_stats()


class ReportDataService:
    # ---------- 入口解析 ----------

    @staticmethod
    def resolve_service_ref(value: Any) -> dict[str, Any] | None:
        """按 id 或服务编号定位数据服务。"""
        text = str(value or "").strip()
        if not text:
            return None
        if text.isdigit():
            return store.find("delivery", int(text))
        return next(
            (row for row in store.rows("delivery") if str(row.get("服务编号")) == text),
            None,
        )

    @staticmethod
    def resolve_task_ref(value: Any) -> dict[str, Any] | None:
        """按 id 或任务编号定位报表任务。"""
        text = str(value or "").strip()
        if not text:
            return None
        if text.isdigit():
            return store.find("reportsch", int(text))
        return next(
            (row for row in store.rows("reportsch") if str(row.get("任务编号")) == text),
            None,
        )

    # ---------- 任务/服务解析 ----------

    def resolve_task(self, *, service: dict[str, Any] | None = None, task: dict[str, Any] | None = None,
                     values: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if task is not None:
            return task
        values = values or {}
        task_code = str(_pick(values, "任务编号", "task_id", "task", "task_code") or "").strip()
        tasks = store.rows("reportsch")
        if task_code:
            return next((row for row in tasks if str(row.get("任务编号")) == task_code), None)
        if service is not None:
            dataset = str(service.get("数据集名称") or "")
            by_name = next((row for row in tasks if str(row.get("报表名称")) == dataset), None)
            if by_name is not None:
                return by_name
            service_key = metric_key(service.get("返回字段"))
            if service_key:
                by_fields = next(
                    (row for row in tasks if metric_key(_task_fields(row)) == service_key),
                    None,
                )
                if by_fields is not None:
                    return by_fields
        if len(tasks) == 1:
            return tasks[0]
        return None

    def find_service_for_task(self, task: dict[str, Any]) -> dict[str, Any] | None:
        services = store.rows("delivery")
        name = str(task.get("报表名称") or "")
        for row in services:
            if str(row.get("数据集名称") or "") == name:
                return row
        task_key = metric_key(_task_fields(task))
        for row in services:
            if task_key and metric_key(row.get("返回字段")) == task_key:
                return row
        return None

    # ---------- 产物与片段 ----------

    def _find_artifact(self, task_code: str, data_date: str, metrics_key: str) -> dict[str, Any] | None:
        for row in store.rows(ARTIFACT):
            if (
                str(row.get("任务编号")) == task_code
                and str(row.get("数据日期")) == data_date
                and metric_key(row.get("指标集合")) == metrics_key
            ):
                return row
        return None

    def _find_confirmed_other(self, task_code: str, data_date: str, metrics_key: str) -> dict[str, Any] | None:
        winners = [
            row
            for row in store.rows(ARTIFACT)
            if str(row.get("任务编号")) != task_code
            and str(row.get("数据日期")) == data_date
            and metric_key(row.get("指标集合")) == metrics_key
            and is_confirmed_complete(row)
        ]
        return min(winners, key=lambda row: int(row.get("id", 0)), default=None)

    def _artifact_fragments(self, metrics: list[str], failed: set[str], previous: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        previous_by_metric = {
            str(fragment.get("指标")): fragment
            for fragment in (previous or [])
            if fragment.get("指标") is not None
        }
        fragments: list[dict[str, Any]] = []
        for index, metric in enumerate(metrics, start=1):
            old = previous_by_metric.get(metric)
            # 已确认的成功结果永远保留；只有未确认片段会被重试或标记失败。
            if old is not None and old.get("状态") == FRAGMENT_CONFIRMED:
                status = FRAGMENT_CONFIRMED
            elif metric in failed:
                status = FRAGMENT_FAILED
            else:
                status = FRAGMENT_CONFIRMED
            fragments.append({"片段序号": index, "指标": metric, "状态": status})
        return fragments

    @staticmethod
    def _fragments_complete(fragments: list[dict[str, Any]]) -> bool:
        return bool(fragments) and all(fragment.get("状态") == FRAGMENT_CONFIRMED for fragment in fragments)

    @staticmethod
    def _artifact_summary(artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            "产物id": artifact.get("id"),
            "任务编号": artifact.get("任务编号"),
            "数据日期": artifact.get("数据日期"),
            "指标集合": artifact.get("指标集合"),
            "状态": artifact.get("状态"),
            "是否确认": bool(artifact.get("是否确认")),
            "版本号": artifact.get("版本") or "",
            "来源任务": artifact.get("来源任务") or artifact.get("任务编号"),
            "来源版本": artifact.get("来源版本") or "",
            "冲突原因": artifact.get("冲突原因"),
            "片段": [dict(fragment) for fragment in artifact.get("片段", [])],
        }

    def _apply_artifact(
        self,
        *,
        task: dict[str, Any],
        data_date: str,
        metrics: list[str],
        metrics_key: str,
        batch_id: str,
        failed: set[str],
    ) -> tuple[dict[str, Any], str, bool]:
        """落地一个日期/指标集合的判定，返回 (产物, 动作, 是否本次新完成)。"""
        existing = self._find_artifact(_task_code(task), data_date, metrics_key)
        other = self._find_confirmed_other(_task_code(task), data_date, metrics_key)
        decision = decide_artifact(
            data_date=data_date,
            metric_set=metrics_key,
            version=str((existing or other or {}).get("版本") or ""),
            existing=existing,
            other_confirmed=other,
        )

        if decision.action == "reuse":
            if other is not None and not is_confirmed_complete(existing):
                # 并发冲突：本任务尚未确认的产物转成复用，并保留冲突原因。
                if existing is None:
                    existing = store.append(ARTIFACT, {
                        "任务编号": _task_code(task),
                        "数据日期": data_date,
                        "指标集合": metrics_key,
                        "指标列表": list(metrics),
                        "片段": [],
                    })
                existing.update({
                    "状态": STATUS_REUSED,
                    "是否确认": True,
                    "来源任务": str(other.get("任务编号") or ""),
                    "复用产物id": int(other.get("id") or 0),
                    "冲突原因": decision.conflict_reason or decision.reason,
                    "来源版本": str(other.get("版本") or ""),
                    "版本": str(other.get("版本") or ""),
                })
            artifact = existing
            if artifact is not None:
                artifact["复用标记"] = True
                artifact["最近批次号"] = batch_id
                artifact["最近动作"] = "复用"
            return artifact, "reuse", False

        if existing is None:
            existing = store.append(ARTIFACT, {
                "id": store.next_id(ARTIFACT),
                "任务编号": _task_code(task),
                "数据日期": data_date,
                "指标集合": metrics_key,
                "指标列表": list(metrics),
                "版本": "",
                "状态": STATUS_PENDING,
                "是否确认": False,
                "来源任务": _task_code(task),
                "复用产物id": None,
                "冲突原因": None,
                "重试次数": 0,
                "片段": [],
            })
        else:
            for key in ("指标列表", "来源任务", "复用产物id", "冲突原因"):
                existing.setdefault(key, None)
            existing["指标列表"] = list(metrics)

        limit = _retry_limit(task)
        attempts = int(existing.get("重试次数") or 0)
        if attempts >= limit:
            existing["状态"] = STATUS_PENDING
            existing["是否确认"] = False
            existing["最近批次号"] = batch_id
            existing["最近动作"] = "达到重试上限"
            return existing, "limit", False

        existing["片段"] = self._artifact_fragments(metrics, failed, existing.get("片段"))
        existing["重试次数"] = attempts + 1
        existing["最近批次号"] = batch_id
        existing["最近动作"] = "重试" if decision.action == "retry" else "生成"
        completed = self._fragments_complete(existing["片段"])
        existing["状态"] = STATUS_COMPLETED if completed else STATUS_PENDING
        existing["是否确认"] = completed
        if completed:
            existing["冲突原因"] = None
        return existing, decision.action, completed

    # ---------- 版本与下载 ----------

    def _versions(self, service_code: str) -> list[dict[str, Any]]:
        return [row for row in store.rows(VERSION) if str(row.get("服务编号")) == service_code]

    def _current_version(self, service_code: str) -> dict[str, Any] | None:
        versions = self._versions(service_code)
        confirmed = [row for row in versions if row.get("是否确认")]
        return max(confirmed, key=lambda row: int(row.get("id", 0)), default=None)

    def _confirmed_task_artifacts(self, task: dict[str, Any] | str) -> list[dict[str, Any]]:
        if isinstance(task, dict):
            task_code = _task_code(task)
            metrics_key = metric_key(_task_metrics(task))
        else:
            task_code = str(task)
            metrics_key = ""
        artifacts = [
            row
            for row in store.rows(ARTIFACT)
            if str(row.get("任务编号")) == task_code
            and is_confirmed_complete(row)
            and (not metrics_key or metric_key(row.get("指标集合")) == metrics_key)
        ]
        artifacts.sort(key=lambda row: (str(row.get("数据日期")), int(row.get("id", 0))))
        return artifacts

    def _task_ready_for_version(self, task: dict[str, Any]) -> bool:
        """只有任务内全部计划日期和全部片段都确认后才允许推进版本。"""
        task_code = _task_code(task)
        metrics_key = metric_key(_task_metrics(task))
        artifacts = [
            row
            for row in store.rows(ARTIFACT)
            if str(row.get("任务编号")) == task_code
            and (not metrics_key or metric_key(row.get("指标集合")) == metrics_key)
        ]
        if any(not is_confirmed_complete(row) for row in artifacts):
            return False
        planned = set(_parse_dates(task.get("数据日期列表")))
        if planned:
            confirmed_dates = {str(row.get("数据日期")) for row in artifacts if is_confirmed_complete(row)}
            if not planned.issubset(confirmed_dates):
                return False
        return bool(artifacts)

    def _commit_version(
        self,
        *,
        task: dict[str, Any],
        service: dict[str, Any] | None,
        service_code: str,
        batch_id: str,
    ) -> tuple[dict[str, Any], bool, str]:
        artifacts = self._confirmed_task_artifacts(task)
        artifact_ids = []
        for artifact in artifacts:
            artifact_id = int(artifact.get("id") or 0)
            if artifact_id and artifact_id not in artifact_ids:
                artifact_ids.append(artifact_id)
        current = self._current_version(service_code)
        if current is not None and list(current.get("产物id列表") or []) == artifact_ids:
            return current, False, "已确认产物集合与当前版本一致，无需重复推进版本"

        next_index = len(self._versions(service_code)) + 1
        version_code = f"{service_code}-v{next_index}"
        fields = _task_fields(task)
        version_row = store.append(VERSION, {
            "服务编号": service_code,
            "版本号": version_code,
            "任务编号": _task_code(task),
            "状态": "已确认",
            "是否确认": True,
            "批次号": batch_id,
            "返回字段": fields,
            "产物id列表": artifact_ids,
            "页大小": int(service.get("分页最大条数") or 20) if service else 20,
        })
        for artifact in artifacts:
            if artifact.get("状态") == STATUS_REUSED:
                artifact.setdefault("来源版本", str(artifact.get("版本") or ""))
                artifact["版本"] = version_code
            elif not artifact.get("版本"):
                artifact["版本"] = version_code
        self._ensure_downloads(task, artifacts, version_code)
        return version_row, True, f"全部交付片段已确认，服务版本推进到 {version_code}"

    def _ensure_downloads(self, task: dict[str, Any], artifacts: list[dict[str, Any]], version_code: str) -> None:
        for artifact in artifacts:
            artifact_id = int(artifact.get("id") or 0)
            if artifact.get("状态") == STATUS_REUSED:
                # 复用产物沿用来源任务的下载记录，不再生成第二条。
                continue
            exists = any(
                int(row.get("产物id") or 0) == artifact_id
                for row in store.rows(DOWNLOAD)
            )
            if exists:
                continue
            # 只对最终确认的产物生成下载记录，且以产物 id 去重，重试不会重复写。
            store.append(DOWNLOAD, {
                "任务编号": _task_code(task),
                "数据日期": str(artifact.get("数据日期") or ""),
                "指标集合": str(artifact.get("指标集合") or ""),
                "产物id": artifact_id,
                "版本号": version_code,
            })

    # ---------- 批次 ----------

    def _batch_key(self, task_code: str, dates: list[str], metrics_key: str, values: dict[str, Any],
                   failed: set[str] | None = None) -> str:
        explicit = str(_pick(values, "批次号", "batch_id", "batch", default="") or "").strip()
        if explicit:
            return explicit
        # 未显式给批次号时使用确定性批次号：相同重试请求天然只生效一次。
        failed_key = ",".join(sorted(failed or ()))
        raw = f"{task_code}|{','.join(dates)}|{metrics_key}|{failed_key}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        readable_dates = ",".join(dates)
        return f"retry-{task_code}-{readable_dates}-{digest}"

    def _find_batch(self, batch_id: str) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows(BATCH) if str(row.get("批次号")) == batch_id),
            None,
        )

    def _batch_response(self, batch: dict[str, Any], *, idempotent: bool) -> dict[str, Any]:
        stats = _stats_payload(dict(batch.get("统计") or {}))
        cumulative = _stats_payload(self._task_stats(str(batch.get("任务编号") or "")))
        payload = {
            "ok": True,
            "message": "批次已执行过，本次仅返回既有结果（幂等生效）" if idempotent else batch.get("说明"),
            "批次号": batch.get("批次号"),
            "幂等": idempotent,
            "任务编号": batch.get("任务编号"),
            "服务编号": batch.get("服务编号"),
            "数据日期": list(batch.get("数据日期") or []),
            "指标集合": list(batch.get("指标集合") or []),
            "返回字段": list(batch.get("返回字段") or []),
            "统计": stats,
            "任务统计": cumulative,
            "已完成": stats["已完成"],
            "待重试": stats["待重试"],
            "复用": stats["复用"],
            "产物": [dict(item) for item in batch.get("产物快照", [])],
            "待重试日期": list(batch.get("待重试日期") or []),
            "冲突": [dict(item) for item in batch.get("冲突") or []],
            "版本号": batch.get("版本号") or "",
            "版本已更新": bool(batch.get("版本已更新")),
            "版本状态": batch.get("版本状态") or "",
            "说明": batch.get("说明"),
        }
        return payload

    def run_retry_batch(
        self,
        *,
        service: dict[str, Any] | None = None,
        task: dict[str, Any] | None = None,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        with _BATCH_LOCK:
            return self._run_retry_batch(service=service, task=task, values=values)

    def _run_retry_batch(
        self,
        *,
        service: dict[str, Any] | None = None,
        task: dict[str, Any] | None = None,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        values = _flatten_payload(values)
        task = self.resolve_task(service=service, task=task, values=values)
        if task is None:
            return {"ok": False, "message": "未找到对应的报表任务，无法确定排期版本", "统计": _EMPTY_STATS}
        if str(task.get("status")) == "已暂停":
            return {"ok": False, "message": f"报表任务 {_task_code(task)} 已暂停调度，不能发起失败续做", "统计": _EMPTY_STATS}
        service = service or self.find_service_for_task(task)
        service_code = _service_code(service, task)

        task_fields = _task_fields(task)
        if service is not None:
            service_fields = ordered_metrics(service.get("返回字段"))
            if service_fields and metric_key(service_fields) != metric_key(task_fields):
                return {
                    "ok": False,
                    "message": "数据服务返回字段与报表任务排期结果不一致，请先对齐排期版本",
                    "返回字段": service_fields,
                    "排期返回字段": task_fields,
                    "统计": _EMPTY_STATS,
                }

        requested_metrics = ordered_metrics(_pick(values, "指标集合", "metrics", "metric_set")) or _task_metrics(task)
        task_metrics = _task_metrics(task)
        if task_metrics and not set(requested_metrics).issubset(set(task_metrics)):
            return {
                "ok": False,
                "message": "请求的指标集合超出报表任务排期范围",
                "指标集合": requested_metrics,
                "排期指标": task_metrics,
                "统计": _EMPTY_STATS,
            }
        metrics_key = metric_key(requested_metrics)

        dates_value = _pick(values, "数据日期", "数据日期列表", "dates", "data_dates")
        if dates_value in (None, ""):
            planned = task.get("数据日期列表")
            if planned:
                dates_value = planned
            else:
                # 没有显式日期时按运行日期减去数据日期偏移得到应补的数据日期。
                dates_value = None
        dates = _parse_dates(
            dates_value,
            reference=_reference_date(values),
            offset=_offset_days(task, values),
        )
        if not dates:
            return {"ok": False, "message": "没有可续做的数据日期", "统计": _EMPTY_STATS}

        failed = set(canonical_metrics(_pick(values, "失败片段", "失败指标", "fail_fragments", "failed_fragments")))
        batch_id = self._batch_key(_task_code(task), dates, metrics_key, values, failed)
        existing_batch = self._find_batch(batch_id)
        if existing_batch is not None:
            if str(existing_batch.get("任务编号")) != _task_code(task):
                return {
                    "ok": False,
                    "message": f"批次号 {batch_id} 已被任务 {existing_batch.get('任务编号')} 占用",
                    "统计": _EMPTY_STATS,
                }
            return self._batch_response(existing_batch, idempotent=True)

        completed = 0
        pending = 0
        reused = 0
        generated = 0
        conflicts: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        artifact_ids: list[int] = []
        pending_dates: list[str] = []
        snapshots: list[dict[str, Any]] = []

        for data_date in dates:
            artifact, action, newly_completed = self._apply_artifact(
                task=task,
                data_date=data_date,
                metrics=requested_metrics,
                metrics_key=metrics_key,
                batch_id=batch_id,
                failed=failed,
            )
            if artifact is None:
                continue
            artifact_id = int(artifact.get("id") or 0)
            if artifact_id:
                artifact_ids.append(artifact_id)
            if action == "reuse":
                reused += 1
            elif action == "limit":
                pass
            elif newly_completed:
                completed += 1
                generated += 1
            else:
                pending += 1

            if artifact.get("状态") == STATUS_PENDING or not artifact.get("是否确认"):
                pending_dates.append(data_date)
            if artifact.get("冲突原因"):
                conflicts.append({
                    "数据日期": data_date,
                    "任务编号": _task_code(task),
                    "来源任务": artifact.get("来源任务"),
                    "产物id": artifact_id,
                    "冲突原因": artifact.get("冲突原因"),
                })
            decisions.append({
                "数据日期": data_date,
                "指标集合": metrics_key,
                "动作": action,
                "产物id": artifact_id,
                "状态": artifact.get("状态"),
                "说明": (
                    f"达到重试次数上限 {_retry_limit(task)}，保留已成功片段并等待人工处理"
                    if action == "limit"
                    else artifact.get("冲突原因") or ""
                ),
            })
            snapshots.append(self._artifact_summary(artifact))

        all_complete = bool(snapshots) and all(bool(item.get("是否确认")) for item in snapshots)
        version_code = ""
        version_updated = False
        version_status = "批次仍有未确认片段，服务版本保持不变"
        if all_complete and self._task_ready_for_version(task):
            version_row, version_updated, version_status = self._commit_version(
                task=task,
                service=service,
                service_code=service_code,
                batch_id=batch_id,
            )
            version_code = str((version_row or {}).get("版本号") or "")
        else:
            if all_complete:
                version_status = "本批次片段已全部确认，但报表任务仍有其他未确认日期，服务版本暂不推进"
            current = self._current_version(service_code)
            version_code = str((current or {}).get("版本号") or "")

        stats = {
            "已完成": completed,
            "待重试": len(pending_dates),
            "复用": reused,
            "生成": generated,
            "冲突复用": len(conflicts),
            "产物总数": len(snapshots),
        }
        message = (
            f"批次 {batch_id} 完成：新增完成 {completed} 个、复用 {reused} 个、待重试 {len(pending_dates)} 个"
        )
        batch_row = store.append(BATCH, {
            "批次号": batch_id,
            "任务编号": _task_code(task),
            "服务编号": service_code,
            "数据日期": dates,
            "指标集合": requested_metrics,
            "返回字段": task_fields,
            "状态": BATCH_COMPLETED if all_complete else BATCH_PENDING,
            "统计": stats,
            "产物id列表": sorted(set(artifact_ids)),
            "产物快照": snapshots,
            "决策": decisions,
            "待重试日期": pending_dates,
            "冲突": conflicts,
            "版本号": version_code,
            "版本已更新": version_updated,
            "版本状态": version_status,
            "说明": message,
            "幂等": False,
        })
        return self._batch_response(batch_row, idempotent=False)

    # ---------- 状态 ----------

    def _task_stats(self, task_code: str) -> dict[str, int]:
        artifacts = [row for row in store.rows(ARTIFACT) if str(row.get("任务编号")) == task_code]
        completed = sum(
            1 for row in artifacts
            if row.get("状态") == STATUS_COMPLETED and row.get("是否确认")
        )
        pending = sum(
            1 for row in artifacts
            if not row.get("是否确认") and row.get("状态") != STATUS_REUSED
        )
        reused = sum(1 for row in artifacts if row.get("状态") == STATUS_REUSED)
        return {
            "已完成": completed,
            "待重试": pending,
            "复用": reused,
            "产物总数": len(artifacts),
        }

    def status(self, *, service: dict[str, Any] | None = None, task: dict[str, Any] | None = None,
               values: dict[str, Any] | None = None) -> dict[str, Any]:
        values = _flatten_payload(values or {})
        task = self.resolve_task(service=service, task=task, values=values)
        if task is None:
            return {
                "ok": False,
                "message": "未找到对应的报表任务，无法汇总续做状态",
                "统计": _stats_payload({"已完成": 0, "待重试": 0, "复用": 0}),
            }
        service = service or self.find_service_for_task(task)
        service_code = _service_code(service, task)
        current = self._current_version(service_code)
        versions = sorted(self._versions(service_code), key=lambda row: int(row.get("id", 0)))
        tasks_artifacts = [
            self._artifact_summary(row)
            for row in store.rows(ARTIFACT)
            if str(row.get("任务编号")) == _task_code(task)
        ]
        downloads = [
            row for row in store.rows(DOWNLOAD)
            if str(row.get("任务编号")) == _task_code(task)
        ]
        batches = [
            row for row in store.rows(BATCH)
            if str(row.get("任务编号")) == _task_code(task)
        ]
        return {
            "ok": True,
            "任务编号": _task_code(task),
            "服务编号": service_code,
            "统计": _stats_payload(self._task_stats(_task_code(task))),
            "返回字段": _task_fields(task),
            "当前版本号": str((current or {}).get("版本号") or ""),
            "版本": {
                "当前版本号": str((current or {}).get("版本号") or ""),
                "已确认": bool(current and current.get("是否确认")),
                "产物数": len((current or {}).get("产物id列表") or []),
                "页大小": int((current or {}).get("页大小") or (service or {}).get("分页最大条数") or 20),
                "历史版本": [str(row.get("版本号")) for row in versions],
            },
            "产物": tasks_artifacts,
            "下载记录数": len(downloads),
            "重复下载记录数": self._duplicate_downloads(downloads),
            "downloaded_records": len(downloads),
            "duplicate_download_records": self._duplicate_downloads(downloads),
            "批次": [
                {
                    "批次号": row.get("批次号"),
                    "状态": row.get("状态"),
                    "统计": _stats_payload(dict(row.get("统计") or {})),
                    "版本号": row.get("版本号"),
                    "版本已更新": row.get("版本已更新"),
                }
                for row in batches
            ],
        }

    @staticmethod
    def _duplicate_downloads(rows: list[dict[str, Any]]) -> int:
        seen: set[int] = set()
        duplicates = 0
        for row in rows:
            artifact_id = int(row.get("产物id") or 0)
            if artifact_id in seen:
                duplicates += 1
            seen.add(artifact_id)
        return duplicates

    def artifacts(self, *, service: dict[str, Any] | None = None, task: dict[str, Any] | None = None) -> dict[str, Any]:
        task = self.resolve_task(service=service, task=task)
        if task is None:
            return {"ok": False, "message": "未找到对应的报表任务", "items": []}
        items = [
            self._artifact_summary(row)
            for row in store.rows(ARTIFACT)
            if str(row.get("任务编号")) == _task_code(task)
        ]
        return {
            "ok": True,
            "任务编号": _task_code(task),
            "返回字段": _task_fields(task),
            "items": items,
            "total": len(items),
        }

    def downloads(self, *, service: dict[str, Any] | None = None, task: dict[str, Any] | None = None) -> dict[str, Any]:
        task = self.resolve_task(service=service, task=task)
        if task is None:
            return {"ok": False, "message": "未找到对应的报表任务", "items": []}
        rows = [
            dict(row) for row in store.rows(DOWNLOAD)
            if str(row.get("任务编号")) == _task_code(task)
        ]
        return {
            "ok": True,
            "任务编号": _task_code(task),
            "items": rows,
            "total": len(rows),
            "重复记录数": self._duplicate_downloads(rows),
        }

    def batches(self, *, service: dict[str, Any] | None = None, task: dict[str, Any] | None = None) -> dict[str, Any]:
        task = self.resolve_task(service=service, task=task)
        if task is None:
            return {"ok": False, "message": "未找到对应的报表任务", "items": []}
        rows = [
            {
                "批次号": row.get("批次号"),
                "任务编号": row.get("任务编号"),
                "服务编号": row.get("服务编号"),
                "数据日期": list(row.get("数据日期") or []),
                "指标集合": list(row.get("指标集合") or []),
                "状态": row.get("状态"),
                "统计": _stats_payload(dict(row.get("统计") or {})),
                "待重试日期": list(row.get("待重试日期") or []),
                "冲突": [dict(item) for item in row.get("冲突") or []],
                "版本号": row.get("版本号") or "",
                "版本已更新": bool(row.get("版本已更新")),
            }
            for row in store.rows(BATCH)
            if str(row.get("任务编号")) == _task_code(task)
        ]
        return {"ok": True, "任务编号": _task_code(task), "items": rows, "total": len(rows)}

    # ---------- 页段 ----------

    def page_segments(
        self,
        *,
        service: dict[str, Any] | None = None,
        task: dict[str, Any] | None = None,
        page: int = 1,
        size: int | None = None,
        version: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        with _BATCH_LOCK:
            return self._page_segments(service=service, task=task, page=page, size=size, version=version)

    def _page_segments(
        self,
        *,
        service: dict[str, Any] | None = None,
        task: dict[str, Any] | None = None,
        page: int = 1,
        size: int | None = None,
        version: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        task = self.resolve_task(service=service, task=task)
        if task is None:
            return None, "未找到对应的报表任务，无法读取页段"
        service = service or self.find_service_for_task(task)
        service_code = _service_code(service, task)

        versions = self._versions(service_code)
        page_size = int(size or (service or {}).get("分页最大条数") or 20)
        page_size = max(min(page_size, 200), 1)
        page = max(int(page or 1), 1)

        if not versions:
            current_artifacts = self._confirmed_task_artifacts(task)
            if not current_artifacts:
                return None, "暂无可读的已确认版本"
            # 没有落库版本时，以当前已确认产物作为一次性快照，仍钉住本次读取。
            version_row = {
                "版本号": f"{service_code}-snapshot",
                "返回字段": _task_fields(task),
                "产物id列表": [int(row.get("id") or 0) for row in current_artifacts],
                "页大小": page_size,
                "任务编号": _task_code(task),
            }
            available = ["", str(version_row["版本号"])]
            current_version = str(version_row["版本号"])
        else:
            current = self._current_version(service_code)
            if current is None:
                return None, "当前没有已确认的服务版本，不能读取页段"
            version_row = current
            current_version = str(current.get("版本号") or "")
            available = [str(row.get("版本号") or "") for row in versions if row.get("是否确认")]

        if page > 1 and not version:
            return None, "续读页段必须携带首次请求返回的版本号，不能跨版本拼接"
        decision = decide_page(
            requested_version=version,
            current_version=current_version,
            available_versions=available,
            page=page,
        )
        if decision.action == "reject":
            return None, decision.reason

        selected_version = decision.version or current_version
        snapshot = next(
            (row for row in versions if str(row.get("版本号")) == selected_version),
            version_row if selected_version == str(version_row.get("版本号")) else None,
        )
        if snapshot is None:
            return None, f"版本 {selected_version} 不存在，不能用其他版本续读当前页段"

        fields = ordered_metrics(snapshot.get("返回字段")) or _task_fields(task)
        if service is not None:
            service_fields = ordered_metrics(service.get("返回字段"))
            if service_fields and metric_key(service_fields) != metric_key(fields):
                return None, "数据服务返回字段与排期结果不一致，不能按该版本交付"

        artifacts: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for artifact_id in snapshot.get("产物id列表") or []:
            artifact = store.find(ARTIFACT, int(artifact_id or 0))
            unique_key = (
                str((artifact or {}).get("任务编号") or ""),
                str((artifact or {}).get("数据日期") or ""),
                str((artifact or {}).get("指标集合") or ""),
            )
            if artifact is None or unique_key in seen_keys:
                continue
            if not is_confirmed_complete(artifact):
                continue
            seen_keys.add(unique_key)
            artifacts.append(artifact)
        artifacts.sort(key=lambda row: (str(row.get("数据日期")), int(row.get("id", 0))))

        total = len(artifacts)
        start = (page - 1) * page_size
        page_artifacts = artifacts[start:start + page_size]
        items = []
        for artifact in page_artifacts:
            item: dict[str, Any] = {
                "产物id": artifact.get("id"),
                "任务编号": artifact.get("任务编号"),
                "数据日期": artifact.get("数据日期"),
                "指标集合": artifact.get("指标集合"),
                "版本号": selected_version,
            }
            for field in fields:
                item[field] = f"{artifact.get('任务编号')}-{artifact.get('数据日期')}-{field}"
            items.append(item)

        next_page = page + 1 if start + page_size < total else None
        return {
            "ok": True,
            "服务编号": service_code,
            "任务编号": _task_code(task),
            "版本号": selected_version,
            "首次请求版本": version or selected_version,
            "当前版本号": current_version,
            "版本切换": bool(version and version != current_version),
            "续读说明": decision.reason,
            "返回字段": fields,
            "items": items,
            "total": total,
            "page": page,
            "size": page_size,
            "next_page": next_page,
            "下一页码": next_page,
            "续读版本号": selected_version,
        }, ""

    # ---------- 失效范围 ----------

    def invalidation_scope(self, task: dict[str, Any], changed_fields: Any) -> dict[str, Any]:
        changed = canonical_metrics(changed_fields if isinstance(changed_fields, (list, tuple, set)) else str(changed_fields)) if changed_fields else []
        artifacts = [row for row in store.rows(ARTIFACT) if str(row.get("任务编号")) == _task_code(task)]
        service = self.find_service_for_task(task)
        service_code = _service_code(service, task)
        versions = self._versions(service_code)

        if any(field in {"包含指标", "指标集合"} for field in changed):
            invalid_artifacts = artifacts
            invalid_versions = versions
            scope = "指标集合变更：该任务全部产物和已确认服务版本失效"
        elif any(field in {"返回字段", "查询参数定义"} for field in changed):
            invalid_artifacts = []
            invalid_versions = versions
            scope = "返回字段变更：产物数据仍可复用，但已确认服务版本需按新字段重新交付"
        elif any(field in {"数据日期偏移", "数据日期列表"} for field in changed):
            planned_dates = set(_parse_dates(task.get("数据日期列表")))
            invalid_artifacts = [
                row for row in artifacts
                if planned_dates and str(row.get("数据日期")) not in planned_dates
            ]
            invalid_versions = []
            scope = "数据日期范围变更：仅排期范围外的数据日期产物失效，已确认版本保持"
        else:
            invalid_artifacts = []
            invalid_versions = []
            scope = "调度参数变更不涉及产物与返回字段，已确认版本和页段不受影响"

        invalid_ids = {int(row.get("id") or 0) for row in invalid_artifacts}
        keep_artifacts = [row for row in artifacts if int(row.get("id") or 0) not in invalid_ids]
        return {
            "ok": True,
            "任务编号": _task_code(task),
            "服务编号": service_code,
            "变更字段": changed,
            "失效范围": scope,
            "失效产物": [self._artifact_summary(row) for row in invalid_artifacts],
            "失效版本": [str(row.get("版本号")) for row in invalid_versions],
            "保留产物": [self._artifact_summary(row) for row in keep_artifacts],
            "分页影响": "已开始的页段继续按首次请求版本读完；新页段请求使用变更后重新确认的版本",
        }

    # ---------- 概览 ----------

    def resume_overview(self) -> dict[str, Any]:
        services = []
        totals = {"已完成": 0, "待重试": 0, "复用": 0, "产物总数": 0}
        for service in store.rows("delivery"):
            task = self.resolve_task(service=service)
            if task is None:
                continue
            stats = self._task_stats(_task_code(task))
            for key in totals:
                totals[key] += int(stats.get(key) or 0)
            current = self._current_version(str(service.get("服务编号") or ""))
            services.append({
                "服务编号": service.get("服务编号"),
                "任务编号": _task_code(task),
                "统计": _stats_payload(stats),
                "当前版本号": str((current or {}).get("版本号") or ""),
                "返回字段": _task_fields(task),
            })
        return {"ok": True, "统计": _stats_payload(totals), "服务": services}


report_data_service = ReportDataService()
