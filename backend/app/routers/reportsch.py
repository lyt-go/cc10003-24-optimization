"""报表调度接口：维护报表任务，覆盖提交调度评审、确认排期、暂停调度等动作。

失败续做入口也挂在这里：按报表任务发起重试批次、查看产物/版本状态，
并可按首次请求版本续读服务页段。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.report_data import report_data_service
from app.services.reportsch import ReportschService

router = APIRouter(prefix="/api/reportsch", tags=["报表调度"])

service = ReportschService()

LIST_FIELDS = ["任务编号", "报表名称", "包含指标", "输出格式", "调度cron", "收件人列表", "数据日期偏移", "重试次数上限"]
STATUSES = ["草稿", "调度评审中", "已排期", "已暂停"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出报表调度清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "reportsch", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="草稿、调度评审中、已排期、已暂停"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤报表调度列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条报表任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="报表任务已登记", entry=entry)


@router.post("/resume/retry")
def run_retry_batch(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """按任务编号发起失败续做批次；相同批次重复提交只生效一次。"""
    task = report_data_service.resolve_task_ref(
        payload.get("任务编号") or payload.get("task_id") or payload.get("task")
    )
    if payload.get("任务编号") or payload.get("task_id") or payload.get("task"):
        if task is None:
            return {"ok": False, "message": "报表任务不存在", "统计": {"已完成": 0, "待重试": 0, "复用": 0}}
    return report_data_service.run_retry_batch(task=task, values=payload)


@router.get("/resume/overview")
def resume_overview() -> dict[str, Any]:
    """汇总各数据服务的已完成、待重试、复用数量。"""
    return report_data_service.resume_overview()


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    """读取单条报表任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"报表任务 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条报表任务执行提交调度评审、确认排期、暂停调度；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    if action in {"重试失败批次", "失败续做", "重试报表", "续做"}:
        task = service.get_entry(entry_id)
        if task is None:
            return ActionResult(ok=False, message=f"报表任务 {entry_id} 不存在或已归档")
        result = report_data_service.run_retry_batch(task=task, values=payload.values)
        return ActionResult(ok=bool(result.get("ok")), message=str(result.get("message") or ""), entry=result)
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/resume")
@router.get("/{entry_id}/resume/status")
def resume_status(entry_id: int) -> dict[str, Any]:
    """查看报表任务的续做状态：已完成、待重试、复用数量以及当前已确认版本。"""
    task = service.get_entry(entry_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"报表任务 {entry_id} 不存在或已归档")
    return report_data_service.status(task=task)


@router.post("/{entry_id}/resume/retry")
@router.post("/{entry_id}/retry-batches")
@router.post("/{entry_id}/retry")
def retry_for_entry(entry_id: int, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """按报表任务 id 发起失败续做。"""
    task = service.get_entry(entry_id)
    if task is None:
        return {"ok": False, "message": f"报表任务 {entry_id} 不存在或已归档", "统计": {"已完成": 0, "待重试": 0, "复用": 0}}
    return report_data_service.run_retry_batch(task=task, values=payload)


@router.get("/{entry_id}/retry-batches/list")
def list_batches(entry_id: int) -> dict[str, Any]:
    """列出该任务的续做批次及其统计。"""
    task = service.get_entry(entry_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"报表任务 {entry_id} 不存在或已归档")
    return report_data_service.batches(task=task)


@router.get("/{entry_id}/artifacts")
def list_artifacts(entry_id: int) -> dict[str, Any]:
    """列出报表任务的产物及其片段确认状态。"""
    task = service.get_entry(entry_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"报表任务 {entry_id} 不存在或已归档")
    return report_data_service.artifacts(task=task)


@router.get("/{entry_id}/downloads")
def list_downloads(entry_id: int) -> dict[str, Any]:
    """列出下载记录，重复记录数应始终为 0。"""
    task = service.get_entry(entry_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"报表任务 {entry_id} 不存在或已归档")
    return report_data_service.downloads(task=task)


@router.get("/{entry_id}/pages")
@router.get("/{entry_id}/segments")
def read_pages(
    entry_id: int,
    page: int = 1,
    size: int | None = None,
    page_size: int | None = None,
    version: str | None = None,
    snapshot_version: str | None = None,
    read_version: str | None = None,
) -> dict[str, Any]:
    """按版本读取服务页段；续页必须带首次请求的版本号，禁止跨版本拼接。"""
    task = service.get_entry(entry_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"报表任务 {entry_id} 不存在或已归档")
    payload, message = report_data_service.page_segments(
        task=task,
        page=page,
        size=size or page_size,
        version=version or snapshot_version or read_version,
    )
    if payload is None:
        return {"ok": False, "message": message}
    return payload


@router.get("/{entry_id}/status")
def entry_status(entry_id: int) -> dict[str, Any]:
    """按 id 查看续做状态的简写入口。"""
    return resume_status(entry_id)


@router.post("/{entry_id}/invalidation-scope")
def invalidation_scope(entry_id: int, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """按变更字段判断产物、版本与页段的失效范围。"""
    task = service.get_entry(entry_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"报表任务 {entry_id} 不存在或已归档")
    changed = payload.get("变更字段") or payload.get("changed_fields") or []
    return report_data_service.invalidation_scope(task, changed)
