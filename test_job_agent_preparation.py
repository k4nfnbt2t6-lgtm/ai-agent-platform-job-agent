"""v6 离线验收：真实 SDK/SQLite 流程，人工资料，禁止网络和模型调用。"""

import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_platform.demo import answer
from agent_platform.terminal_ui import PROJECT
from job_agent.analysis import analyze_jd, normalized
from job_agent.cli import interactive
from job_agent.preparation import interview_prep, resume_tailor
from job_agent.preparation_files import save_preparation
from job_agent.resume_loader import load_resume
from job_agent.runtime import JobRuntime, JobModel


JD = "公司：人工准备科技\n岗位：AI Agent开发实习\n工作地点：广州\n岗位职责：实现工具调用和岗位去重。\n任职要求：2027届本科，专业不限；熟悉Python、Tool Calling、MCP。\n加分项：了解RAG。"
RESUME = "2027届本科在读，能源与动力工程\n项目：AI Agent Platform，使用Python完成工具调用。\n项目：Job Agent，使用SQLite实现岗位去重。\n项目：Jieqi Agent，实现本地对局测试。\n正在学习或尚未熟练：MCP\nPython基础\nAPI基础\n目标：AI应用开发实习\n个人说明：人工验收材料。"


def job(jd=JD):
    return {"job_id": 1, "company": "人工准备科技", "title": "开发实习", "city": "广州",
            "jd": jd, "source_url": None, "detail_url": None}


class PreparationEvidenceTests(unittest.TestCase):
    def test_tailor_has_complete_sections_and_traceable_facts(self):
        report = resume_tailor(job(), RESUME)
        self.assertIn("## 教育背景", report["tailored_resume"])
        self.assertIn("## 技术技能", report["tailored_resume"])
        self.assertIn("## 项目经历", report["tailored_resume"])
        self.assertNotIn("原始简历正文", report["tailored_resume"])
        self.assertTrue(report["changes"])
        for change in report["changes"]:
            for line in normalized(change["before"]).splitlines():
                self.assertIn(line, normalized(RESUME))
            self.assertIn(change["after"], report["tailored_resume"])
            self.assertTrue(change["reason"])
            self.assertTrue(change["jd_evidence"])

    def test_learning_context_and_python_basic_never_upgrade(self):
        report = resume_tailor(job(), RESUME)
        matching = {x["skill"]: x for x in report["matching"]["technical_matches"]}
        self.assertEqual(matching["MCP"]["status"], "learning")
        self.assertEqual(matching["Python"]["status"], "learning")
        self.assertIn("Python基础", report["tailored_resume"])
        self.assertIn("正在学习或尚未熟练：MCP", report["tailored_resume"])

    def test_career_target_is_not_a_capability_or_project(self):
        resume = "目标：AI应用开发实习\n准备做 Job Agent 项目"
        report = resume_tailor(job("任职要求：熟悉AI应用开发。"), resume)
        self.assertNotIn("掌握AI应用开发", report["tailored_resume"])
        questions = interview_prep(job(), resume)["questions"]
        self.assertFalse(any(q["type"] == "project" for q in questions))

    def test_missing_skill_and_qualification_not_added_as_personal_fact(self):
        report = resume_tailor(job("任职要求：硕士学历；熟练Docker、RAG。"), "本科在读，能源与动力工程\n项目：Job Agent")
        self.assertNotIn("Docker", report["tailored_resume"])
        self.assertNotIn("硕士", report["tailored_resume"])
        self.assertTrue(report["gaps"]["qualification_checks"])

    def test_basic_api_not_made_into_llm_api_practice(self):
        report = resume_tailor(job("任职要求：有LLM API实战。"), "API基础")
        self.assertEqual(report["matching"]["technical_matches"][0]["status"], "needs_confirmation")
        self.assertNotIn("LLM API", report["tailored_resume"])

    def test_project_name_does_not_imply_mcp_or_rag(self):
        report = resume_tailor(job("任职要求：熟悉MCP、RAG。"), "项目：AI Agent Platform / Job Agent")
        self.assertTrue(all(x["status"] == "not_evidenced" for x in report["matching"]["technical_matches"]))
        self.assertNotIn("MCP", report["tailored_resume"])
        self.assertNotIn("RAG", report["tailored_resume"])

    def test_technical_questions_link_jd_and_distinguish_readiness(self):
        report = interview_prep(job(), RESUME)
        technical = {q["skill"]: q for q in report["questions"] if q["type"] == "technical"}
        self.assertEqual(technical["MCP"]["status"], "learning")
        self.assertEqual(technical["RAG"]["status"], "not_evidenced")
        self.assertEqual(technical["Tool Calling"]["status"], "matched")
        for question in technical.values():
            self.assertTrue(question["jd_evidence"])
            self.assertTrue(question["knowledge_points"])
            self.assertTrue(question["reference_answer"])
            self.assertIn("不是你的经历声明", question["answer_boundary"])

    def test_three_real_named_projects_get_evidence_based_questions(self):
        questions = [q for q in interview_prep(job(), RESUME)["questions"] if q["type"] == "project"]
        self.assertEqual({q["project"] for q in questions}, {"AI Agent Platform", "Job Agent", "Jieqi Agent"})
        for q in questions:
            self.assertTrue(q["resume_evidence"])
            self.assertTrue(q["follow_ups"])
            self.assertLess(len(q["reference_answer"]), 220)
            self.assertIn("经历声明", q["answer_boundary"])

    def test_absent_projects_not_invented(self):
        questions = interview_prep(job(), "Python基础")["questions"]
        self.assertFalse(any(q["type"] == "project" for q in questions))

    def test_different_jds_have_different_questions_and_checklists(self):
        first = interview_prep(job("职责：开发RAG检索。\n任职要求：熟悉RAG。"), RESUME)
        second = interview_prep(job("职责：维护岗位数据库。\n任职要求：熟悉SQL。"), RESUME)
        self.assertNotEqual(first["questions"], second["questions"])
        self.assertNotEqual(first["checklist"], second["checklist"])
        self.assertNotEqual(resume_tailor(job(JD), RESUME)["changes"], resume_tailor(job("任职要求：熟悉SQL。"), RESUME)["changes"])

    def test_any_group_is_one_preparation_unit_not_all_required(self):
        report = interview_prep(job("任职要求：至少掌握一门语言，如Python/Java/C++。"), "Python基础")
        technical = [q for q in report["questions"] if q.get("requirement_group")]
        self.assertEqual(len(technical), 1)
        self.assertIn("非全部必须", technical[0]["readiness"])
        self.assertEqual(len(report["checklist"]), 1)

    def test_same_skill_different_duties_have_specific_questions(self):
        first = interview_prep(job("岗位职责：维护岗位数据库。\n任职要求：熟悉Python。"), "Python基础")
        second = interview_prep(job("岗位职责：实现模型工具调用。\n任职要求：熟悉Python。"), "Python基础")
        self.assertNotEqual(first["questions"], second["questions"])
        self.assertNotEqual(first["checklist"], second["checklist"])

    def test_skills_in_one_jd_line_get_their_own_question_and_answer(self):
        report = interview_prep(job("任职要求：熟悉Python、Git和SQL。"), "Python基础")
        questions = {q["skill"]: q for q in report["questions"]}
        self.assertIn("回退", questions["Git"]["question"])
        self.assertIn("岗位记录", questions["SQL"]["question"])
        self.assertIn("异常", questions["Python"]["question"])
        self.assertTrue(questions["SQL"]["reference_answer"].startswith("SQL："))
        self.assertNotIn("Python：", questions["SQL"]["reference_answer"])

    def test_negative_or_planned_project_not_presented_as_real_project(self):
        for text in ("项目：计划开发Jieqi Agent", "尚未完成Job Agent项目", "目标：开发AI Agent Platform项目"):
            with self.subTest(text=text):
                self.assertFalse(any(q["type"] == "project" for q in interview_prep(job(), text)["questions"]))

    def test_unknown_jd_has_clear_manual_review_question(self):
        report = interview_prep(job("岗位职责：整理业务资料。"), "本科在读")
        self.assertIn("人工核对", report["questions"][0]["readiness"])
        self.assertEqual(len(report["questions"]), 10)

    def test_unknown_technology_answer_explicitly_needs_verification(self):
        report = interview_prep(job("任职要求：熟悉LangGraph。"), "正在学习LangGraph")
        question = next(q for q in report["questions"] if q.get("skill") == "LangGraph")
        self.assertIn("暂无", question["reference_answer"])
        self.assertIn("核对", question["reference_answer"])

    def test_exactly_ten_distinct_questions_with_or_without_projects(self):
        for resume in (RESUME, "Python基础", "本科在读"):
            with self.subTest(resume=resume):
                questions = interview_prep(job(), resume)["questions"]
                self.assertEqual(len(questions), 10)
                self.assertEqual(len({q["question"] for q in questions}), 10)
                self.assertEqual([q["id"] for q in questions], list(range(1, 11)))

    def test_ten_questions_cover_core_topics_and_three_project_followups(self):
        questions = interview_prep(job(), RESUME)["questions"]
        self.assertTrue({"Python", "AI 应用开发", "Tool Calling", "LLM API", "错误处理"}
                        <= {q.get("skill") for q in questions})
        projects = [q for q in questions if q["type"] == "project"]
        self.assertEqual(len(projects), 3)
        self.assertTrue(all(len(q["follow_ups"]) == 2 for q in projects))

    def test_interview_document_does_not_repeat_raw_duties_or_resume(self):
        from job_agent.preparation_files import markdown
        report = interview_prep(job(), RESUME)
        document = markdown(report)
        for duty in report["jd_analysis"]["responsibilities"]:
            self.assertNotIn(duty, document)
        self.assertNotIn("简历依据：", document)
        self.assertEqual(document.count("## 问题 "), 10)
        self.assertIn("学习／复盘建议", document)

    def test_learning_notes_and_answers_are_not_claimed_personal_achievements(self):
        questions = interview_prep(job(), RESUME)["questions"]
        for q in questions:
            self.assertTrue(q["learning_advice"])
            self.assertNotIn("我已经", q["reference_answer"])
        mcp = next(q for q in questions if q.get("skill") == "MCP")
        self.assertIn("学习中", mcp["readiness"])

    def test_full_resume_has_no_report_text_and_preserves_metrics_and_contribution(self):
        resume = "人工同学\n教育背景\n人工大学 | 能源与动力工程 | 本科2027届\n项目经历\n个人项目，AI工具辅助开发。\nJieqi Agent\n• 定向测试42/42，全量393/393；仅修改客户端。\n技能与学习方向\nPython基础\n学习中：MCP，尚未熟练。"
        report = resume_tailor(job(), resume)
        text = report["tailored_resume"]
        for fact in ("人工大学", "能源与动力工程", "42/42", "393/393", "仅修改客户端", "AI工具辅助开发", "Python基础", "尚未熟练"):
            self.assertIn(fact, text)
        for prompt in ("修改前", "修改后", "原始简历正文", "本地分析", "匹配度", "建议优先展示"):
            self.assertNotIn(prompt, text)
        self.assertNotIn("## 工作", text)

    def test_project_order_changes_with_jd_and_does_not_transfer_project_skills(self):
        resume = "教育背景\n本科在读\n项目经历\nJob Agent\n• 使用SQL保存岗位。\nAI Agent Platform\n• 实现Tool Calling。\nJieqi Agent\n• 修复localStorage同页重连。"
        ai = resume_tailor(job("岗位职责：实现Tool Calling。"), resume)
        repair = resume_tailor(job("岗位职责：修复localStorage同页重连。"), resume)
        self.assertEqual(ai["project_order"][0], "AI Agent Platform")
        self.assertEqual(repair["project_order"][0], "Jieqi Agent")
        self.assertNotIn("Tool Calling", repair["tailored_resume"].split("### Jieqi Agent")[1].split("###")[0])

    def test_skill_order_changes_with_jd_without_adding_missing_skills(self):
        resume = "技术技能\nPython基础\nGit基础\n学习中：MCP，尚未熟练。"
        first = resume_tailor(job("任职要求：熟悉Python。"), resume)
        second = resume_tailor(job("任职要求：熟悉Git、Docker。"), resume)
        self.assertEqual(first["skill_order"][0], "Python基础")
        self.assertEqual(second["skill_order"][0], "Git基础")
        self.assertNotIn("Docker", second["tailored_resume"])

    def test_learning_heading_stays_with_child_skills_when_reordered(self):
        resume = "技术技能\n正在学习或尚未熟练：\nMCP\nRAG\n已掌握：Git"
        report = resume_tailor(job("任职要求：掌握MCP、RAG。"), resume)
        text = report["tailored_resume"]
        self.assertIn("正在学习或尚未熟练：；MCP；RAG", text)
        self.assertFalse(any(item["status"] == "matched" for item in report["matching"]["technical_matches"]))

    def test_pdf_wrapped_project_bullet_is_rejoined_without_new_claim(self):
        resume = "项目经历\nJieqi Agent\n• 复现缺陷并修改代\n码，使用AI工具辅助。"
        report = resume_tailor(job(), resume)
        self.assertIn("复现缺陷并修改代码，使用AI工具辅助。", report["tailored_resume"])
        change = next(x for x in report["changes"] if "修改代" in x["before"])
        self.assertIn("\n码", change["before"])
        self.assertIn("修改代码", change["after"])

    def test_completed_project_without_section_header_is_recognized(self):
        resume = "本科在读\n完成 AI Agent Platform 本地工程项目。\n使用 Python 完成测试。\n正在学习 MCP。"
        report = resume_tailor(job(), resume)
        self.assertIn("## 项目经历", report["tailored_resume"])
        questions = interview_prep(job(), resume)["questions"]
        self.assertEqual([q["project"] for q in questions if q["type"] == "project"], ["AI Agent Platform"])

    def test_standalone_learning_or_target_project_is_not_completed(self):
        for line in ("目标：开发 Job Agent", "计划开发 AI Agent Platform", "正在学习 Jieqi Agent"):
            with self.subTest(line=line):
                self.assertFalse(any(q["type"] == "project" for q in interview_prep(job(), line)["questions"]))


class PreparationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        shutil.copytree(PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        self.runtime = JobRuntime(self.root, demo=True)
        self.addAsyncCleanup(self.runtime.close)
        record = self.runtime.workspace.memory.remember(JD, analyze_jd(JD), "人工资料")
        self.identity = self.runtime.workspace.memory.library.linked("jd", record["jd_id"])
        self.network = patch.object(socket, "getaddrinfo", side_effect=AssertionError("离线测试禁止网络"))
        self.network.start()
        self.addCleanup(self.network.stop)

    async def test_sdk_loads_only_requested_skill_and_saves_structured_files(self):
        for skill in ("resume-tailor", "interview-prep"):
            data = await self.runtime.prepare_job(self.identity, RESUME, skill)
            self.assertEqual([x["name"] for x in self.runtime.recorder.tool_calls], ["load_skill", skill.replace("-", "_")])
            self.assertEqual(self.runtime.workspace.loaded, {skill})
            self.assertEqual({t.name for t in self.runtime.build_agent(JobModel(), "x").tools}, {"load_skill", skill.replace("-", "_")})
            saved = json.loads(Path(data["files"]["report.json"]).read_text())
            self.assertEqual(saved["job_id"], self.identity)
            self.assertEqual(saved["model_requests"], 0)
            self.assertTrue(Path(data["files"]["report.md"]).is_file())

    async def test_original_resume_never_overwritten_and_repeated_exports_are_new(self):
        original = self.runtime.workspace.root / "resume.md"
        original.write_text(RESUME)
        body = original.read_bytes()
        first = await self.runtime.prepare_job(self.identity, load_resume(original)[0], "resume-tailor")
        second = await self.runtime.prepare_job(self.identity, load_resume(original)[0], "resume-tailor")
        self.assertNotEqual(first["files"], second["files"])
        self.assertEqual(original.read_bytes(), body)
        self.assertEqual(Path(first["files"]["tailored-resume.md"]).read_text(), first["tailored_resume"])
        for path in first["files"].values():
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(path).parent.stat().st_mode & 0o777, 0o700)

    async def test_pdf_source_bytes_unchanged(self):
        from test_job_agent import resume_pdf
        source = self.root / "resume.pdf"
        source.write_bytes(resume_pdf("项目：Job Agent，使用Python。\nPython基础"))
        before = source.read_bytes()
        data = await self.runtime.prepare_job(self.identity, load_resume(source)[0], "resume-tailor")
        self.assertIsNotNone(data)
        self.assertEqual(source.read_bytes(), before)

    async def test_formal_resume_and_explanations_are_separate_private_files(self):
        data = await self.runtime.prepare_job(self.identity, RESUME, "resume-tailor")
        formal = Path(data["files"]["tailored-resume.md"]).read_text()
        explanation = Path(data["files"]["report.md"]).read_text()
        self.assertNotIn("修改前", formal)
        self.assertIn("修改前", explanation)
        self.assertIn("项目顺序", explanation)
        self.assertNotEqual(formal, explanation)

    async def test_job_status_notes_scores_and_jd_are_unchanged(self):
        library = self.runtime.workspace.memory.library
        library.set_status(self.identity, "interview")
        library.add_note(self.identity, "人工备注")
        before = library.list()
        for skill in ("resume-tailor", "interview-prep"):
            await self.runtime.prepare_job(self.identity, RESUME, skill)
        self.assertEqual(library.list(), before)

    async def test_current_resume_recomputed_after_change(self):
        first = await self.runtime.prepare_job(self.identity, "熟练Python", "resume-tailor")
        second = await self.runtime.prepare_job(self.identity, "正在学习Python", "resume-tailor")
        def status(data):
            return next(x["status"] for x in data["matching"]["technical_matches"] if x["skill"] == "Python")
        self.assertEqual(status(first), "matched")
        self.assertEqual(status(second), "learning")

    async def test_missing_resume_and_invalid_ids_give_errors_without_files(self):
        for identity, resume in ((self.identity, None), ("abc", RESUME), (9999, RESUME), (0, RESUME)):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                await self.runtime.prepare_job(identity, resume, "resume-tailor")
        self.assertFalse((self.runtime.workspace.root / "preparations").exists())

    async def test_empty_jd_rejected(self):
        with self.runtime.workspace.memory.connection as db:
            db.execute("UPDATE job_library SET jd='' WHERE job_id=?", (self.identity,))
        with self.assertRaisesRegex(ValueError, "缺少 JD"):
            await self.runtime.prepare_job(self.identity, RESUME, "resume-tailor")

    async def test_credential_input_blocked_before_export(self):
        with self.assertRaisesRegex(ValueError, "凭据"):
            await self.runtime.prepare_job(self.identity, RESUME + "\npassword=placeholder", "resume-tailor")
        self.assertFalse((self.runtime.workspace.root / "preparations").exists())

    async def test_linked_output_directory_cannot_redirect_export(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.runtime.workspace.root / "preparations").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            await self.runtime.prepare_job(self.identity, RESUME, "resume-tailor")
        self.assertEqual(list(outside.iterdir()), [])

    async def test_early_final_does_not_fake_report_creation(self):
        class Pretend(JobModel):
            async def get_response(self, *args, **kwargs):
                return answer("简历已经优化完成")
        with patch.object(self.runtime, "demo_model", return_value=Pretend()):
            self.assertIsNone(await self.runtime.prepare_job(self.identity, RESUME, "resume-tailor"))
        self.assertFalse((self.runtime.workspace.root / "preparations").exists())

    async def test_llm_session_preparation_requires_approval_and_refusal_never_calls_api(self):
        approval = AsyncMock(return_value=False)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "offline-placeholder", "OPENAI_BASE_URL": "https://example.com/v1"}):
            runtime = JobRuntime(self.root, mode="llm", approve_llm=approval)
            try:
                with patch.object(runtime.llm, "ask", new=AsyncMock(side_effect=AssertionError("不许调用模型"))) as ask:
                    for skill in ("resume-tailor", "interview-prep"):
                        self.assertIsNone(await runtime.prepare_job(self.identity, RESUME, skill))
                    ask.assert_not_called()
                self.assertEqual(approval.await_count, 2)
            finally:
                await runtime.close()

    async def test_cli_commands_and_errors_continue_without_changing_status(self):
        lines = ["/tailor", "/tailor abc", "/interview-prep 9999", "/tailor 1 extra", "/tailor 1", "/interview-prep 1", "/quit"]
        buffer = io.StringIO()
        with patch("job_agent.cli.read_line", new=AsyncMock(side_effect=lines)), contextlib.redirect_stdout(buffer):
            await interactive(self.runtime, resume=RESUME)
        output = buffer.getvalue()
        self.assertIn("用法：/tailor", output)
        self.assertIn("未找到岗位", output)
        self.assertIn("修改前后对比", output)
        self.assertIn("参考答案", output)
        self.assertEqual(len(list((self.runtime.workspace.root / "preparations").glob('*/report.json'))), 2)
        self.assertEqual(self.runtime.workspace.memory.library.get(self.identity)["status"], "discovered")

    async def test_cli_missing_resume_has_clear_hint(self):
        buffer = io.StringIO()
        with patch("job_agent.cli.read_line", new=AsyncMock(side_effect=["/tailor 1", "/quit"])), contextlib.redirect_stdout(buffer):
            await interactive(self.runtime)
        self.assertIn("提供当前真实简历", buffer.getvalue())

    async def test_original_analysis_still_works_after_preparation(self):
        await self.runtime.prepare_job(self.identity, RESUME, "resume-tailor")
        data = await self.runtime.analyze(JD, RESUME)
        self.assertIsNotNone(data["resume_match"])
        self.assertEqual([x["name"] for x in self.runtime.recorder.tool_calls], ["load_skill", "analyze_jd", "match_resume", "skill_gap_analysis"])


if __name__ == "__main__":
    unittest.main()
