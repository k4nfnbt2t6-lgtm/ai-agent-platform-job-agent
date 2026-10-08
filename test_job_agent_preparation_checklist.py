"""公开合成回归及可选的本机真实响应重放；准备状态一致，禁止网络。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from accept_job_v6_api import artificial_materials, prepared_materials
from job_agent.analysis import analyze_jd, match_resume
from job_agent.llm import LLMError
from job_agent.preparation import interview_prep
from job_agent.preparation_files import markdown, save_preparation
from job_agent.preparation_llm import checked_interview
from job_agent.preparation_schemas import InterviewOutput


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "tests/fixtures/job_agent/interview.synthetic.json"
LOCAL_RESPONSE = ROOT / "tests/fixtures/job_agent/interview-final-api.json"


def job(jd):
    return {"job_id": 1, "company": "人工准备科技", "title": "AI开发实习", "city": "广州",
            "jd": jd, "detail_url": None, "source_url": None}


class PreparationChecklistTests(unittest.TestCase):
    def setUp(self):
        for method in ("getaddrinfo", "create_connection"):
            deny = patch.object(socket, method, side_effect=AssertionError("离线回归禁止网络"))
            deny.start()
            self.addCleanup(deny.stop)
        self.jd, self.resume = artificial_materials()
        self.prepared = prepared_materials(self.jd, self.resume)["interview-prep"]
        self.response = json.loads(FIXTURE.read_text())["response"]

    def replay(self):
        return checked_interview(InterviewOutput.model_validate(self.response), self.prepared)

    def test_fixture_is_exact_saved_final_api_response(self):
        if not LOCAL_RESPONSE.is_file():
            self.skipTest("真实 API 响应仅保留本地，不随 Git 分发。")
        self.assertEqual(hashlib.sha256(LOCAL_RESPONSE.read_bytes()).hexdigest(),
                         "c5696833a42b526404fb79c821186b8e5ede494053e5f28c72932d8be63d8ee1")

    def assert_consistent(self, data):
        questions = {q["skill"]: q for q in data["questions"] if q["type"] == "technical"}
        for item in data["checklist"]:
            for topic in item["preparation_topics"]:
                if topic["skill"] in questions:
                    self.assertEqual(topic["status"], questions[topic["skill"]]["status"])
            if not item.get("group_id") and item["skill"] in questions:
                self.assertEqual(item["status"], questions[item["skill"]]["status"])

    def test_final_real_response_replays_without_altering_model_content(self):
        if not LOCAL_RESPONSE.is_file():
            self.skipTest("真实 API 响应仅保留本地；公开副本仍执行全部合成一致性回归。")
        self.response = json.loads(LOCAL_RESPONSE.read_text())
        before = deepcopy(self.response)
        data = self.replay()
        self.assertEqual(self.response, before)
        self.assertEqual(len(data["questions"]), 10)
        self.assert_consistent(data)
        for output, saved in zip(data["questions"], self.response["questions"]):
            for field in ("question", "reference_answer", "knowledge_points", "follow_ups", "learning_advice"):
                self.assertEqual(output[field], saved[field])
        rows = {r["skill"]: r for r in data["checklist"]}
        self.assertEqual(rows["Python"]["status"], "learning")
        self.assertEqual(rows["SQL"]["status"], "needs_confirmation")
        self.assertEqual(rows["Tool Calling"]["status"], "matched")
        self.assertEqual(rows["LLM API"]["status"], "not_evidenced")
        self.assertTrue(all(q["resume_evidence"] for q in data["questions"] if q["type"] == "project"))

    def test_four_readiness_states_and_explicit_gap_reach_checklist(self):
        for resume, skill, status in (
            ("## 技术技能\nPython基础", "Python", "learning"),
            ("## 技术技能\n熟练 Python", "Python", "matched"),
            ("## 项目经历\n### Job Agent\n- 使用SQLite保存岗位并实现去重。", "SQL", "needs_confirmation"),
            ("## 技术技能\n正在学习 MCP", "MCP", "learning"),
            ("## 技术技能\n未掌握 MCP", "MCP", "explicit_gap"),
            ("目标：AI应用开发实习", "LLM API", "not_evidenced"),
        ):
            with self.subTest(skill=skill, status=status):
                data = interview_prep(job(f"任职要求：熟悉{skill}。"), resume)
                item = data["checklist"][0]
                self.assertEqual(item["status"], status)
                self.assert_consistent(data)
                if status in ("learning", "needs_confirmation"):
                    self.assertNotIn("可重点复盘", item["action"])

    def test_llm_api_practice_does_not_follow_first_keyword_in_same_jd_clause(self):
        data = self.replay()
        rows = {r["skill"]: r for r in data["checklist"]}
        api = rows["LLM API"]
        self.assertEqual(api["jd_evidence"], rows["Tool Calling"]["jd_evidence"])
        self.assertNotEqual(api["task"], rows["Tool Calling"]["task"])
        for expected in ("API 请求与响应", "HTTP", "401", "429", "超时", "重试边界", "Token", "费用"):
            self.assertIn(expected, api["task"])
        for expected in ("模拟", "另行确认", "不自动重试"):
            self.assertIn(expected, api["task"])
        self.assertIn("401 不重试", api["acceptance"])
        self.assertIn("费用可能未知", api["acceptance"])

    def test_sql_task_is_proposed_learning_not_unverified_existing_implementation(self):
        data = self.replay()
        sql = next(r for r in data["checklist"] if r["skill"] == "SQL")
        self.assertNotIn("现有 SQLite 岗位记录的参数化查询", sql["task"])
        for expected in ("待确认", "建议", "最小样例", "不把建议写成现有功能"):
            self.assertIn(expected, sql["task"])
        self.assertIn("SQLite", " ".join(sql["resume_evidence"]))
        self.assertIn("不能仅凭 SQLite 经历宣称 SQL 熟练", sql["acceptance"])

    def test_optional_group_reselects_with_preparation_evidence_not_job_score(self):
        resume = "## 技术技能\n正在学习 MCP\n## 项目经历\n### Job Agent\n- 使用SQLite保存岗位并实现去重。"
        data = interview_prep(job("加分项：有SQL或MCP实践。"), resume)
        self.assertEqual(len(data["checklist"]), 1)
        item = data["checklist"][0]
        self.assertEqual(data["matching"]["technical_matches"][0]["status"], "matched")
        self.assertEqual(item["status"], "learning")
        self.assertEqual([t["skill"] for t in item["preparation_topics"]], ["MCP"])
        self.assertIn("不要求同时掌握全部备选", item["task"])
        self.assert_consistent(data)

    def test_combinations_do_not_multiply_requirements(self):
        data = interview_prep(job("任职要求：至少掌握一门语言，如Python/Java/C++。"), "Python基础")
        self.assertEqual(len(data["checklist"]), 1)
        self.assertEqual(data["checklist"][0]["status"], "learning")
        self.assertEqual(data["checklist"][0]["preparation_topics"][0]["skill"], "Python")
        self.assert_consistent(data)

    def test_existing_job_matching_and_scoring_are_unchanged(self):
        expected = match_resume(analyze_jd(self.jd), self.resume)
        self.assertEqual(self.replay()["matching"], expected)
        rows = {r["skill"]: r for r in expected["technical_matches"]}
        self.assertEqual(rows["SQL"]["status"], "matched")  # SQLite 项目经验仍满足该项 JD。

    def test_markdown_and_saved_json_keep_readiness_evidence(self):
        data = self.replay()
        with tempfile.TemporaryDirectory() as directory:
            saved = save_preparation(Path(directory).resolve(), data)
            actual = json.loads(Path(saved["files"]["report.json"]).read_text())
            self.assert_consistent(actual)
            text = Path(saved["files"]["report.md"]).read_text()
            self.assertEqual(text, markdown(actual))
            for expected in ("Python：学习中", "SQL：证据待确认", "状态：learning", "原文依据：", "SQLite"):
                self.assertIn(expected, text)

    def test_unrelated_project_reference_is_still_rejected(self):
        bad = deepcopy(self.response)
        bad["questions"][7]["resume_ids"] = ["F11"]
        with self.assertRaisesRegex(LLMError, "其他主题或其他项目"):
            checked_interview(InterviewOutput.model_validate(bad), self.prepared)


if __name__ == "__main__":
    unittest.main()
