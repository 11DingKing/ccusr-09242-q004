from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..database import get_db
from .. import crud, schemas
from ..errors import (
    HTTPStatus,
    ERROR_NOT_FOUND,
    ERROR_SNAPSHOT,
    fmt,
)
from ..security import require_permission

router = APIRouter(
    prefix="/projects/{project_id}/risk-snapshots",
    tags=["项目风险快照"],
)


def _get_project_or_404(db: Session, project_id: int):
    project = crud.get_project(db, project_id=project_id)
    if not project:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["project"],
        )
    return project


@router.post(
    "",
    response_model=schemas.RiskSnapshotDetail,
    summary="生成项目风险快照（同一基准时间重复请求返回已冻结快照）",
)
def generate_snapshot(
    project_id: int,
    response: Response,
    req: Optional[schemas.RiskSnapshotGenerateRequest] = None,
    role: str = Depends(require_permission("snapshot:generate")),
    db: Session = Depends(get_db),
):
    project = _get_project_or_404(db, project_id)
    as_of = (
        crud.normalize_snapshot_as_of(req.as_of)
        if req and req.as_of
        else datetime.utcnow()
    )
    if project.created_at and as_of < project.created_at:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=fmt(
                ERROR_SNAPSHOT["as_of_before_project_created"],
                as_of=as_of.isoformat(sep=" ", timespec="seconds"),
                created_at=project.created_at.isoformat(sep=" ", timespec="seconds"),
            ),
        )
    operator = req.operator if req and req.operator else role
    snapshot, created = crud.get_or_create_risk_snapshot(
        db, project, as_of, generated_by=operator
    )
    response.status_code = HTTPStatus.CREATED if created else HTTPStatus.OK
    return crud.risk_snapshot_to_detail(snapshot)


@router.get(
    "",
    response_model=List[schemas.RiskSnapshotMeta],
    summary="查询项目风险快照列表（仅元信息）",
)
def list_snapshots(
    project_id: int,
    role: str = Depends(require_permission("snapshot:download")),
    db: Session = Depends(get_db),
):
    _get_project_or_404(db, project_id)
    snapshots = crud.list_risk_snapshots(db, project_id)
    return [crud.risk_snapshot_to_meta(s) for s in snapshots]


@router.get(
    "/{snapshot_id}",
    response_model=schemas.RiskSnapshotDetail,
    summary="下载指定风险快照（内容已冻结，授权人员可重复下载）",
)
def download_snapshot(
    project_id: int,
    snapshot_id: int,
    role: str = Depends(require_permission("snapshot:download")),
    db: Session = Depends(get_db),
):
    _get_project_or_404(db, project_id)
    snapshot = crud.get_risk_snapshot(db, project_id, snapshot_id)
    if not snapshot:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["risk_snapshot"],
        )
    return crud.risk_snapshot_to_detail(snapshot)
