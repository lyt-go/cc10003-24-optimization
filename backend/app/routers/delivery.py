"""数据服务接口：维护服务版本，覆盖提交服务评审、发布服务、下线服务等动作。

报表数据服务以这里为业务入口：失败续做、状态汇总、产物、下载记录与页段读取
都按“数据服务 + 报表任务 + 同一排期版本”交付。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.delivery import DeliveryService
from app.services.report_data import report_data_service

router = APIRouter(prefix="/api/delivery", tags=["数据服务"])

service = DeliveryService()

LIST_FIELDS = ["服务编号", "数据集名称", "查询参数定义", "返回字段", "速率限制", "分页最大条数", "版本号", "消费方应用"]
STATUSES = ["草稿", "服务评审中", "已发布", "已下线"]


def _entry(entry_id: int) -> dict[str, Any]:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"服务版本 {entry_id} 不存在或已归档")
    return entry


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据服务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "delivery", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按服务编号检索"),
    status: str | None = Query(default=None, description="草稿、服务评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按服务编号与状态过滤数据服务列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条服务版本，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="服务版本已登记", entry=entry)


@router.post("/resume/retry")
def run_retry_batch(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """以数据服务为入口发起失败续做；相同批次重复提交只生效一次。"""
    ref = payload.get("服务编号") or payload.get("service_id") or payload.get("service")
    entry = report_data_service.resolve_service_ref(ref) if ref else None
    if ref and entry is None:
        return {"ok": False, "message": "数据服务不存在", "统计": {"已完成": 0, "待重试": 0, "复用": 0}}
    return report_data_service.run_retry_batch(service=entry, values=payload)


@router.get("/resume/overview")
def resume_overview() -> dict[str, Any]:
    """汇总各数据服务的已完成、待重试、复用数量。"""
    return report_data_service.resume_overview()


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    """读取单条服务版本明细；不存在时给出可读的错误说明。"""
    return _entry(entry_id)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条服务版本执行提交服务评审、发布服务、下线服务；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry = _entry(entry_id)
    if action in {"重试失败批次", "失败续做", "重试报表", "续做"}:
        result = report_data_service.run_retry_batch(service=entry, values=payload.values)
        return ActionResult(ok=bool(result.get("ok")), message=str(result.get("message") or ""), entry=result)
    updated, message = service.run_action(entry_id, action)
    if updated is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=updated)


@router.get("/{entry_id}/resume")
@router.get("/{entry_id}/resume/status")
def resume_status(entry_id: int) -> dict[str, Any]:
    """按数据服务查看续做状态：已完成、待重试、复用数量与当前排期版本。"""
    return report_data_service.status(service=_entry(entry_id))


@router.post("/{entry_id}/resume/retry")
@router.post("/{entry_id}/retry-batches")
@router.post("/{entry_id}/retry")
def retry_for_entry(entry_id: int, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """按数据服务 id 发起失败续做。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        return {"ok": False, "message": f"服务版本 {entry_id} 不存在或已归档", "统计": {"已完成": 0, "待重试": 0, "复用": 0}}
    return report_data_service.run_retry_batch(service=entry, values=payload)


@router.get("/{entry_id}/retry-batches/list")
def list_batches(entry_id: int) -> dict[str, Any]:
    """列出该服务的续做批次及其统计。"""
    return report_data_service.batches(service=_entry(entry_id))


@router.get("/{entry_id}/artifacts")
def list_artifacts(entry_id: int) -> dict[str, Any]:
    """列出数据服务对应任务的产物与片段确认状态。"""
    return report_data_service.artifacts(service=_entry(entry_id))


@router.get("/{entry_id}/downloads")
def list_downloads(entry_id: int) -> dict[str, Any]:
    """列出下载记录；重复记录数应始终为 0。"""
    return report_data_service.downloads(service=_entry(entry_id))


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
    """按服务页段读取已确认版本；续页使用首次请求版本，读取期间出新版本也不跨版本拼接。"""
    payload, message = report_data_service.page_segments(
        service=_entry(entry_id),
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
    return report_data_service.status(service=_entry(entry_id))


@router.post("/{entry_id}/invalidation-scope")
def invalidation_scope(entry_id: int, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """按变更字段判断产物、版本与页段的失效范围。"""
    entry = _entry(entry_id)
    task = report_data_service.resolve_task(service=entry)
    if task is None:
        return {"ok": False, "message": "该数据服务未关联报表任务，无法判断失效范围"}
    changed = payload.get("变更字段") or payload.get("changed_fields") or []
    return report_data_service.invalidation_scope(task, changed)
