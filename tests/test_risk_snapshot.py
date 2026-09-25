"""风险快照接口测试。

覆盖：
1. 时间点边界（cutoff 次日零点、created_at 恰好等于边界、状态日志重放）
2. 关联记录缺失（无立项/无里程碑/已投产无产能报告/项目不存在）
3. 快照重复生成（幂等、摘要一致、后续项目修改不影响已冻结快照、排序稳定）
4. 权限不足（401/403、viewer 可读不可生成、审计仅负责人）与敏感字段不泄露
"""

import json
import unittest
from datetime import date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.database import Base, get_db
from app.main import app
from app import models
from app.enums import (
    FollowUpPriority,
    FollowUpStatus,
    MilestoneStatus,
    MilestoneType,
    ProjectStatus,
    Region,
    ParkType,
)

# 固定的测试令牌
TEST_TOKENS = (
    "赵局长:director:director-test-token,"
    "钱分析师:analyst:analyst-test-token,"
    "孙专员:viewer:viewer-test-token"
)
DIRECTOR_HEADERS = {"Authorization": "Bearer director-test-token"}
ANALYST_HEADERS = {"Authorization": "Bearer analyst-test-token"}
VIEWER_HEADERS = {"Authorization": "Bearer viewer-test-token"}

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class SnapshotTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls._orig_tokens = config.settings.RISK_SNAPSHOT_TOKENS
        config.settings.RISK_SNAPSHOT_TOKENS = TEST_TOKENS
        # 每个测试类都重新安装数据库覆盖（tearDownClass 会清除）
        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        config.settings.RISK_SNAPSHOT_TOKENS = cls._orig_tokens
        app.dependency_overrides.clear()

    def setUp(self):
        # 每个用例清空快照与业务数据
        db = TestingSessionLocal()
        db.query(models.RiskSnapshotAccessLog).delete()
        db.query(models.RiskSnapshotItem).delete()
        db.query(models.RiskSnapshot).delete()
        db.query(models.CapacityFollowUp).delete()
        db.query(models.MonthlyCapacityReport).delete()
        db.query(models.ProjectStatusLog).delete()
        db.query(models.ProjectMilestone).delete()
        db.query(models.ProjectApproval).delete()
        db.query(models.ProjectCategory).delete()
        db.query(models.Project).delete()
        db.query(models.IndustrialPark).delete()
        db.query(models.Entity).delete()
        db.commit()
        db.close()

    # ---------- 造数辅助 ----------
    def make_park(self, name="测试园区"):
        db = TestingSessionLocal()
        park = models.IndustrialPark(
            name=name,
            park_type=ParkType.KEY_INDUSTRIAL,
            city="南宁市",
            contact_person="园区敏感联系人",
            contact_phone="13700001111",
        )
        db.add(park)
        db.commit()
        db.refresh(park)
        db.close()
        return park.id

    def make_entity(self, name="测试主体"):
        db = TestingSessionLocal()
        entity = models.Entity(
            name=name,
            region=Region.GUANGXI,
            country_or_province="广西",
            city="南宁",
            contact_person="主体敏感联系人",
            contact_phone="13900009999",
            contact_email="secret@example.com",
        )
        db.add(entity)
        db.commit()
        db.refresh(entity)
        db.close()
        return entity.id

    def make_project(
        self,
        name="测试水果加工项目",
        status=ProjectStatus.ATTRACTING_INVESTMENT,
        planned_investment_10k=10000.0,
        expected_annual_capacity_tonnes=None,
        promised_monthly_capacity_tonnes=None,
        project_leader=None,
        leader_phone=None,
        responsible_department=None,
        expected_output_value_10k=None,
    ):
        db = TestingSessionLocal()
        project = models.Project(
            name=name,
            investment_direction="热带果汁加工",
            planned_investment_10k=planned_investment_10k,
            expected_annual_capacity_tonnes=expected_annual_capacity_tonnes,
            promised_monthly_capacity_tonnes=promised_monthly_capacity_tonnes,
            expected_output_value_10k=expected_output_value_10k,
            project_leader=project_leader,
            leader_phone=leader_phone,
            responsible_department=responsible_department,
            park_id=self.make_park(f"园区-{name}"),
            initiator_id=self.make_entity(f"主体-{name}"),
            status=status,
            created_at=datetime(2026, 1, 1, 0, 0, 0),
        )
        db.add(project)
        db.commit()
        db.refresh(project)
        db.close()
        return project.id

    def add_status_log(self, project_id, to_status, changed_at, from_status=None):
        db = TestingSessionLocal()
        db.add(
            models.ProjectStatusLog(
                project_id=project_id,
                from_status=from_status,
                to_status=to_status,
                changed_at=changed_at,
                reason="测试流转",
            )
        )
        db.commit()
        db.close()

    def generate(self, project_id, as_of, headers=ANALYST_HEADERS):
        return self.client.post(
            "/api/v1/risk-snapshots/",
            headers=headers,
            json={"project_id": project_id, "as_of_date": as_of.isoformat()},
        )


class TimeBoundaryTests(SnapshotTestBase):
    def test_cutoff_is_next_midnight_and_boundary_records_are_excluded(self):
        """边界：as_of 当天 23:59:59 的资料纳入，次日 00:00:00 的资料排除。"""
        pid = self.make_project(name="边界项目")
        self.add_status_log(
            pid, ProjectStatus.NEGOTIATING, datetime(2026, 3, 10, 9, 0)
        )
        # 时间点之后才发生的状态变化，快照不应看到
        self.add_status_log(
            pid, ProjectStatus.COMMISSIONED, datetime(2026, 3, 20, 9, 0),
            from_status=ProjectStatus.UNDER_CONSTRUCTION,
        )

        db = TestingSessionLocal()
        # 恰好在截止时刻之前
        db.add(
            models.ProjectMilestone(
                project_id=pid,
                sequence=1,
                milestone_type=MilestoneType.FOUNDATION,
                name="边界内里程碑",
                planned_date=date(2026, 4, 1),
                created_at=datetime(2026, 3, 15, 23, 59, 59),
            )
        )
        # 恰好等于 cutoff（3/16 00:00:00），必须排除
        db.add(
            models.ProjectMilestone(
                project_id=pid,
                sequence=2,
                milestone_type=MilestoneType.MAIN_STRUCTURE,
                name="边界外里程碑",
                planned_date=date(2026, 5, 1),
                created_at=datetime(2026, 3, 16, 0, 0, 0),
            )
        )
        db.add(
            models.MonthlyCapacityReport(
                project_id=pid,
                report_year=2026,
                report_month=2,
                actual_output_tonnes=100.0,
                created_at=datetime(2026, 3, 15, 12, 0),
            )
        )
        db.add(
            models.MonthlyCapacityReport(
                project_id=pid,
                report_year=2026,
                report_month=3,
                actual_output_tonnes=200.0,
                created_at=datetime(2026, 3, 16, 8, 0),
            )
        )
        db.add(
            models.CapacityFollowUp(
                project_id=pid,
                title="边界内跟进",
                created_at=datetime(2026, 3, 15, 8, 0),
            )
        )
        db.add(
            models.CapacityFollowUp(
                project_id=pid,
                title="边界外跟进",
                created_at=datetime(2026, 3, 17, 8, 0),
            )
        )
        db.add(
            models.CooperationIntent(
                project_id=pid,
                submitter_id=1,
                cooperation_content="边界内合作意向",
                submitted_at=datetime(2026, 3, 14, 10, 0),
            )
        )
        db.add(
            models.CooperationIntent(
                project_id=pid,
                submitter_id=1,
                cooperation_content="边界外合作意向",
                submitted_at=datetime(2026, 3, 16, 10, 0),
            )
        )
        db.commit()
        # 给边界内意向（id 随插入顺序，需查询）补一轮洽谈
        inner_intent = (
            db.query(models.CooperationIntent)
            .filter_by(cooperation_content="边界内合作意向")
            .first()
        )
        db.add(
            models.NegotiationRecord(
                intent_id=inner_intent.id,
                round=1,
                title="首轮洽谈",
                held_at=datetime(2026, 3, 15, 14, 0),
                key_topics="投资条款",
                created_at=datetime(2026, 3, 15, 18, 0),
            )
        )
        db.commit()
        db.close()

        resp = self.generate(pid, date(2026, 3, 15))
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        snap = body["snapshot"]

        self.assertTrue(body["newly_generated"])
        self.assertEqual(
            snap["snapshot_meta"]["cutoff_at"], "2026-03-16T00:00:00"
        )
        # 状态按日志重放：3/20 的投产日志不可见
        self.assertEqual(
            snap["snapshot_meta"]["status_at_snapshot"], "洽谈中"
        )
        self.assertEqual(snap["snapshot_meta"]["status_basis"], "status_log")
        # 关联记录按入库时间严格截止
        self.assertEqual(len(snap["milestones"]), 1)
        self.assertEqual(snap["milestones"][0]["name"], "边界内里程碑")
        self.assertEqual(snap["capacity"]["report_count"], 1)
        self.assertEqual(
            snap["capacity"]["latest_report"]["report_month"], 2
        )
        self.assertEqual(len(snap["follow_ups"]), 1)
        self.assertEqual(snap["follow_ups"][0]["title"], "边界内跟进")
        # 合作意向按提交时间截止，洽谈记录随意向冻结
        self.assertEqual(len(snap["intents"]), 1)
        self.assertEqual(
            snap["intents"][0]["cooperation_content"], "边界内合作意向"
        )
        self.assertEqual(len(snap["intents"][0]["negotiations"]), 1)
        self.assertEqual(
            snap["intents"][0]["negotiations"][0]["title"], "首轮洽谈"
        )
        # 来源计数与之一致
        source_counts = {s["entity"]: s["record_count"] for s in snap["sources"]}
        self.assertEqual(source_counts["里程碑"], 1)
        self.assertEqual(source_counts["合作意向"], 1)
        self.assertEqual(source_counts["月度产能报告"], 1)
        self.assertEqual(source_counts["产能跟进事项"], 1)

    def test_snapshot_as_of_different_dates_yields_different_scope(self):
        """同一项目不同时间点看到的资料范围不同，且互不影响。"""
        pid = self.make_project(name="跨期项目")
        db = TestingSessionLocal()
        db.add(
            models.ProjectApproval(
                project_id=pid,
                approval_number="立字[2026]第1号",
                approval_date=date(2026, 3, 1),
                approving_authority="市发改局",
                agreed_investment_10k=9000.0,
                created_at=datetime(2026, 3, 5, 10, 0),
            )
        )
        db.commit()
        db.close()

        early = self.generate(pid, date(2026, 3, 1)).json()["snapshot"]
        later = self.generate(pid, date(2026, 3, 31)).json()["snapshot"]
        self.assertIsNone(early["approval"])
        self.assertIsNotNone(later["approval"])
        self.assertEqual(
            later["investment"]["agreed_investment_10k"], 9000.0
        )


class MissingRelationTests(SnapshotTestBase):
    def test_project_with_no_related_records_still_snapshotted(self):
        """招商中项目没有立项/里程碑/报告时快照正常生成，缺失字段被列出。"""
        pid = self.make_project(name="裸项目")  # 多个可选字段为空
        resp = self.generate(pid, date(2026, 6, 1))
        self.assertEqual(resp.status_code, 201, resp.text)
        snap = resp.json()["snapshot"]

        self.assertIsNone(snap["approval"])
        self.assertEqual(snap["milestones"], [])
        self.assertEqual(snap["follow_ups"], [])
        self.assertFalse(snap["capacity"]["eligible"])
        self.assertEqual(snap["capacity"]["report_count"], 0)

        missing_fields = {m["field"] for m in snap["missing_fields"]}
        self.assertIn("expected_annual_capacity_tonnes", missing_fields)
        self.assertIn("responsible_department", missing_fields)
        self.assertIn("project_leader", missing_fields)
        # 缺失字段稳定排序：固定字段序
        fields = [m["field"] for m in snap["missing_fields"]]
        self.assertEqual(fields, sorted(fields, key=lambda f: fields.index(f)))

    def test_commissioned_project_without_capacity_report_is_a_risk(self):
        """已投产但无月度产能报告 -> 高风险，且承诺产能缺失进缺失字段。"""
        pid = self.make_project(
            name="投产无报告项目",
            status=ProjectStatus.COMMISSIONED,
            promised_monthly_capacity_tonnes=500.0,
            project_leader="有人负责",
            responsible_department="招商局",
            expected_annual_capacity_tonnes=6000.0,
        )
        resp = self.generate(pid, date(2026, 6, 1))
        self.assertEqual(resp.status_code, 201, resp.text)
        snap = resp.json()["snapshot"]

        self.assertEqual(
            snap["snapshot_meta"]["status_at_snapshot"], "已投产"
        )
        categories = [r["category"] for r in snap["risk_items"]]
        self.assertIn("产能兑现风险", categories)
        no_report = next(
            r for r in snap["risk_items"] if "尚无月度产能报告" in r["title"]
        )
        self.assertEqual(no_report["severity"], "high")

    def test_snapshot_for_nonexistent_project_returns_404(self):
        resp = self.generate(99999, date(2026, 6, 1))
        self.assertEqual(resp.status_code, 404)

    def test_as_of_before_project_creation_is_rejected(self):
        """时间点早于项目建档时间应返回 400，且不生成快照。"""
        pid = self.make_project(name="新建项目")
        resp = self.generate(pid, date(2000, 1, 1))
        self.assertEqual(resp.status_code, 400)
        db = TestingSessionLocal()
        self.assertEqual(
            db.query(models.RiskSnapshot)
            .filter(models.RiskSnapshot.project_id == pid)
            .count(),
            0,
        )
        db.close()

    def test_missing_completion_deadline_reported(self):
        """立项信息缺少竣工期限与协议产能时进入缺失字段清单。"""
        pid = self.make_project(name="缺字段立项项目")
        db = TestingSessionLocal()
        db.add(
            models.ProjectApproval(
                project_id=pid,
                approval_number="立字[2026]第8号",
                approval_date=date(2026, 1, 10),
                approving_authority="市发改局",
                agreed_investment_10k=10000.0,
                agreed_capacity_tonnes=None,
                completion_deadline=None,
                created_at=datetime(2026, 1, 11, 10, 0),
            )
        )
        db.commit()
        db.close()
        snap = self.generate(pid, date(2026, 2, 1)).json()["snapshot"]
        missing = {(m["scope"], m["field"]) for m in snap["missing_fields"]}
        self.assertIn(("approval", "completion_deadline"), missing)
        self.assertIn(("approval", "agreed_capacity_tonnes"), missing)


class ReproducibilityTests(SnapshotTestBase):
    def test_duplicate_generation_is_idempotent_and_immutable(self):
        pid = self.make_project(
            name="冻结项目",
            status=ProjectStatus.COMMISSIONED,
            promised_monthly_capacity_tonnes=100.0,
            expected_annual_capacity_tonnes=1200.0,
            project_leader="有人",
            responsible_department="招商局",
            expected_output_value_10k=5000.0,
        )
        db = TestingSessionLocal()
        db.add(
            models.MonthlyCapacityReport(
                project_id=pid,
                report_year=2026,
                report_month=4,
                actual_output_tonnes=10.0,  # 达产率 10% -> 紧急
                capacity_utilization_rate=10.0,
                created_at=datetime(2026, 5, 5, 10, 0),
            )
        )
        db.add(
            models.CapacityFollowUp(
                project_id=pid,
                title="逾期未解决跟进",
                status=FollowUpStatus.PENDING,
                priority=FollowUpPriority.HIGH,
                deadline=date(2026, 4, 30),
                created_at=datetime(2026, 5, 6, 10, 0),
            )
        )
        db.add(
            models.ProjectMilestone(
                project_id=pid,
                sequence=1,
                milestone_type=MilestoneType.FOUNDATION,
                name="延期里程碑",
                status=MilestoneStatus.DELAYED,
                planned_date=date(2026, 3, 1),
                created_at=datetime(2026, 2, 1, 10, 0),
            )
        )
        db.commit()
        db.close()

        as_of = date(2026, 6, 1)
        first = self.generate(pid, as_of)
        self.assertEqual(first.status_code, 201, first.text)
        first_body = first.json()
        self.assertTrue(first_body["newly_generated"])
        snapshot_id = first_body["snapshot_id"]

        # 再次生成：幂等返回同一份
        second = self.generate(pid, as_of)
        self.assertEqual(second.status_code, 200, second.text)
        second_body = second.json()
        self.assertFalse(second_body["newly_generated"])
        self.assertEqual(second_body["snapshot_id"], snapshot_id)

        first_text = json.dumps(
            first_body["snapshot"], ensure_ascii=False, sort_keys=True
        )
        second_text = json.dumps(
            second_body["snapshot"], ensure_ascii=False, sort_keys=True
        )
        self.assertEqual(first_text, second_text)

        # 风险项稳定排序：紧急产能风险排在最前
        risks = first_body["snapshot"]["risk_items"]
        self.assertEqual(risks[0]["category"], "产能兑现风险")
        self.assertEqual(risks[0]["severity"], "urgent")
        severities = [r["severity"] for r in risks]
        self.assertEqual(severities, sorted(severities, key=["urgent", "high", "medium", "low"].index))

        # 快照生成后修改项目，已生成载荷不受影响
        upd = self.client.put(
            f"/api/v1/projects/{pid}",
            json={"name": "冻结项目-已改名", "planned_investment_10k": 99999.0},
        )
        self.assertEqual(upd.status_code, 200, upd.text)

        got = self.client.get(
            f"/api/v1/risk-snapshots/{snapshot_id}", headers=DIRECTOR_HEADERS
        )
        self.assertEqual(got.status_code, 200, got.text)
        frozen = got.json()
        self.assertEqual(frozen["project"]["name"], "冻结项目")
        self.assertEqual(frozen["project"]["planned_investment_10k"], 10000.0)
        self.assertEqual(
            frozen["snapshot_meta"]["project_name"], "冻结项目"
        )

        # 清单中的项目名同样冻结
        listing = self.client.get(
            f"/api/v1/risk-snapshots/?project_id={pid}", headers=VIEWER_HEADERS
        ).json()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["project_name"], "冻结项目")

        # 下载内容与冻结载荷一致，且带摘要头
        dl = self.client.get(
            f"/api/v1/risk-snapshots/{snapshot_id}/download",
            headers=VIEWER_HEADERS,
        )
        self.assertEqual(dl.status_code, 200)
        self.assertIn("attachment", dl.headers["content-disposition"])
        self.assertEqual(
            dl.headers["x-snapshot-hash"], listing[0]["payload_hash"]
        )
        # 下载文件字节的 SHA-256 必须等于摘要头，可离线核验
        import hashlib

        self.assertEqual(
            hashlib.sha256(dl.content).hexdigest(),
            dl.headers["x-snapshot-hash"],
        )
        self.assertEqual(
            json.dumps(dl.json(), ensure_ascii=False, sort_keys=True),
            json.dumps(frozen, ensure_ascii=False, sort_keys=True),
        )


class PermissionAndSensitiveDataTests(SnapshotTestBase):
    def test_missing_or_invalid_token_is_unauthorized(self):
        pid = self.make_project(name="权限项目")
        # 无令牌
        r1 = self.client.post(
            "/api/v1/risk-snapshots/",
            json={"project_id": pid, "as_of_date": "2026-06-01"},
        )
        self.assertEqual(r1.status_code, 401)
        # 错误令牌
        r2 = self.client.post(
            "/api/v1/risk-snapshots/",
            headers={"Authorization": "Bearer not-a-real-token"},
            json={"project_id": pid, "as_of_date": "2026-06-01"},
        )
        self.assertEqual(r2.status_code, 401)
        # 查看同样需要令牌
        r3 = self.client.get("/api/v1/risk-snapshots/")
        self.assertEqual(r3.status_code, 401)

    def test_viewer_cannot_generate_but_can_download(self):
        pid = self.make_project(name="专员项目")
        denied = self.generate(pid, date(2026, 6, 1), headers=VIEWER_HEADERS)
        self.assertEqual(denied.status_code, 403)

        created = self.generate(pid, date(2026, 6, 1), headers=ANALYST_HEADERS)
        self.assertEqual(created.status_code, 201)
        snapshot_id = created.json()["snapshot_id"]

        ok = self.client.get(
            f"/api/v1/risk-snapshots/{snapshot_id}", headers=VIEWER_HEADERS
        )
        self.assertEqual(ok.status_code, 200)
        dl = self.client.get(
            f"/api/v1/risk-snapshots/{snapshot_id}/download",
            headers=VIEWER_HEADERS,
        )
        self.assertEqual(dl.status_code, 200)

    def test_access_logs_director_only_and_downloads_audited(self):
        pid = self.make_project(name="审计项目")
        snapshot_id = self.generate(pid, date(2026, 6, 1)).json()["snapshot_id"]

        # analyst 不能查审计
        denied = self.client.get(
            f"/api/v1/risk-snapshots/{snapshot_id}/access-logs",
            headers=ANALYST_HEADERS,
        )
        self.assertEqual(denied.status_code, 403)

        # 专员下载一次、负责人查看一次
        self.client.get(
            f"/api/v1/risk-snapshots/{snapshot_id}/download",
            headers=VIEWER_HEADERS,
        )
        self.client.get(
            f"/api/v1/risk-snapshots/{snapshot_id}", headers=DIRECTOR_HEADERS
        )

        logs = self.client.get(
            f"/api/v1/risk-snapshots/{snapshot_id}/access-logs",
            headers=DIRECTOR_HEADERS,
        )
        self.assertEqual(logs.status_code, 200)
        entries = logs.json()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["action"], "view")  # 最新在前
        self.assertEqual(entries[0]["accessed_by_name"], "赵局长")
        self.assertEqual(entries[1]["action"], "download")
        self.assertEqual(entries[1]["accessed_by_name"], "孙专员")

        detail = self.client.get(
            f"/api/v1/risk-snapshots/{snapshot_id}", headers=DIRECTOR_HEADERS
        ).json()
        # 下载次数体现在清单
        listing = self.client.get(
            f"/api/v1/risk-snapshots/?project_id={pid}", headers=DIRECTOR_HEADERS
        ).json()
        self.assertEqual(listing[0]["download_count"], 1)

    def test_snapshot_payload_never_contains_contact_sensitive_fields(self):
        pid = self.make_project(
            name="脱敏项目",
            project_leader="内部负责人李四",
            leader_phone="13811112222",
        )
        snap = self.generate(pid, date(2026, 6, 1)).json()["snapshot"]
        raw = json.dumps(snap, ensure_ascii=False)

        # 敏感值不出现
        self.assertNotIn("内部负责人李四", raw)
        self.assertNotIn("13811112222", raw)
        self.assertNotIn("主体敏感联系人", raw)
        self.assertNotIn("13900009999", raw)
        self.assertNotIn("secret@example.com", raw)
        self.assertNotIn("园区敏感联系人", raw)
        self.assertNotIn("13700001111", raw)

        def assert_no_sensitive_keys(obj, path=""):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    self.assertNotIn(
                        key,
                        {
                            "contact_person",
                            "contact_phone",
                            "contact_email",
                            "leader_phone",
                            "responsible_person",
                            "address",
                        },
                        msg=f"敏感字段出现在 {path}",
                    )
                    assert_no_sensitive_keys(value, f"{path}.{key}")
            elif isinstance(obj, list):
                for i, value in enumerate(obj):
                    assert_no_sensitive_keys(value, f"{path}[{i}]")

        assert_no_sensitive_keys(snap)


if __name__ == "__main__":
    unittest.main()
