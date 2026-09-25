"""项目风险快照服务。

按给定基准时间点汇总项目及关联记录，并把计算依据冻结为不可变快照：

- 只纳入基准时间之前创建（``created_at <= as_of``）的关联记录；
- 项目状态依据状态日志还原到基准时点；
- 快照内容序列化后落库，后续项目修改不影响已生成快照；
- 同一项目同一基准时间重复生成时，直接返回已冻结的快照；
- 快照内容不含联系人电话、邮箱等敏感字段。
"""

import hashlib
import json
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from .. import models, schemas
from ..enums import (
    FollowUpStatus,
    MilestoneStatus,
    ProjectStatus,
)

SNAPSHOT_PAYLOAD_VERSION = 1

# 未解决跟进事项状态
OPEN_FOLLOW_UP_STATUSES = (FollowUpStatus.PENDING, FollowUpStatus.IN_PROGRESS)

# 已立项及以后的状态：处于这些阶段却缺少立项记录属于异常
POST_APPROVAL_STATUSES = (
    ProjectStatus.ESTABLISHED,
    ProjectStatus.UNDER_CONSTRUCTION,
    ProjectStatus.COMMISSIONED,
)

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def normalize_as_of(as_of: datetime) -> datetime:
    """统一转换为 naive UTC，保证唯一约束与记录过滤可比较。"""
    if as_of.tzinfo is not None:
        as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)
    return as_of


def _status_at(
    logs: List[models.ProjectStatusLog], as_of: datetime
) -> ProjectStatus:
    """按状态日志把项目状态还原到基准时点；无日志时为创建时的初始状态。"""
    relevant = [log for log in logs if log.changed_at and log.changed_at <= as_of]
    if not relevant:
        return ProjectStatus.ATTRACTING_INVESTMENT
    latest = max(relevant, key=lambda log: (log.changed_at, log.id or 0))
    return latest.to_status


def _effective_promised_capacity(
    project: models.Project,
) -> Tuple[Optional[float], Optional[str]]:
    """与统计口径一致：优先承诺月产能，否则按年产能 / 12 推算。"""
    if project.promised_monthly_capacity_tonnes:
        return (
            project.promised_monthly_capacity_tonnes,
            "promised_monthly_capacity_tonnes",
        )
    if project.expected_annual_capacity_tonnes:
        return (
            round(project.expected_annual_capacity_tonnes / 12.0, 2),
            "expected_annual_capacity_tonnes/12",
        )
    return None, None


def _load_records(db: Session, project_id: int, as_of: datetime):
    """读取基准时间点之前创建的全部关联记录。"""
    approval = (
        db.query(models.ProjectApproval)
        .filter(
            models.ProjectApproval.project_id == project_id,
            models.ProjectApproval.created_at <= as_of,
        )
        .order_by(models.ProjectApproval.id)
        .first()
    )
    milestones = (
        db.query(models.ProjectMilestone)
        .filter(
            models.ProjectMilestone.project_id == project_id,
            models.ProjectMilestone.created_at <= as_of,
        )
        .order_by(models.ProjectMilestone.sequence, models.ProjectMilestone.id)
        .all()
    )
    reports = (
        db.query(models.MonthlyCapacityReport)
        .filter(
            models.MonthlyCapacityReport.project_id == project_id,
            models.MonthlyCapacityReport.created_at <= as_of,
        )
        .order_by(
            models.MonthlyCapacityReport.report_year,
            models.MonthlyCapacityReport.report_month,
            models.MonthlyCapacityReport.id,
        )
        .all()
    )
    follow_ups = (
        db.query(models.CapacityFollowUp)
        .filter(
            models.CapacityFollowUp.project_id == project_id,
            models.CapacityFollowUp.created_at <= as_of,
        )
        .order_by(models.CapacityFollowUp.id)
        .all()
    )
    logs = (
        db.query(models.ProjectStatusLog)
        .filter(
            models.ProjectStatusLog.project_id == project_id,
            models.ProjectStatusLog.changed_at <= as_of,
        )
        .all()
    )
    return approval, milestones, reports, follow_ups, logs


def _build_risk_items(
    status_at: ProjectStatus,
    approval: Optional[models.ProjectApproval],
    milestones: List[models.ProjectMilestone],
    reports: List[models.MonthlyCapacityReport],
    follow_ups: List[models.CapacityFollowUp],
    promised: Optional[float],
    utilization: Optional[float],
    as_of: datetime,
) -> List[dict]:
    """基于冻结数据推导风险项，按 (严重度, 编码) 稳定排序。"""
    as_of_date = as_of.date()
    risks: List[dict] = []

    if status_at in POST_APPROVAL_STATUSES and approval is None:
        risks.append(
            {
                "code": "APPROVAL_MISSING",
                "severity": "high",
                "message": f"项目已处于「{status_at.value}」阶段，但缺少立项记录",
                "count": 1,
            }
        )

    delayed = [m for m in milestones if m.status == MilestoneStatus.DELAYED]
    if delayed:
        risks.append(
            {
                "code": "MILESTONE_DELAYED",
                "severity": "medium",
                "message": f"{len(delayed)} 条里程碑处于延期状态",
                "count": len(delayed),
            }
        )

    overdue = [
        m
        for m in milestones
        if m.status != MilestoneStatus.COMPLETED
        and m.planned_date
        and m.planned_date < as_of_date
    ]
    if overdue:
        risks.append(
            {
                "code": "MILESTONE_OVERDUE",
                "severity": "medium",
                "message": f"{len(overdue)} 条里程碑计划日期早于基准时间但仍未完成",
                "count": len(overdue),
            }
        )

    if status_at == ProjectStatus.COMMISSIONED:
        if not reports:
            risks.append(
                {
                    "code": "CAPACITY_REPORT_MISSING",
                    "severity": "high",
                    "message": "项目已投产，但基准时间前没有任何月度产能登记",
                    "count": 1,
                }
            )
        if not promised:
            risks.append(
                {
                    "code": "PROMISED_CAPACITY_MISSING",
                    "severity": "medium",
                    "message": "缺少承诺月产能，无法核算达产率",
                    "count": 1,
                }
            )
        if utilization is not None and utilization < 100:
            risks.append(
                {
                    "code": "CAPACITY_UTILIZATION_GAP",
                    "severity": "medium",
                    "message": f"最新月度达产率 {utilization}%，未达到承诺产能",
                    "count": 1,
                }
            )

    open_follow_ups = [f for f in follow_ups if f.status in OPEN_FOLLOW_UP_STATUSES]
    if open_follow_ups:
        risks.append(
            {
                "code": "FOLLOW_UP_OPEN",
                "severity": "medium",
                "message": f"{len(open_follow_ups)} 项产能跟进事项尚未解决",
                "count": len(open_follow_ups),
            }
        )
    overdue_follow_ups = [
        f for f in open_follow_ups if f.deadline and f.deadline < as_of_date
    ]
    if overdue_follow_ups:
        risks.append(
            {
                "code": "FOLLOW_UP_OVERDUE",
                "severity": "high",
                "message": f"{len(overdue_follow_ups)} 项跟进事项已过截止日期仍未解决",
                "count": len(overdue_follow_ups),
            }
        )

    risks.sort(key=lambda r: (SEVERITY_ORDER[r["severity"]], r["code"]))
    return risks


def _collect_missing_fields(
    project: models.Project,
    status_at: ProjectStatus,
    approval: Optional[models.ProjectApproval],
    reports: List[models.MonthlyCapacityReport],
) -> List[str]:
    """汇总例会关心但缺失的关键字段，按字段名字典序返回。"""
    missing: List[str] = []
    if project.expected_annual_capacity_tonnes is None:
        missing.append("expected_annual_capacity_tonnes")
    if project.expected_output_value_10k is None:
        missing.append("expected_output_value_10k")
    if project.expected_jobs is None:
        missing.append("expected_jobs")
    if not project.project_leader:
        missing.append("project_leader")
    if not project.responsible_department:
        missing.append("responsible_department")
    if status_at in POST_APPROVAL_STATUSES and approval is None:
        missing.append("agreed_investment_10k")
    if status_at == ProjectStatus.COMMISSIONED:
        if not project.promised_monthly_capacity_tonnes:
            missing.append("promised_monthly_capacity_tonnes")
        if not reports:
            missing.append("latest_capacity_report")
    return sorted(set(missing))


def build_report(
    db: Session, project: models.Project, as_of: datetime
) -> schemas.RiskSnapshotReport:
    """按基准时间点汇总项目及关联记录，生成待冻结的快照内容。"""
    approval, milestones, reports, follow_ups, logs = _load_records(
        db, project.id, as_of
    )
    status_at = _status_at(logs, as_of)
    promised, promised_source = _effective_promised_capacity(project)

    latest_report = reports[-1] if reports else None
    utilization: Optional[float] = None
    if latest_report is not None:
        utilization = latest_report.capacity_utilization_rate
        if utilization is None and promised:
            utilization = round(
                latest_report.actual_output_tonnes / promised * 100, 2
            )

    open_follow_ups = [f for f in follow_ups if f.status in OPEN_FOLLOW_UP_STATUSES]
    # 稳定排序：有截止日期的按日期升序在前，无日期的按 id 升序在后
    open_follow_ups.sort(
        key=lambda f: (f.deadline is None, f.deadline or date.min, f.id)
    )

    investment_sources = [
        {
            "table": "projects",
            "record_id": project.id,
            "field": "planned_investment_10k",
            "source_time": project.updated_at,
        }
    ]
    if approval is not None:
        investment_sources.append(
            {
                "table": "project_approvals",
                "record_id": approval.id,
                "field": "agreed_investment_10k",
                "source_time": approval.created_at,
            }
        )

    risk_items = _build_risk_items(
        status_at=status_at,
        approval=approval,
        milestones=milestones,
        reports=reports,
        follow_ups=follow_ups,
        promised=promised,
        utilization=utilization,
        as_of=as_of,
    )
    missing_fields = _collect_missing_fields(project, status_at, approval, reports)

    source_times = {
        "project_updated_at": project.updated_at,
        "approval_created_at": approval.created_at if approval else None,
        "milestones_latest_updated_at": max(
            (m.updated_at for m in milestones if m.updated_at), default=None
        ),
        "latest_capacity_report_created_at": (
            latest_report.created_at if latest_report else None
        ),
        "follow_ups_latest_updated_at": max(
            (f.updated_at for f in follow_ups if f.updated_at), default=None
        ),
    }

    return schemas.RiskSnapshotReport(
        project=schemas.SnapshotProjectSection(
            id=project.id,
            name=project.name,
            project_code=project.project_code,
            status=status_at,
            park_id=project.park_id,
            park_name=project.park.name if project.park else None,
            initiator_id=project.initiator_id,
            initiator_name=project.initiator.name if project.initiator else None,
            responsible_department=project.responsible_department,
            project_leader=project.project_leader,
        ),
        investment=schemas.SnapshotInvestmentSection(
            planned_investment_10k=project.planned_investment_10k,
            agreed_investment_10k=(
                approval.agreed_investment_10k if approval else None
            ),
            sources=[schemas.SnapshotSourceRef(**s) for s in investment_sources],
        ),
        milestones=[
            schemas.SnapshotMilestoneItem(
                id=m.id,
                sequence=m.sequence,
                name=m.name,
                milestone_type=m.milestone_type,
                status=m.status,
                planned_date=m.planned_date,
                actual_date=m.actual_date,
                completion_rate=m.completion_rate or 0.0,
                source_time=m.updated_at,
            )
            for m in milestones
        ],
        capacity=schemas.SnapshotCapacitySection(
            promised_monthly_capacity_tonnes=promised,
            promised_source=promised_source,
            utilization_rate=utilization,
            latest_report=(
                schemas.SnapshotCapacityReportItem(
                    id=latest_report.id,
                    report_year=latest_report.report_year,
                    report_month=latest_report.report_month,
                    actual_output_tonnes=latest_report.actual_output_tonnes,
                    capacity_utilization_rate=latest_report.capacity_utilization_rate,
                    local_material_procurement_10k=latest_report.local_material_procurement_10k,
                    source_time=latest_report.created_at,
                )
                if latest_report
                else None
            ),
        ),
        open_follow_ups=[
            schemas.SnapshotFollowUpItem(
                id=f.id,
                title=f.title,
                status=f.status,
                priority=f.priority,
                gap_percentage=f.gap_percentage,
                deadline=f.deadline,
                source_time=f.updated_at,
            )
            for f in open_follow_ups
        ],
        risk_items=[schemas.SnapshotRiskItem(**r) for r in risk_items],
        missing_fields=missing_fields,
        source_times=schemas.SnapshotSourceTimes(**source_times),
    )


def get_or_create_snapshot(
    db: Session,
    project: models.Project,
    as_of: datetime,
    generated_by: Optional[str] = None,
) -> Tuple[models.ProjectRiskSnapshot, bool]:
    """返回 (快照, 是否新建)。同一项目同一基准时间重复请求时返回已冻结快照。"""
    existing = (
        db.query(models.ProjectRiskSnapshot)
        .filter(
            models.ProjectRiskSnapshot.project_id == project.id,
            models.ProjectRiskSnapshot.as_of == as_of,
        )
        .first()
    )
    if existing:
        return existing, False

    report = build_report(db, project, as_of)
    payload = json.dumps(
        {"version": SNAPSHOT_PAYLOAD_VERSION, "report": report.model_dump(mode="json")},
        ensure_ascii=False,
        sort_keys=True,
    )
    snapshot = models.ProjectRiskSnapshot(
        project_id=project.id,
        as_of=as_of,
        generated_by=generated_by,
        payload=payload,
        payload_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot, True


def list_snapshots(db: Session, project_id: int) -> List[models.ProjectRiskSnapshot]:
    return (
        db.query(models.ProjectRiskSnapshot)
        .filter(models.ProjectRiskSnapshot.project_id == project_id)
        .order_by(
            models.ProjectRiskSnapshot.as_of.desc(),
            models.ProjectRiskSnapshot.id.desc(),
        )
        .all()
    )


def get_snapshot(
    db: Session, project_id: int, snapshot_id: int
) -> Optional[models.ProjectRiskSnapshot]:
    return (
        db.query(models.ProjectRiskSnapshot)
        .filter(
            models.ProjectRiskSnapshot.project_id == project_id,
            models.ProjectRiskSnapshot.id == snapshot_id,
        )
        .first()
    )


def snapshot_to_meta(snapshot: models.ProjectRiskSnapshot) -> schemas.RiskSnapshotMeta:
    return schemas.RiskSnapshotMeta(
        id=snapshot.id,
        project_id=snapshot.project_id,
        as_of=snapshot.as_of,
        generated_at=snapshot.generated_at,
        generated_by=snapshot.generated_by,
        payload_hash=snapshot.payload_hash,
    )


def snapshot_to_detail(
    snapshot: models.ProjectRiskSnapshot,
) -> schemas.RiskSnapshotDetail:
    payload = json.loads(snapshot.payload)
    report = schemas.RiskSnapshotReport(**payload["report"])
    return schemas.RiskSnapshotDetail(
        **snapshot_to_meta(snapshot).model_dump(),
        report=report,
    )
