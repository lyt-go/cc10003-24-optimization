"""报表数据服务统一入口：失败续做、状态、页段与失效范围。

同一套续做逻辑同时挂在 `/api/reportsch`（按报表任务）和 `/api/delivery`
（按数据服务）下，这里再提供一个不区分入口的聚合路由，方便以数据服务为
业务入口直接调用。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query

from app.services.report_data import report_data_service

router = APIRouter(prefix="/api/report-data", tags=["报表数据服务"])


def _resolve(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    service = report_data_service.resolve_service_ref(
        payload.get("服务编号") or payload.get("service_id") or payload.get("service")
    )
    task = report_data_service.resolve_task_ref(
        payload.get("任务编号") or payload.get("task_id") or payload.get("task")
    )
    return service, task


@router.post("/retry")
def retry(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """执行失败续做批次；相同批次号或相同请求参数重复执行只生效一次。"""
    service, task = _resolve(payload)
    return report_data_service.run_retry_batch(service=service, task=task, values=payload)


@router.post("/retry-batches")
def retry_batches(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """失败续做批次的兼容入口。"""
    return retry(payload)


@router.get("/status")
def status(
    service_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """查看续做状态：已完成、待重试、复用数量与当前已确认版本。"""
    service = report_data_service.resolve_service_ref(service_id)
    task = report_data_service.resolve_task_ref(task_id)
    return report_data_service.status(service=service, task=task)


@router.get("/overview")
def overview() -> dict[str, Any]:
    """汇总各数据服务的已完成、待重试、复用数量。"""
    return report_data_service.resume_overview()


@router.get("/pages")
def pages(
    service_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    page: int = 1,
    size: int | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """按版本读取页段；page > 1 时必须携带首次请求返回的版本号。"""
    service = report_data_service.resolve_service_ref(service_id)
    task = report_data_service.resolve_task_ref(task_id)
    payload, message = report_data_service.page_segments(
        service=service, task=task, page=page, size=size, version=version
    )
    if payload is None:
        return {"ok": False, "message": message}
    return payload


@router.get("/retry-batches/list")
def list_batches(
    service_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """列出续做批次及其统计。"""
    service = report_data_service.resolve_service_ref(service_id)
    task = report_data_service.resolve_task_ref(task_id)
    return report_data_service.batches(service=service, task=task)


@router.get("/artifacts")
def artifacts(
    service_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """列出续做产物与片段确认状态。"""
    service = report_data_service.resolve_service_ref(service_id)
    task = report_data_service.resolve_task_ref(task_id)
    return report_data_service.artifacts(service=service, task=task)


@router.get("/downloads")
def downloads(
    service_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """列出下载记录与重复记录数。"""
    service = report_data_service.resolve_service_ref(service_id)
    task = report_data_service.resolve_task_ref(task_id)
    return report_data_service.downloads(service=service, task=task)


@router.post("/invalidation-scope")
def invalidation_scope(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """按变更字段判断产物、版本与页段的失效范围。"""
    service, task = _resolve(payload)
    task = task or report_data_service.resolve_task(service=service)
    if task is None:
        return {"ok": False, "message": "未找到对应的报表任务"}
    changed = payload.get("变更字段") or payload.get("changed_fields") or []
    return report_data_service.invalidation_scope(task, changed)
