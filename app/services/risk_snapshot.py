"""风险快照领域服务：按指定时间点汇总项目及关联记录、识别风险与缺失字段。

时间点语义
----------
- ``as_of_date`` 为报告日期（含当日），截止时刻 ``cutoff`` 取该日期次日 00:00（UTC），
  所有关联记录以其业务入库时间（``created_at`` / ``submitted_at`` / ``changed_at``）
  严格 ``< cutoff`` 纳入，保证「在指定日期当天看到的资料」边界可判定。
- 项目状态不直接取当前值，而依据 ``ProjectStatusLog`` 重放到截止时刻；没有日志时
  回退为当前状态并在来源中标注。
- 字段值（如投资金额、里程碑状态）没有独立历史表，取快照生成时的当前值，
  各数据块统一在 ``sources`` 中给出采用记录的最近更新时间，即「本报告采用了哪一版资料」。

冻结语义
--------
快照生成后全量载荷以 JSON 文本写入 ``RiskSnapshot.payload`` 并计算 SHA-256 摘要；
快照与项目表不建外键，项目后续修改或删除均不影响已生成快照。
"""

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..enums import (
    FollowUpStatus,
    MilestoneStatus,
    ProjectStatus,
)

# 风险严重度排序权重（数值越小越严重、排序越靠前）
SEVERITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_LABELS = {
    "urgent": "紧急",
    "high": "高",
    "medium": "中",
    "low": "低",
}

# 风险类别固定顺序（同等严重度时稳定排序）
CATEGORY_ORDER = [
    "产能兑现风险",
    "跟进事项风险",
    "里程碑风险",
    "建设进度风险",
    "投资兑现风险",
]

# 缺失字段固定展示顺序
MISSING_FIELD_ORDER = [
    ("project", "expected_annual_capacity_tonnes"),
    ("project", "promised_monthly_capacity_tonnes"),
    ("project", "expected_output_value_10k"),
    ("project", "responsible_department"),
    ("project", "project_leader"),
    ("approval", "agreed_capacity_tonnes"),
    ("approval", "completion_deadline"),
    ("milestone", "actual_date"),
    ("capacity_report", "capacity_utilization_rate"),
]
MISSING_FIELD_INDEX = {key: i for i, key in enumerate(MISSING_FIELD_ORDER)}

FIELD_LABELS = {
    "expected_annual_capacity_tonnes": "预期年产能",
    "promised_monthly_capacity_tonnes": "承诺月产能",
    "expected_output_value_10k": "预期年产值",
    "responsible_department": "责任部门",
    "project_leader": "项目负责人",
    "agreed_capacity_tonnes": "协议产能",
    "completion_deadline": "竣工期限",
    "actual_date": "实际完成日期",
    "capacity_utilization_rate": "产能利用率",
}

# 项目摘要白名单：联系人敏感字段（负责人姓名/电话、主体联系人等）一律不进入快照
PROJECT_WHITELIST = [
    "id",
    "name",
    "project_code",
    "status",
    "investment_direction",
    "planned_investment_10k",
    "expected_annual_capacity_tonnes",
    "planned_land_area_mu",
    "expected_output_value_10k",
    "expected_jobs",
    "construction_cycle_months",
    "commissioned_date",
    "promised_monthly_capacity_tonnes",
    "expected_local_procurement_pct",
    "park_id",
    "initiator_id",
    "publish_date",
    "created_at",
    "updated_at",
]


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _iso_date(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


def _enum_value(value):
    return value.value if hasattr(value, "value") else value


def cutoff_for(as_of_date: date) -> datetime:
    """指定日期次日 00:00（UTC naive），即记录入库时间的排他上界。"""
    return datetime.combine(as_of_date + timedelta(days=1), datetime.min.time())


def _replay_status(
    db: Session, project_id: int, cutoff: datetime
) -> Tuple[ProjectStatus, str, Optional[datetime]]:
    """依据状态日志重放项目在截止时刻的状态。

    返回 (状态, 依据, 最近状态变更时间)。无日志时回退当前状态。
    """
    latest_log = (
        db.query(models.ProjectStatusLog)
        .filter(
            models.ProjectStatusLog.project_id == project_id,
            models.ProjectStatusLog.changed_at < cutoff,
        )
        .order_by(
            models.ProjectStatusLog.changed_at.desc(),
            models.ProjectStatusLog.id.desc(),
        )
        .first()
    )
    if latest_log is not None:
        return latest_log.to_status, "status_log", latest_log.changed_at
    project = db.query(models.Project.status).filter(
        models.Project.id == project_id
    ).first()
    current = project[0] if project else ProjectStatus.ATTRACTING_INVESTMENT
    return current, "current", None


def _freeze_project(project: models.Project, status_at_snapshot: ProjectStatus) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for field in PROJECT_WHITELIST:
        value = getattr(project, field, None)
        if isinstance(value, datetime):
            value = _iso(value)
        elif isinstance(value, date):
            value = _iso_date(value)
        elif hasattr(value, "value"):
            value = value.value
        data[field] = value
    # 以重放状态覆盖当前状态
    data["status"] = status_at_snapshot.value
    data["park_name"] = project.park.name if project.park else None
    data["initiator_name"] = project.initiator.name if project.initiator else None
    return data


def _freeze_approval(approval: Optional[models.ProjectApproval]) -> Optional[Dict[str, Any]]:
    if approval is None:
        return None
    return {
        "id": approval.id,
        "approval_number": approval.approval_number,
        "approval_date": _iso_date(approval.approval_date),
        "approving_authority": approval.approving_authority,
        "agreed_investment_10k": approval.agreed_investment_10k,
        "agreed_capacity_tonnes": approval.agreed_capacity_tonnes,
        "agreed_land_area_mu": approval.agreed_land_area_mu,
        "construction_start_deadline": _iso_date(approval.construction_start_deadline),
        "completion_deadline": _iso_date(approval.completion_deadline),
        "created_at": _iso(approval.created_at),
    }


def _freeze_milestone(milestone: models.ProjectMilestone) -> Dict[str, Any]:
    # 不含 responsible_person 等联系人字段
    return {
        "id": milestone.id,
        "sequence": milestone.sequence,
        "milestone_type": _enum_value(milestone.milestone_type),
        "name": milestone.name,
        "status": _enum_value(milestone.status),
        "planned_date": _iso_date(milestone.planned_date),
        "actual_date": _iso_date(milestone.actual_date),
        "completion_rate": milestone.completion_rate,
        "updated_at": _iso(milestone.updated_at),
    }


def _freeze_report(report: models.MonthlyCapacityReport) -> Dict[str, Any]:
    return {
        "id": report.id,
        "report_year": report.report_year,
        "report_month": report.report_month,
        "actual_output_tonnes": report.actual_output_tonnes,
        "capacity_utilization_rate": report.capacity_utilization_rate,
        "local_material_procurement_10k": report.local_material_procurement_10k,
        "employee_count": report.employee_count,
        "created_at": _iso(report.created_at),
        "updated_at": _iso(report.updated_at),
    }


def _freeze_intent(
    intent: models.CooperationIntent, negotiations: List[models.NegotiationRecord]
) -> Dict[str, Any]:
    # 不含提交方/对方主体的联系人字段，仅保留主体名称便于阅读
    return {
        "id": intent.id,
        "submitter_id": intent.submitter_id,
        "submitter_name": intent.submitter.name if intent.submitter else None,
        "counterparty_id": intent.counterparty_id,
        "counterparty_name": intent.counterparty.name if intent.counterparty else None,
        "status": _enum_value(intent.status),
        "cooperation_mode": intent.cooperation_mode,
        "proposed_investment_10k": intent.proposed_investment_10k,
        "proposed_capacity_tonnes": intent.proposed_capacity_tonnes,
        "cooperation_content": intent.cooperation_content,
        "submitted_at": _iso(intent.submitted_at),
        "reviewed_at": _iso(intent.reviewed_at),
        "negotiations": [_freeze_negotiation(n) for n in negotiations],
    }


def _freeze_negotiation(record: models.NegotiationRecord) -> Dict[str, Any]:
    # 不含 host / participants / minutes_author 等参与人敏感字段
    return {
        "id": record.id,
        "round": record.round,
        "title": record.title,
        "held_at": _iso(record.held_at),
        "location": record.location,
        "key_topics": record.key_topics,
        "consensus": record.consensus,
        "disagreements": record.disagreements,
        "next_steps": record.next_steps,
        "next_meeting_date": _iso_date(record.next_meeting_date),
        "created_at": _iso(record.created_at),
    }


def _freeze_follow_up(follow_up: models.CapacityFollowUp) -> Dict[str, Any]:
    return {
        "id": follow_up.id,
        "report_id": follow_up.report_id,
        "title": follow_up.title,
        "status": _enum_value(follow_up.status),
        "priority": _enum_value(follow_up.priority),
        "gap_percentage": follow_up.gap_percentage,
        "deadline": _iso_date(follow_up.deadline),
        "created_at": _iso(follow_up.created_at),
        "updated_at": _iso(follow_up.updated_at),
    }


def _freeze_status_log(log: models.ProjectStatusLog) -> Dict[str, Any]:
    return {
        "id": log.id,
        "from_status": _enum_value(log.from_status),
        "to_status": _enum_value(log.to_status),
        "changed_at": _iso(log.changed_at),
        "reason": log.reason,
    }


def _promised_monthly_capacity(project: models.Project) -> Optional[float]:
    if project.promised_monthly_capacity_tonnes:
        return project.promised_monthly_capacity_tonnes
    if project.expected_annual_capacity_tonnes:
        return round(project.expected_annual_capacity_tonnes / 12.0, 2)
    return None


def _risk_item(
    category: str,
    severity: str,
    ref_type: str,
    ref_id: Any,
    title: str,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "category": category,
        "severity": severity,
        "severity_label": SEVERITY_LABELS[severity],
        "ref_type": ref_type,
        "ref_id": ref_id,
        "title": title,
        "detail": detail,
    }


def _missing_field(
    scope: str,
    ref_id: Any,
    field: str,
    ref_name: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "scope": scope,
        "scope_ref_id": ref_id,
        "scope_ref_name": ref_name,
        "field": field,
        "field_label": FIELD_LABELS.get(field, field),
    }


def _sort_risk_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(item: Dict[str, Any]):
        return (
            SEVERITY_ORDER.get(item["severity"], 99),
            CATEGORY_ORDER.index(item["category"])
            if item["category"] in CATEGORY_ORDER
            else len(CATEGORY_ORDER),
            item["ref_type"],
            item["ref_id"] if item["ref_id"] is not None else 0,
            item["title"],
        )

    return sorted(items, key=key)


def _sort_missing_fields(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(item: Dict[str, Any]):
        return (
            MISSING_FIELD_INDEX.get(
                (item["scope"], item["field"]), len(MISSING_FIELD_INDEX)
            ),
            item["scope"],
            item["scope_ref_id"] if item["scope_ref_id"] is not None else 0,
        )

    return sorted(items, key=key)


def _evaluate(
    as_of: date,
    project: models.Project,
    status_at_snapshot: ProjectStatus,
    approval: Optional[models.ProjectApproval],
    milestones: List[models.ProjectMilestone],
    reports: List[models.MonthlyCapacityReport],
    follow_ups: List[models.CapacityFollowUp],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """根据冻结的资料识别风险项、缺失字段并汇总产能/投资结论。"""
    risks: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []

    # ---- 项目级缺失字段（只记录字段名，绝不回传字段值）----
    project_optional_fields = [
        "expected_annual_capacity_tonnes",
        "expected_output_value_10k",
        "responsible_department",
        "project_leader",
    ]
    for field in project_optional_fields:
        if getattr(project, field, None) in (None, ""):
            missing.append(_missing_field("project", project.id, field))
    if status_at_snapshot == ProjectStatus.COMMISSIONED:
        if not _promised_monthly_capacity(project):
            missing.append(
                _missing_field("project", project.id, "promised_monthly_capacity_tonnes")
            )

    # ---- 里程碑风险 ----
    for m in milestones:
        if m.status == MilestoneStatus.DELAYED:
            risks.append(
                _risk_item(
                    "里程碑风险",
                    "high",
                    "milestone",
                    m.id,
                    f"第{m.sequence}个里程碑「{m.name}」已标记延期",
                    f"计划日期 {_iso_date(m.planned_date)}，当前状态：已延期",
                )
            )
        elif m.status != MilestoneStatus.COMPLETED and m.planned_date < as_of:
            overdue_days = (as_of - m.planned_date).days
            severity = "high" if m.status == MilestoneStatus.NOT_STARTED else "medium"
            risks.append(
                _risk_item(
                    "里程碑风险",
                    severity,
                    "milestone",
                    m.id,
                    f"第{m.sequence}个里程碑「{m.name}」超过计划日期未完成",
                    f"计划日期 {_iso_date(m.planned_date)}，已逾期 {overdue_days} 天，"
                    f"当前状态：{_enum_value(m.status)}",
                )
            )
        if m.status == MilestoneStatus.COMPLETED and m.actual_date is None:
            missing.append(
                _missing_field("milestone", m.id, "actual_date", ref_name=m.name)
            )

    # ---- 立项信息、建设进度与投资兑现 ----
    investment_summary: Dict[str, Any] = {
        "planned_investment_10k": project.planned_investment_10k,
        "agreed_investment_10k": None,
        "gap_amount_10k": None,
        "gap_rate_pct": None,
    }
    if approval is not None:
        if approval.completion_deadline is None:
            missing.append(_missing_field("approval", approval.id, "completion_deadline"))
        if approval.agreed_capacity_tonnes is None:
            missing.append(
                _missing_field(
                    "approval", approval.id, "agreed_capacity_tonnes",
                    ref_name=approval.approval_number,
                )
            )
        if (
            status_at_snapshot == ProjectStatus.UNDER_CONSTRUCTION
            and approval.completion_deadline is not None
            and approval.completion_deadline < as_of
        ):
            risks.append(
                _risk_item(
                    "建设进度风险",
                    "high",
                    "approval",
                    approval.id,
                    "项目超过竣工期限仍处于建设中",
                    f"竣工期限 {_iso_date(approval.completion_deadline)}，"
                    f"截至 {_iso_date(as_of)} 未投产",
                )
            )
        planned = project.planned_investment_10k or 0.0
        agreed = approval.agreed_investment_10k or 0.0
        gap_amount = round(planned - agreed, 2)
        gap_rate = round(gap_amount / planned * 100, 2) if planned > 0 else 0.0
        investment_summary.update(
            {
                "agreed_investment_10k": agreed,
                "gap_amount_10k": gap_amount,
                "gap_rate_pct": gap_rate,
            }
        )
        if gap_amount > 0:
            severity = "medium" if gap_rate >= 20 else "low"
            risks.append(
                _risk_item(
                    "投资兑现风险",
                    severity,
                    "approval",
                    approval.id,
                    "协议投资额低于规划投资额",
                    f"规划 {planned} 万元，协议 {agreed} 万元，"
                    f"缺口 {gap_amount} 万元（{gap_rate}%）",
                )
            )

    # ---- 产能兑现 ----
    latest_report = (
        max(reports, key=lambda r: (r.report_year, r.report_month)) if reports else None
    )
    promised = _promised_monthly_capacity(project)
    capacity_summary: Dict[str, Any] = {
        "eligible": status_at_snapshot == ProjectStatus.COMMISSIONED,
        "promised_monthly_capacity_tonnes": promised,
        "report_count": len(reports),
        "latest_report": _freeze_report(latest_report) if latest_report else None,
        "utilization_rate_pct": None,
        "latest_local_procurement_10k": None,
    }
    if status_at_snapshot == ProjectStatus.COMMISSIONED:
        if latest_report is None:
            risks.append(
                _risk_item(
                    "产能兑现风险",
                    "high",
                    "project",
                    project.id,
                    "项目已投产但截至该时间点尚无月度产能报告",
                    "无法核实产能兑现情况，请督促补报",
                )
            )
        else:
            if latest_report.capacity_utilization_rate is None:
                missing.append(
                    _missing_field(
                        "capacity_report",
                        latest_report.id,
                        "capacity_utilization_rate",
                    )
                )
            actual = latest_report.actual_output_tonnes or 0.0
            if promised:
                utilization = round(actual / promised * 100, 2)
                capacity_summary["utilization_rate_pct"] = utilization
                if utilization < 50:
                    severity = "urgent"
                elif utilization < 70:
                    severity = "high"
                elif utilization < 85:
                    severity = "medium"
                else:
                    severity = "low"
                if utilization < 100:
                    risks.append(
                        _risk_item(
                            "产能兑现风险",
                            severity,
                            "capacity_report",
                            latest_report.id,
                            f"{latest_report.report_year}年{latest_report.report_month}月产能未达承诺",
                            f"承诺月产能 {promised} 吨，实际 {actual} 吨，"
                            f"达产率 {utilization}%",
                        )
                    )
            capacity_summary["latest_local_procurement_10k"] = (
                latest_report.local_material_procurement_10k
            )

    # ---- 未解决跟进事项 ----
    open_statuses = {FollowUpStatus.PENDING, FollowUpStatus.IN_PROGRESS}
    open_follow_ups = [f for f in follow_ups if f.status in open_statuses]
    for f in open_follow_ups:
        if f.deadline is not None and f.deadline < as_of:
            severity = "high" if f.status == FollowUpStatus.PENDING else "medium"
            risks.append(
                _risk_item(
                    "跟进事项风险",
                    severity,
                    "follow_up",
                    f.id,
                    f"未解决跟进事项「{f.title}」已超过办理期限",
                    f"截止期限 {_iso_date(f.deadline)}，当前状态：{_enum_value(f.status)}",
                )
            )
        else:
            risks.append(
                _risk_item(
                    "跟进事项风险",
                    "medium",
                    "follow_up",
                    f.id,
                    f"未解决跟进事项「{f.title}」仍在办理中",
                    f"当前状态：{_enum_value(f.status)}，"
                    f"期限：{_iso_date(f.deadline) or '未设定'}",
                )
            )

    return _sort_risk_items(risks), _sort_missing_fields(missing), {
        "investment": investment_summary,
        "capacity": capacity_summary,
    }


def _build_sources(
    project: models.Project,
    approval: Optional[models.ProjectApproval],
    milestones: List[models.ProjectMilestone],
    intents: List[models.CooperationIntent],
    reports: List[models.MonthlyCapacityReport],
    follow_ups: List[models.CapacityFollowUp],
    status_log_count: int,
    status_basis: str,
    latest_log_at: Optional[datetime],
) -> List[Dict[str, Any]]:
    """汇总各数据块的来源时间，回答「报告采用了哪一版资料」。"""
    sources = [
        {
            "entity": "项目基础信息",
            "record_count": 1,
            "latest_source_time": _iso(project.updated_at),
            "note": "字段取快照生成时的最新版本",
        },
        {
            "entity": "立项信息",
            "record_count": 1 if approval else 0,
            "latest_source_time": _iso(approval.created_at) if approval else None,
            "note": "已冻结" if approval else "该时间点前无立项记录",
        },
        {
            "entity": "里程碑",
            "record_count": len(milestones),
            "latest_source_time": _iso(
                max((m.updated_at for m in milestones), default=None)
            ),
            "note": None,
        },
        {
            "entity": "合作意向",
            "record_count": len(intents),
            "latest_source_time": _iso(
                max((i.submitted_at for i in intents), default=None)
            ),
            "note": "按意向提交时间截止纳入",
        },
        {
            "entity": "月度产能报告",
            "record_count": len(reports),
            "latest_source_time": _iso(
                max((r.created_at for r in reports), default=None)
            ),
            "note": None,
        },
        {
            "entity": "产能跟进事项",
            "record_count": len(follow_ups),
            "latest_source_time": _iso(
                max((f.updated_at for f in follow_ups), default=None)
            ),
            "note": None,
        },
        {
            "entity": "项目状态日志",
            "record_count": status_log_count,
            "latest_source_time": _iso(latest_log_at),
            "note": "按状态日志重放" if status_basis == "status_log" else "无日志，采用当前状态",
        },
    ]
    return sources


def build_snapshot_payload(
    db: Session,
    project: models.Project,
    as_of: date,
    generated_by: Dict[str, str],
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    cutoff = cutoff_for(as_of)
    generated_at = generated_at or datetime.utcnow()

    status_at_snapshot, status_basis, latest_log_at = _replay_status(
        db, project.id, cutoff
    )

    approval = (
        db.query(models.ProjectApproval)
        .filter(
            models.ProjectApproval.project_id == project.id,
            models.ProjectApproval.created_at < cutoff,
        )
        .first()
    )
    milestones = (
        db.query(models.ProjectMilestone)
        .filter(
            models.ProjectMilestone.project_id == project.id,
            models.ProjectMilestone.created_at < cutoff,
        )
        .order_by(
            models.ProjectMilestone.sequence, models.ProjectMilestone.id
        )
        .all()
    )
    intents = (
        db.query(models.CooperationIntent)
        .filter(
            models.CooperationIntent.project_id == project.id,
            models.CooperationIntent.submitted_at < cutoff,
        )
        .order_by(models.CooperationIntent.submitted_at.desc())
        .all()
    )
    intent_ids = [intent.id for intent in intents]
    negotiations_by_intent: Dict[int, List[models.NegotiationRecord]] = {}
    if intent_ids:
        neg_rows = (
            db.query(models.NegotiationRecord)
            .filter(
                models.NegotiationRecord.intent_id.in_(intent_ids),
                models.NegotiationRecord.created_at < cutoff,
            )
            .order_by(
                models.NegotiationRecord.round,
                models.NegotiationRecord.held_at,
            )
            .all()
        )
        for row in neg_rows:
            negotiations_by_intent.setdefault(row.intent_id, []).append(row)
    reports = (
        db.query(models.MonthlyCapacityReport)
        .filter(
            models.MonthlyCapacityReport.project_id == project.id,
            models.MonthlyCapacityReport.created_at < cutoff,
        )
        .all()
    )
    follow_ups = (
        db.query(models.CapacityFollowUp)
        .filter(
            models.CapacityFollowUp.project_id == project.id,
            models.CapacityFollowUp.created_at < cutoff,
        )
        .order_by(
            models.CapacityFollowUp.created_at.desc(),
            models.CapacityFollowUp.id.desc(),
        )
        .all()
    )
    status_logs = (
        db.query(models.ProjectStatusLog)
        .filter(
            models.ProjectStatusLog.project_id == project.id,
            models.ProjectStatusLog.changed_at < cutoff,
        )
        .order_by(models.ProjectStatusLog.changed_at.desc())
        .all()
    )

    risks, missing, summaries = _evaluate(
        as_of,
        project,
        status_at_snapshot,
        approval,
        milestones,
        reports,
        follow_ups,
    )

    payload = {
        "snapshot_meta": {
            "project_id": project.id,
            "project_name": project.name,
            "project_code": project.project_code,
            "as_of_date": _iso_date(as_of),
            "cutoff_at": _iso(cutoff),
            "generated_at": _iso(generated_at),
            "generated_by": generated_by,
            "status_at_snapshot": status_at_snapshot.value,
            "status_basis": status_basis,
            "risk_count": len(risks),
            "missing_field_count": len(missing),
        },
        "project": _freeze_project(project, status_at_snapshot),
        "investment": summaries["investment"],
        "capacity": summaries["capacity"],
        "approval": _freeze_approval(approval),
        "milestones": [_freeze_milestone(m) for m in milestones],
        "intents": [
            _freeze_intent(intent, negotiations_by_intent.get(intent.id, []))
            for intent in intents
        ],
        "follow_ups": [_freeze_follow_up(f) for f in follow_ups],
        "status_logs": [_freeze_status_log(log) for log in status_logs],
        "risk_items": risks,
        "missing_fields": missing,
        "sources": _build_sources(
            project,
            approval,
            milestones,
            intents,
            reports,
            follow_ups,
            len(status_logs),
            status_basis,
            latest_log_at,
        ),
    }
    return payload


def serialize_payload(payload: Dict[str, Any]) -> str:
    """确定性序列化：键排序、不转义中文，保证同一资料产生同一摘要。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def hash_payload(payload_text: str) -> str:
    return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()


def create_risk_snapshot(
    db: Session,
    project_id: int,
    as_of: date,
    user: Any,
) -> Tuple[Optional[models.RiskSnapshot], bool]:
    """生成（或幂等取回）风险快照。

    返回 (快照, 是否新生成)。同一项目同一时间点重复生成时返回已有快照。
    """
    project = (
        db.query(models.Project)
        .filter(models.Project.id == project_id)
        .first()
    )
    if project is None:
        return None, False
    # 时间点早于项目建档：此时项目尚不可见，生成快照没有业务意义
    if project.created_at is not None and as_of < project.created_at.date():
        raise ValueError(
            f"报告时间点 {as_of.isoformat()} 早于项目建档时间 "
            f"{project.created_at.date().isoformat()}，无法生成快照"
        )

    generated_by = {"name": user.name, "role": user.role}
    payload = build_snapshot_payload(db, project, as_of, generated_by)
    payload_text = serialize_payload(payload)
    payload_hash = hash_payload(payload_text)

    existing = (
        db.query(models.RiskSnapshot)
        .filter(
            models.RiskSnapshot.project_id == project_id,
            models.RiskSnapshot.as_of_date == as_of,
        )
        .first()
    )
    if existing is not None:
        return existing, False

    snapshot = models.RiskSnapshot(
        project_id=project_id,
        as_of_date=as_of,
        cutoff_at=cutoff_for(as_of),
        status_at_snapshot=ProjectStatus(payload["snapshot_meta"]["status_at_snapshot"]),
        project_name=project.name,
        payload=payload_text,
        payload_hash=payload_hash,
        risk_count=len(payload["risk_items"]),
        missing_field_count=len(payload["missing_fields"]),
        created_by_name=user.name,
        created_by_role=user.role,
    )
    db.add(snapshot)
    try:
        db.flush()
    except IntegrityError:
        # 并发下同一 (项目, 时间点) 唯一约束冲突，幂等取回
        db.rollback()
        existing = (
            db.query(models.RiskSnapshot)
            .filter(
                models.RiskSnapshot.project_id == project_id,
                models.RiskSnapshot.as_of_date == as_of,
            )
            .first()
        )
        return existing, False

    for order, item in enumerate(payload["risk_items"]):
        db.add(
            models.RiskSnapshotItem(
                snapshot_id=snapshot.id,
                sort_order=order,
                category=item["category"],
                severity=item["severity"],
                ref_type=item["ref_type"],
                ref_id=item["ref_id"],
                title=item["title"],
                detail=item["detail"],
            )
        )
    db.commit()
    db.refresh(snapshot)
    return snapshot, True


def get_snapshot(db: Session, snapshot_id: int) -> Optional[models.RiskSnapshot]:
    return (
        db.query(models.RiskSnapshot)
        .filter(models.RiskSnapshot.id == snapshot_id)
        .first()
    )


def list_snapshots(
    db: Session,
    project_id: Optional[int] = None,
    as_of: Optional[date] = None,
    skip: int = 0,
    limit: int = 100,
) -> List[models.RiskSnapshot]:
    query = db.query(models.RiskSnapshot)
    if project_id is not None:
        query = query.filter(models.RiskSnapshot.project_id == project_id)
    if as_of is not None:
        query = query.filter(models.RiskSnapshot.as_of_date == as_of)
    return (
        query.order_by(
            models.RiskSnapshot.as_of_date.desc(),
            models.RiskSnapshot.created_at.desc(),
            models.RiskSnapshot.id.desc(),
        )
        .offset(skip)
        .limit(limit)
        .all()
    )


def payload_dict(snapshot: models.RiskSnapshot) -> Dict[str, Any]:
    return json.loads(snapshot.payload)


def record_access(
    db: Session, snapshot: models.RiskSnapshot, user: Any, action: str
) -> models.RiskSnapshotAccessLog:
    log = models.RiskSnapshotAccessLog(
        snapshot_id=snapshot.id,
        accessed_by_name=user.name,
        accessed_by_role=user.role,
        action=action,
    )
    db.add(log)
    snapshot.download_count = (snapshot.download_count or 0) + (
        1 if action == "download" else 0
    )
    db.commit()
    db.refresh(log)
    return log


def list_access_logs(
    db: Session, snapshot_id: int
) -> List[models.RiskSnapshotAccessLog]:
    return (
        db.query(models.RiskSnapshotAccessLog)
        .filter(models.RiskSnapshotAccessLog.snapshot_id == snapshot_id)
        .order_by(
            models.RiskSnapshotAccessLog.accessed_at.desc(),
            models.RiskSnapshotAccessLog.id.desc(),
        )
        .all()
    )
