"""只读风险快照接口。

- POST   /risk-snapshots/                 按时间点生成（或幂等取回）快照
- GET    /risk-snapshots/                 快照清单（可按项目/日期筛选）
- GET    /risk-snapshots/lookup           按项目+时间点精确查找
- GET    /risk-snapshots/{snapshot_id}    查看冻结载荷（记录审计）
- GET    /risk-snapshots/{snapshot_id}/download   下载冻结载荷（记录审计）
- GET    /risk-snapshots/{snapshot_id}/access-logs 下载/查看审计（仅负责人）

快照载荷不包含联系人姓名、电话、邮箱等敏感字段。
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import schemas
from ..database import get_db
from ..errors import ERROR_NOT_FOUND, HTTPStatus
from ..services import risk_snapshot as svc
from ..services.auth import (
    SnapshotUser,
    authenticate,
    require_generate_user,
    require_read_user,
)

router = APIRouter(prefix="/risk-snapshots", tags=["风险快照（只读冻结报告）"])


class SnapshotCreateRequest(BaseModel):
    project_id: int = Field(..., description="项目ID")
    as_of_date: date = Field(..., description="报告时间点（含当日）")


class SnapshotCreateResponse(BaseModel):
    snapshot_id: int
    newly_generated: bool = Field(
        ..., description="true=本次新生成；false=同项目同时间点快照已存在，返回原冻结版本"
    )
    snapshot: schemas.RiskSnapshotPayload


def _require_project_exists(db: Session, project_id: int):
    from .. import models

    project = (
        db.query(models.Project.id).filter(models.Project.id == project_id).first()
    )
    if project is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["project"],
        )


@router.post(
    "/",
    response_model=SnapshotCreateResponse,
    summary="按指定时间点生成项目风险快照（重复生成幂等返回原快照）",
)
def create_snapshot(
    req: SnapshotCreateRequest,
    db: Session = Depends(get_db),
    user: SnapshotUser = Depends(require_generate_user),
):
    _require_project_exists(db, req.project_id)
    try:
        snapshot, created = svc.create_risk_snapshot(
            db, project_id=req.project_id, as_of=req.as_of_date, user=user
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST, detail=str(exc)
        )
    if snapshot is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["project"],
        )
    body = SnapshotCreateResponse(
        snapshot_id=snapshot.id,
        newly_generated=created,
        snapshot=svc.payload_dict(snapshot),
    )
    # 新生成 201；重复生成幂等返回原冻结版本 200
    return JSONResponse(
        content=jsonable_encoder(body),
        status_code=HTTPStatus.CREATED if created else HTTPStatus.OK,
    )


@router.get(
    "/",
    response_model=List[schemas.RiskSnapshotListItem],
    summary="查询风险快照清单",
)
def list_snapshots(
    project_id: Optional[int] = Query(None, description="项目ID筛选"),
    as_of_date: Optional[date] = Query(None, description="时间点筛选"),
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: SnapshotUser = Depends(require_read_user),
):
    return svc.list_snapshots(
        db,
        project_id=project_id,
        as_of=as_of_date,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/lookup",
    response_model=SnapshotCreateResponse,
    summary="按项目+时间点精确查找已生成快照",
)
def lookup_snapshot(
    project_id: int = Query(..., description="项目ID"),
    as_of_date: date = Query(..., description="报告时间点"),
    db: Session = Depends(get_db),
    user: SnapshotUser = Depends(require_read_user),
):
    snapshots = svc.list_snapshots(db, project_id=project_id, as_of=as_of_date, limit=1)
    if not snapshots:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="该项目在此时间点的风险快照尚未生成",
        )
    snapshot = snapshots[0]
    return SnapshotCreateResponse(
        snapshot_id=snapshot.id,
        newly_generated=False,
        snapshot=svc.payload_dict(snapshot),
    )


@router.get(
    "/{snapshot_id}",
    response_model=schemas.RiskSnapshotPayload,
    summary="查看风险快照冻结载荷",
)
def get_snapshot(
    snapshot_id: int,
    db: Session = Depends(get_db),
    user: SnapshotUser = Depends(require_read_user),
):
    snapshot = svc.get_snapshot(db, snapshot_id=snapshot_id)
    if snapshot is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="风险快照不存在",
        )
    svc.record_access(db, snapshot, user, action="view")
    return svc.payload_dict(snapshot)


@router.get(
    "/{snapshot_id}/download",
    summary="下载风险快照冻结载荷（JSON 文件，重复下载内容不变）",
)
def download_snapshot(
    snapshot_id: int,
    db: Session = Depends(get_db),
    user: SnapshotUser = Depends(require_read_user),
):
    snapshot = svc.get_snapshot(db, snapshot_id=snapshot_id)
    if snapshot is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="风险快照不存在",
        )
    svc.record_access(db, snapshot, user, action="download")
    # 直接回传冻结原文（键排序、紧凑 JSON），文件字节的 SHA-256 即 X-Snapshot-Hash，
    # 负责人可据此离线核验文件未被篡改；GET 查看接口返回同内容的可读 JSON。
    filename = (
        f"risk_snapshot_project_{snapshot.project_id}_{snapshot.as_of_date.isoformat()}.json"
    )
    return Response(
        content=snapshot.payload,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Snapshot-Hash": snapshot.payload_hash,
            "X-Snapshot-Hash-Algorithm": "sha256 of canonical payload (sort_keys, compact)",
        },
    )


@router.get(
    "/{snapshot_id}/access-logs",
    response_model=List[schemas.SnapshotAccessLogItem],
    summary="查询快照查看/下载审计记录（仅负责人）",
)
def get_snapshot_access_logs(
    snapshot_id: int,
    db: Session = Depends(get_db),
    user: SnapshotUser = Depends(authenticate),
):
    if user.role != "director":
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="权限不足：仅负责人可查看审计记录",
        )
    snapshot = svc.get_snapshot(db, snapshot_id=snapshot_id)
    if snapshot is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail="风险快照不存在",
        )
    return svc.list_access_logs(db, snapshot_id=snapshot_id)
