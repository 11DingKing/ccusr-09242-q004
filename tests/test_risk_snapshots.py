"""项目风险快照接口测试。

覆盖：时间点边界、关联记录缺失、快照重复生成（冻结语义）、权限不足、
联系人敏感字段剔除以及风险项/跟进事项的稳定排序。
"""

import os
import tempfile
import unittest
from datetime import date, datetime

# 在导入应用前指向独立临时数据库，避免污染仓库中的 invest_ledger.db
_TMP_DB = tempfile.NamedTemporaryFile(
    prefix="risk_snapshots_", suffix=".db", delete=False
)
_TMP_DB.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.enums import (  # noqa: E402
    FollowUpPriority,
    FollowUpStatus,
    MilestoneStatus,
    MilestoneType,
    ParkType,
    ProjectStatus,
    Region,
)

ADMIN = {"X-User-Role": "admin"}
INVESTMENT_LEAD = {"X-User-Role": "investment_lead"}
VIEWER = {"X-User-Role": "viewer"}

SNAPSHOT_URL = "/api/v1/projects/{pid}/risk-snapshots"


class RiskSnapshotApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()
        os.unlink(_TMP_DB.name)

    def setUp(self):
        db = SessionLocal()
        try:
            for table in reversed(Base.metadata.sorted_tables):
                db.execute(table.delete())
            db.commit()
        finally:
            db.close()

    # ---------- 数据构造辅助 ----------

    def _make_project(self, name="快照测试项目", code="SNAP-001", **overrides):
        """创建园区 + 主体 + 项目（含联系人敏感字段），返回 (项目, 园区, 主体) ID。"""
        db = SessionLocal()
        try:
            park = models.IndustrialPark(
                name=f"{name}-园区",
                park_type=ParkType.KEY_INDUSTRIAL,
                city="南宁市",
                contact_person="园区联络员",
                contact_phone="0771-5550001",
            )
            entity = models.Entity(
                name=f"{name}-主体",
                region=Region.GUANGXI,
                country_or_province="广西",
                contact_person="王联系人",
                contact_phone="13877776666",
                contact_email="contact@example.com",
            )
            db.add_all([park, entity])
            db.flush()
            defaults = dict(
                name=name,
                project_code=code,
                status=ProjectStatus.ATTRACTING_INVESTMENT,
                investment_direction="芒果深加工",
                planned_investment_10k=5000.0,
                expected_annual_capacity_tonnes=1200.0,
                expected_output_value_10k=9000.0,
                expected_jobs=150,
                promised_monthly_capacity_tonnes=100.0,
                park_id=park.id,
                initiator_id=entity.id,
                responsible_department="招商一局",
                project_leader="李项目负责人",
                leader_phone="13900001111",
                created_at=datetime(2026, 1, 1, 0, 0, 0),
            )
            defaults.update(overrides)
            project = models.Project(**defaults)
            db.add(project)
            db.commit()
            return project.id, park.id, entity.id
        finally:
            db.close()

    def _add_records(self, *objs):
        db = SessionLocal()
        try:
            db.add_all(objs)
            db.commit()
        finally:
            db.close()

    def _commissioned_log(self, project_id, changed_at):
        return models.ProjectStatusLog(
            project_id=project_id,
            from_status=ProjectStatus.UNDER_CONSTRUCTION,
            to_status=ProjectStatus.COMMISSIONED,
            changed_at=changed_at,
            reason="正式投产里程碑完成，项目已投产",
        )

    def _seed_full_project(self):
        """已投产项目：立项 + 里程碑 + 产能报告 + 未解决/已解决跟进事项。"""
        project_id, park_id, entity_id = self._make_project(
            status=ProjectStatus.COMMISSIONED
        )
        self._add_records(
            models.ProjectApproval(
                project_id=project_id,
                approval_number="TEST-AP-001",
                approval_date=date(2026, 2, 1),
                approving_authority="市发改委",
                agreed_investment_10k=4800.0,
                created_at=datetime(2026, 2, 1, 9, 0, 0),
            ),
            models.ProjectMilestone(
                project_id=project_id,
                sequence=1,
                milestone_type=MilestoneType.FOUNDATION,
                name="奠基开工",
                status=MilestoneStatus.COMPLETED,
                planned_date=date(2026, 3, 1),
                actual_date=date(2026, 3, 2),
                completion_rate=100.0,
                created_at=datetime(2026, 2, 1, 10, 0, 0),
            ),
            models.ProjectMilestone(
                project_id=project_id,
                sequence=2,
                milestone_type=MilestoneType.OFFICIAL_PRODUCTION,
                name="正式投产运营",
                status=MilestoneStatus.IN_PROGRESS,
                planned_date=date(2026, 12, 1),
                completion_rate=60.0,
                created_at=datetime(2026, 2, 1, 11, 0, 0),
            ),
            models.MonthlyCapacityReport(
                project_id=project_id,
                report_year=2026,
                report_month=3,
                actual_output_tonnes=80.0,
                capacity_utilization_rate=80.0,
                local_material_procurement_10k=500.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
            ),
            models.CapacityFollowUp(
                project_id=project_id,
                title="3月产能未达标",
                status=FollowUpStatus.PENDING,
                priority=FollowUpPriority.MEDIUM,
                gap_percentage=20.0,
                deadline=date(2026, 5, 1),
                created_at=datetime(2026, 4, 1, 10, 0, 0),
            ),
            models.CapacityFollowUp(
                project_id=project_id,
                title="历史遗留问题",
                status=FollowUpStatus.RESOLVED,
                priority=FollowUpPriority.LOW,
                created_at=datetime(2026, 4, 2, 10, 0, 0),
            ),
            self._commissioned_log(project_id, datetime(2026, 3, 5, 9, 0, 0)),
        )
        return project_id

    # ---------- 主流程 ----------

    def test_generate_snapshot_freezes_full_report(self):
        project_id = self._seed_full_project()

        resp = self.client.post(
            SNAPSHOT_URL.format(pid=project_id),
            json={"as_of": "2026-06-01T00:00:00"},
            headers=ADMIN,
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        report = body["report"]

        self.assertEqual(body["project_id"], project_id)
        self.assertEqual(body["as_of"], "2026-06-01T00:00:00")
        self.assertEqual(body["generated_by"], "admin")
        self.assertEqual(len(body["payload_hash"]), 64)

        # 投资额：规划值来自项目，协议值来自立项记录，来源可追溯
        self.assertEqual(report["investment"]["planned_investment_10k"], 5000.0)
        self.assertEqual(report["investment"]["agreed_investment_10k"], 4800.0)
        self.assertEqual(
            [s["field"] for s in report["investment"]["sources"]],
            ["planned_investment_10k", "agreed_investment_10k"],
        )
        self.assertEqual(
            report["investment"]["sources"][1]["source_time"],
            "2026-02-01T09:00:00",
        )

        # 项目状态还原到基准时点
        self.assertEqual(report["project"]["status"], "已投产")

        # 里程碑按 sequence 稳定排序
        self.assertEqual([m["sequence"] for m in report["milestones"]], [1, 2])

        # 产能兑现取基准时间前最新一条月度登记
        self.assertEqual(report["capacity"]["latest_report"]["report_month"], 3)
        self.assertEqual(report["capacity"]["utilization_rate"], 80.0)
        self.assertEqual(
            report["capacity"]["promised_monthly_capacity_tonnes"], 100.0
        )

        # 未解决跟进事项：已解决的不纳入
        self.assertEqual(
            [f["title"] for f in report["open_follow_ups"]], ["3月产能未达标"]
        )

        # 风险项按 (严重度, 编码) 稳定排序
        self.assertEqual(
            [r["code"] for r in report["risk_items"]],
            ["FOLLOW_UP_OVERDUE", "CAPACITY_UTILIZATION_GAP", "FOLLOW_UP_OPEN"],
        )
        self.assertEqual(report["missing_fields"], [])

        # 来源时间可解释计算依据
        self.assertEqual(
            report["source_times"]["approval_created_at"], "2026-02-01T09:00:00"
        )
        self.assertEqual(
            report["source_times"]["latest_capacity_report_created_at"],
            "2026-04-01T09:00:00",
        )
        self.assertIsNotNone(report["source_times"]["project_updated_at"])

    def test_generated_by_uses_operator_field(self):
        project_id, _, _ = self._make_project()
        resp = self.client.post(
            SNAPSHOT_URL.format(pid=project_id),
            json={"as_of": "2026-06-01T00:00:00", "operator": "张科长"},
            headers=ADMIN,
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["generated_by"], "张科长")

    # ---------- 时间点边界 ----------

    def test_as_of_defaults_to_now(self):
        project_id, _, _ = self._make_project()
        resp = self.client.post(SNAPSHOT_URL.format(pid=project_id), headers=ADMIN)
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertIsNotNone(body["as_of"])
        self.assertEqual(body["report"]["project"]["status"], "招商中")

    def test_as_of_before_project_created_rejected(self):
        project_id, _, _ = self._make_project()
        resp = self.client.post(
            SNAPSHOT_URL.format(pid=project_id),
            json={"as_of": "2025-12-31T23:59:59"},
            headers=ADMIN,
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("早于项目创建时间", resp.json()["detail"])

    def test_as_of_boundary_includes_records_created_at_exact_moment(self):
        project_id, _, _ = self._make_project()
        self._add_records(
            models.ProjectMilestone(
                project_id=project_id,
                sequence=1,
                milestone_type=MilestoneType.FOUNDATION,
                name="奠基开工",
                status=MilestoneStatus.NOT_STARTED,
                planned_date=date(2026, 3, 1),
                created_at=datetime(2026, 3, 1, 10, 0, 0),
            )
        )

        # 边界：created_at == as_of 的记录应纳入快照
        resp = self.client.post(
            SNAPSHOT_URL.format(pid=project_id),
            json={"as_of": "2026-03-01T10:00:00"},
            headers=ADMIN,
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(len(resp.json()["report"]["milestones"]), 1)

        # 早一秒：记录尚未创建，不应纳入
        resp = self.client.post(
            SNAPSHOT_URL.format(pid=project_id),
            json={"as_of": "2026-03-01T09:59:59"},
            headers=ADMIN,
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["report"]["milestones"], [])

    def test_as_of_with_timezone_is_normalized(self):
        project_id, _, _ = self._make_project()
        resp = self.client.post(
            SNAPSHOT_URL.format(pid=project_id),
            json={"as_of": "2026-06-01T08:00:00+08:00"},
            headers=ADMIN,
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["as_of"], "2026-06-01T00:00:00")

    def test_status_reconstructed_from_logs_at_as_of(self):
        project_id, _, _ = self._make_project(status=ProjectStatus.ESTABLISHED)
        self._add_records(
            models.ProjectStatusLog(
                project_id=project_id,
                from_status=ProjectStatus.ATTRACTING_INVESTMENT,
                to_status=ProjectStatus.NEGOTIATING,
                changed_at=datetime(2026, 2, 1, 9, 0, 0),
            ),
            models.ProjectStatusLog(
                project_id=project_id,
                from_status=ProjectStatus.NEGOTIATING,
                to_status=ProjectStatus.ESTABLISHED,
                changed_at=datetime(2026, 3, 1, 9, 0, 0),
            ),
        )

        cases = [
            ("2026-02-15T00:00:00", "洽谈中"),
            ("2026-03-02T00:00:00", "已立项"),
        ]
        for as_of, expected in cases:
            resp = self.client.post(
                SNAPSHOT_URL.format(pid=project_id),
                json={"as_of": as_of},
                headers=ADMIN,
            )
            self.assertEqual(resp.status_code, 201, resp.text)
            self.assertEqual(
                resp.json()["report"]["project"]["status"], expected
            )

    # ---------- 关联记录缺失 ----------

    def test_missing_approval_and_fields_flagged(self):
        # 已立项但无立项记录，且多个关键字段缺失
        project_id, _, _ = self._make_project(
            name="缺资料项目",
            code="SNAP-002",
            status=ProjectStatus.ESTABLISHED,
            expected_output_value_10k=None,
            expected_jobs=None,
            project_leader=None,
            responsible_department=None,
        )
        self._add_records(
            models.ProjectStatusLog(
                project_id=project_id,
                from_status=ProjectStatus.NEGOTIATING,
                to_status=ProjectStatus.ESTABLISHED,
                changed_at=datetime(2026, 2, 1, 9, 0, 0),
            )
        )

        resp = self.client.post(
            SNAPSHOT_URL.format(pid=project_id),
            json={"as_of": "2026-06-01T00:00:00"},
            headers=ADMIN,
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        report = resp.json()["report"]

        codes = [r["code"] for r in report["risk_items"]]
        self.assertIn("APPROVAL_MISSING", codes)
        self.assertIsNone(report["investment"]["agreed_investment_10k"])
        # 缺失字段按字段名字典序返回
        self.assertEqual(
            report["missing_fields"],
            [
                "agreed_investment_10k",
                "expected_jobs",
                "expected_output_value_10k",
                "project_leader",
                "responsible_department",
            ],
        )

    def test_commissioned_project_without_capacity_report(self):
        # 已投产但无月度产能登记，且未填承诺产能与预期产能
        project_id, _, _ = self._make_project(
            name="无产能登记项目",
            code="SNAP-003",
            status=ProjectStatus.COMMISSIONED,
            promised_monthly_capacity_tonnes=None,
            expected_annual_capacity_tonnes=None,
        )
        self._add_records(
            self._commissioned_log(project_id, datetime(2026, 3, 1, 9, 0, 0))
        )

        resp = self.client.post(
            SNAPSHOT_URL.format(pid=project_id),
            json={"as_of": "2026-06-01T00:00:00"},
            headers=ADMIN,
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        report = resp.json()["report"]

        codes = [r["code"] for r in report["risk_items"]]
        self.assertIn("CAPACITY_REPORT_MISSING", codes)
        self.assertIn("PROMISED_CAPACITY_MISSING", codes)
        self.assertIsNone(report["capacity"]["latest_report"])
        self.assertIsNone(report["capacity"]["utilization_rate"])
        self.assertIn("latest_capacity_report", report["missing_fields"])
        self.assertIn(
            "promised_monthly_capacity_tonnes", report["missing_fields"]
        )

    # ---------- 快照重复生成（冻结语义） ----------

    def test_duplicate_generation_returns_frozen_snapshot(self):
        project_id, _, _ = self._make_project()
        url = SNAPSHOT_URL.format(pid=project_id)

        first = self.client.post(
            url, json={"as_of": "2026-06-01T00:00:00"}, headers=ADMIN
        )
        self.assertEqual(first.status_code, 201, first.text)
        first_body = first.json()
        self.assertEqual(
            first_body["report"]["investment"]["planned_investment_10k"], 5000.0
        )

        # 快照生成后修改项目投资额
        db = SessionLocal()
        try:
            project = db.get(models.Project, project_id)
            project.planned_investment_10k = 9000.0
            db.commit()
        finally:
            db.close()

        # 同一基准时间重复生成：返回已冻结快照，不受后续修改影响
        second = self.client.post(
            url, json={"as_of": "2026-06-01T00:00:00"}, headers=ADMIN
        )
        self.assertEqual(second.status_code, 200, second.text)
        second_body = second.json()
        self.assertEqual(second_body["id"], first_body["id"])
        self.assertEqual(second_body["payload_hash"], first_body["payload_hash"])
        self.assertEqual(
            second_body["report"]["investment"]["planned_investment_10k"],
            5000.0,
        )

        # 授权人员再次下载，内容仍为冻结值
        download = self.client.get(f"{url}/{first_body['id']}", headers=ADMIN)
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(
            download.json()["report"]["investment"]["planned_investment_10k"],
            5000.0,
        )

        # 换一个基准时间则生成新快照，反映最新值
        third = self.client.post(
            url, json={"as_of": "2026-06-02T00:00:00"}, headers=ADMIN
        )
        self.assertEqual(third.status_code, 201, third.text)
        self.assertNotEqual(third.json()["id"], first_body["id"])
        self.assertEqual(
            third.json()["report"]["investment"]["planned_investment_10k"],
            9000.0,
        )

    # ---------- 权限不足 ----------

    def test_snapshot_requires_authorized_role(self):
        project_id, _, _ = self._make_project()
        url = SNAPSHOT_URL.format(pid=project_id)
        payload = {"as_of": "2026-06-01T00:00:00"}

        # 未提供角色 / 未知角色 / 无权限角色：生成均被拒绝
        self.assertEqual(self.client.post(url, json=payload).status_code, 403)
        self.assertEqual(
            self.client.post(
                url, json=payload, headers={"X-User-Role": "guest"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(url, json=payload, headers=VIEWER).status_code,
            403,
        )

        # 授权角色生成成功
        created = self.client.post(url, json=payload, headers=ADMIN)
        self.assertEqual(created.status_code, 201, created.text)
        snapshot_id = created.json()["id"]

        # viewer / 无角色：下载（列表与详情）均被拒绝
        self.assertEqual(self.client.get(url, headers=VIEWER).status_code, 403)
        self.assertEqual(
            self.client.get(f"{url}/{snapshot_id}", headers=VIEWER).status_code,
            403,
        )
        self.assertEqual(self.client.get(url).status_code, 403)

        # 授权角色可再次下载
        self.assertEqual(self.client.get(url, headers=ADMIN).status_code, 200)
        self.assertEqual(
            self.client.get(f"{url}/{snapshot_id}", headers=ADMIN).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"{url}/{snapshot_id}", headers=INVESTMENT_LEAD
            ).status_code,
            200,
        )

    # ---------- 敏感字段剔除 ----------

    def test_snapshot_omits_contact_sensitive_fields(self):
        project_id, _, _ = self._make_project()
        created = self.client.post(
            SNAPSHOT_URL.format(pid=project_id),
            json={"as_of": "2026-06-01T00:00:00"},
            headers=ADMIN,
        )
        self.assertEqual(created.status_code, 201, created.text)
        body = created.text
        for leaked in (
            "13877776666",
            "contact@example.com",
            "13900001111",
            "0771-5550001",
            "contact_person",
            "contact_phone",
            "contact_email",
            "leader_phone",
        ):
            self.assertNotIn(leaked, body)

        # 负责人姓名与部门保留，满足例会使用
        report = created.json()["report"]
        self.assertEqual(report["project"]["project_leader"], "李项目负责人")
        self.assertEqual(report["project"]["responsible_department"], "招商一局")

    # ---------- 稳定排序 ----------

    def test_open_follow_ups_and_risks_have_stable_ordering(self):
        project_id, _, _ = self._make_project(status=ProjectStatus.COMMISSIONED)
        self._add_records(
            self._commissioned_log(project_id, datetime(2026, 3, 5, 9, 0, 0)),
            models.ProjectApproval(
                project_id=project_id,
                approval_number="TEST-AP-002",
                approval_date=date(2026, 2, 1),
                approving_authority="市发改委",
                agreed_investment_10k=4800.0,
                created_at=datetime(2026, 2, 1, 9, 0, 0),
            ),
            models.CapacityFollowUp(
                project_id=project_id,
                title="无截止日期事项",
                status=FollowUpStatus.IN_PROGRESS,
                priority=FollowUpPriority.LOW,
                deadline=None,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
            ),
            models.CapacityFollowUp(
                project_id=project_id,
                title="五月到期事项",
                status=FollowUpStatus.PENDING,
                priority=FollowUpPriority.MEDIUM,
                deadline=date(2026, 5, 1),
                created_at=datetime(2026, 4, 2, 9, 0, 0),
            ),
            models.CapacityFollowUp(
                project_id=project_id,
                title="四月到期事项",
                status=FollowUpStatus.PENDING,
                priority=FollowUpPriority.HIGH,
                deadline=date(2026, 4, 1),
                created_at=datetime(2026, 4, 3, 9, 0, 0),
            ),
            models.CapacityFollowUp(
                project_id=project_id,
                title="已解决事项",
                status=FollowUpStatus.RESOLVED,
                priority=FollowUpPriority.LOW,
                deadline=date(2026, 3, 1),
                created_at=datetime(2026, 4, 4, 9, 0, 0),
            ),
        )

        resp = self.client.post(
            SNAPSHOT_URL.format(pid=project_id),
            json={"as_of": "2026-06-01T00:00:00"},
            headers=ADMIN,
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        report = resp.json()["report"]

        # 未解决跟进：有截止日期的按日期升序在前，无日期的在后；已解决的不纳入
        self.assertEqual(
            [f["title"] for f in report["open_follow_ups"]],
            ["四月到期事项", "五月到期事项", "无截止日期事项"],
        )

        # 风险项：高严重度在前，同级按编码字典序
        self.assertEqual(
            [r["code"] for r in report["risk_items"]],
            ["CAPACITY_REPORT_MISSING", "FOLLOW_UP_OVERDUE", "FOLLOW_UP_OPEN"],
        )
        self.assertEqual(
            report["risk_items"][1]["count"], 2  # 两项跟进已过截止日期
        )

    # ---------- 404 与列表 ----------

    def test_snapshot_not_found_cases(self):
        project_id, _, _ = self._make_project()
        other_id, _, _ = self._make_project(name="另一个项目", code="SNAP-004")

        created = self.client.post(
            SNAPSHOT_URL.format(pid=project_id),
            json={"as_of": "2026-06-01T00:00:00"},
            headers=ADMIN,
        )
        self.assertEqual(created.status_code, 201, created.text)
        snapshot_id = created.json()["id"]

        # 项目不存在
        self.assertEqual(
            self.client.post(
                SNAPSHOT_URL.format(pid=99999),
                json={"as_of": "2026-06-01T00:00:00"},
                headers=ADMIN,
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(SNAPSHOT_URL.format(pid=99999), headers=ADMIN).status_code,
            404,
        )
        # 快照不存在
        self.assertEqual(
            self.client.get(
                f"{SNAPSHOT_URL.format(pid=project_id)}/99999", headers=ADMIN
            ).status_code,
            404,
        )
        # 快照不属于该项目
        self.assertEqual(
            self.client.get(
                f"{SNAPSHOT_URL.format(pid=other_id)}/{snapshot_id}",
                headers=ADMIN,
            ).status_code,
            404,
        )

    def test_list_snapshots_returns_metadata_only(self):
        project_id, _, _ = self._make_project()
        url = SNAPSHOT_URL.format(pid=project_id)
        for as_of in ("2026-05-01T00:00:00", "2026-06-01T00:00:00"):
            resp = self.client.post(url, json={"as_of": as_of}, headers=ADMIN)
            self.assertEqual(resp.status_code, 201, resp.text)

        resp = self.client.get(url, headers=ADMIN)
        self.assertEqual(resp.status_code, 200, resp.text)
        items = resp.json()
        self.assertEqual(len(items), 2)
        # 按基准时间倒序
        self.assertEqual(
            [i["as_of"] for i in items],
            ["2026-06-01T00:00:00", "2026-05-01T00:00:00"],
        )
        for item in items:
            self.assertNotIn("report", item)
            self.assertEqual(len(item["payload_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
