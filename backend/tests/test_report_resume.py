"""报表数据服务续做回归测试：只覆盖失败续做、复用与页段版本边界。

运行：backend/.venv/bin/python -m unittest discover -s backend/tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.report_data import report_data_service  # noqa: E402
from app.services.report_resume import decide_artifact, decide_page  # noqa: E402
from app.store import store  # noqa: E402


class ResumeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()

    def task(self, task_id: int = 3) -> dict:
        row = store.find("reportsch", task_id)
        self.assertIsNotNone(row)
        return row  # type: ignore[return-value]

    # ---------- 共用判定 ----------

    def test_shared_decision_prefers_own_confirmed_artifact(self) -> None:
        decision = decide_artifact(
            data_date="2026-10-12",
            metric_set="点击量,曝光量",
            version="v1",
            existing={"id": 1, "状态": "已完成", "是否确认": True, "任务编号": "REPO-0003"},
        )
        self.assertEqual(decision.kind, "retry")
        self.assertEqual(decision.action, "reuse")
        self.assertIsNone(decision.conflict_reason)

    def test_shared_decision_marks_conflict_reason_for_other_task(self) -> None:
        decision = decide_artifact(
            data_date="2026-10-20",
            metric_set="点击数",
            version="",
            other_confirmed={"id": 9, "状态": "已完成", "是否确认": True, "任务编号": "REPO-0004"},
        )
        self.assertEqual(decision.action, "reuse")
        self.assertIn("REPO-0004", decision.conflict_reason or "")

    def test_shared_page_decision_pins_first_version(self) -> None:
        decision = decide_page(
            requested_version="DELI-0003-v1",
            current_version="DELI-0003-v2",
            available_versions=["DELI-0003-v1", "DELI-0003-v2"],
            page=2,
        )
        self.assertEqual(decision.kind, "page")
        self.assertEqual(decision.version, "DELI-0003-v1")
        self.assertEqual(decision.action, "pin")

    def test_shared_page_decision_rejects_unknown_version(self) -> None:
        decision = decide_page(
            requested_version="DELI-0003-v9",
            current_version="DELI-0003-v1",
            available_versions=["DELI-0003-v1"],
            page=2,
        )
        self.assertEqual(decision.action, "reject")

    # ---------- 重试样例 ----------

    def test_retry_only_supplies_failed_date(self) -> None:
        before = report_data_service.status(task=self.task())["统计"]
        self.assertEqual(before["已完成"], 1)
        self.assertEqual(before["待重试"], 1)

        result = report_data_service.run_retry_batch(
            task=self.task(), values={"数据日期": ["2026-10-13"]}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["统计"]["已完成"], 1)
        self.assertEqual(result["统计"]["复用"], 0)
        self.assertEqual(result["统计"]["待重试"], 0)
        self.assertTrue(result["版本已更新"])
        self.assertEqual(result["版本号"], "DELI-0003-v2")

    def test_same_retry_batch_is_idempotent_and_keeps_downloads_unique(self) -> None:
        values = {"数据日期": ["2026-10-12", "2026-10-13"], "批次号": "REPO-0003-B1"}
        first = report_data_service.run_retry_batch(task=self.task(), values=values)
        self.assertFalse(first["幂等"])
        self.assertEqual(first["统计"]["复用"], 1)
        self.assertEqual(first["统计"]["已完成"], 1)

        second = report_data_service.run_retry_batch(task=self.task(), values=values)
        self.assertTrue(second["幂等"])
        self.assertEqual(second["统计"], first["统计"])

        downloads = report_data_service.downloads(task=self.task())
        self.assertEqual(downloads["重复记录数"], 0)
        self.assertEqual(downloads["total"], 2)

    def test_failed_fragment_keeps_successful_fragment(self) -> None:
        store.append("reportsch", {
            "id": 6,
            "任务编号": "REPO-0006",
            "报表名称": "单日续做",
            "包含指标": "点击量,曝光量",
            "数据日期列表": ["2026-10-14"],
            "返回字段": "点击量,曝光量",
            "重试次数上限": "3",
            "status": "已排期",
        })
        task = store.find("reportsch", 6)
        result = report_data_service.run_retry_batch(
            task=task,
            values={"数据日期": ["2026-10-14"], "失败片段": ["曝光量"], "批次号": "B-FAIL"},
        )
        artifact = next(item for item in result["产物"] if item["数据日期"] == "2026-10-14")
        statuses = {fragment["指标"]: fragment["状态"] for fragment in artifact["片段"]}
        self.assertEqual(statuses["点击量"], "已确认")
        self.assertEqual(statuses["曝光量"], "失败")
        self.assertFalse(artifact["是否确认"])
        self.assertFalse(result["版本已更新"])
        self.assertEqual(result["待重试日期"], ["2026-10-14"])

        fixed = report_data_service.run_retry_batch(
            task=task,
            values={"数据日期": ["2026-10-14"], "批次号": "B-FIX"},
        )
        artifact = next(item for item in fixed["产物"] if item["数据日期"] == "2026-10-14")
        self.assertTrue(artifact["是否确认"])
        self.assertTrue(fixed["版本已更新"])

    def test_conflicting_task_reuses_confirmed_artifact(self) -> None:
        store.append("reportsch", {
            "id": 4,
            "任务编号": "REPO-0004",
            "报表名称": "并发任务",
            "包含指标": "点击数",
            "数据日期列表": ["2026-10-20"],
            "返回字段": "点击数",
            "重试次数上限": "3",
            "status": "已排期",
        })
        store.append("reportsch", {
            "id": 5,
            "任务编号": "REPO-0005",
            "报表名称": "并发任务副本",
            "包含指标": "点击数",
            "数据日期列表": ["2026-10-20"],
            "返回字段": "点击数",
            "重试次数上限": "3",
            "status": "已排期",
        })
        first = report_data_service.run_retry_batch(
            task=store.find("reportsch", 4), values={"数据日期": ["2026-10-20"]}
        )
        self.assertTrue(first["版本已更新"])

        second = report_data_service.run_retry_batch(
            task=store.find("reportsch", 5), values={"数据日期": ["2026-10-20"]}
        )
        self.assertEqual(second["统计"]["复用"], 1)
        self.assertEqual(second["统计"]["冲突复用"], 1)
        self.assertEqual(len(second["冲突"]), 1)
        self.assertIn("REPO-0004", second["冲突"][0]["冲突原因"])
        self.assertEqual(second["产物"][0]["状态"], "复用")

    # ---------- 页段边界 ----------

    def test_page_continuation_uses_first_version_after_new_commit(self) -> None:
        first_page, message = report_data_service.page_segments(
            task=self.task(), page=1, size=1
        )
        self.assertIsNotNone(first_page)
        self.assertEqual(message, "")
        version = first_page["版本号"]

        report_data_service.run_retry_batch(
            task=self.task(), values={"数据日期": ["2026-10-13"], "批次号": "B-PAGE"}
        )
        current = report_data_service.status(task=self.task())["当前版本号"]
        self.assertNotEqual(current, version)

        second_page, message = report_data_service.page_segments(
            task=self.task(), page=2, size=1, version=version
        )
        self.assertIsNotNone(second_page)
        self.assertEqual(second_page["版本号"], version)
        self.assertTrue(second_page["版本切换"])
        self.assertIn(version, second_page["续读说明"])

    def test_page_mismatch_is_rejected(self) -> None:
        payload, message = report_data_service.page_segments(
            task=self.task(), page=2, size=1, version="DELI-0003-v9"
        )
        self.assertIsNone(payload)
        self.assertIn("不存在", message)

    def test_page_without_version_after_first_page_is_rejected(self) -> None:
        payload, message = report_data_service.page_segments(task=self.task(), page=2, size=1)
        self.assertIsNone(payload)
        self.assertIn("版本号", message)

    def test_page_fields_match_schedule_fields(self) -> None:
        task = self.task()
        payload, _ = report_data_service.page_segments(task=task, page=1, size=1)
        self.assertEqual(payload["返回字段"], ["点击量", "曝光量"])
        item = payload["items"][0]
        for field in payload["返回字段"]:
            self.assertIn(field, item)


if __name__ == "__main__":
    unittest.main()
