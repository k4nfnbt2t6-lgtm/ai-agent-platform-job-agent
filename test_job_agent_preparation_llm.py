"""人工材料 + 真实 SDK/模拟 HTTP；禁止真实网络和收费。"""

from copy import deepcopy
from argparse import Namespace
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx2
from openai import AsyncOpenAI
from pydantic import ValidationError

from agent_platform.terminal_ui import PROJECT
from job_agent.analysis import analyze_jd
from job_agent.cli import print_preparation, run as run_job
from job_agent.llm import LLMError
from job_agent.preparation_llm import PROMPT, checked_interview, checked_tailor, plan
from job_agent.preparation_schemas import InterviewOutput, TailorOutput
from job_agent.runtime import JobRuntime
from test_job_agent_llm import api_response


JD = (PROJECT / "job_agent/examples/preparation-jd.example.md").read_text()
RESUME = (PROJECT / "job_agent/examples/preparation-resume.example.md").read_text()
JOB = {"job_id": 1, "company": "人工准备科技", "title": "AI Agent应用开发实习生", "city": "广州",
       "jd": JD, "source_url": None, "detail_url": None}


def tailor_output(payload):
    revisions = []
    for fact in payload["resume_facts"]:
        if fact["editable"] and "Python" in fact["text"] and "工具调用" in fact["text"]:
            revisions.append({"fact_id": fact["id"], "source_quote": fact["source"],
                "after": "围绕工具调用，使用 Python 完成输入校验与异常处理；通过本地测试核对结果。",
                "reason": "岗位关注工具输入和失败处理，将原实现行动与验证方式放在句子中心。", "jd_ids": ["J4"]})
    return {"revisions": revisions, "project_order": list(reversed(payload["projects"])),
            "skill_order": list(reversed(payload["skill_ids"]))}


def interview_output(payload):
    return {"questions": [{"topic_id": topic["id"],
        "question": f"{topic['topic']} 在智能助手输入失败时应如何设计验证边界（主题 {index}）？",
        "reference_answer": f"针对 {topic['topic']}，可以先明确输入约束、成功输出和失败状态，再验证异常不会被误记为完成。用正常、无效输入和中途失败样例检查结果；涉及写入时应先校验再提交，保留可解释错误。这样可以将输入错误与执行错误分开定位；测试还应检查失败前后的状态一致，并说明哪些步骤已经完成、哪些尚未执行。",
        "knowledge_points": [topic["topic"], "输入校验", "失败验证"],
        "follow_ups": ["若输入无效，怎样证明没有发生写入？", "失败后再次执行，如何避免重复处理？"] if topic["type"] == "project" else [],
        "learning_advice": "先按原文核对已完成范围；未掌握的部分做最小例子，用测试验证后再准备个人回答。",
        "jd_ids": [next((jd["id"] for jd in payload["jd_segments"] if jd["id"] == "J4"), payload["jd_segments"][0]["id"])], "resume_ids": topic["resume_ids"]}
        for index, topic in enumerate(payload["topics"], 1)]}


class PreparationSemanticTests(unittest.TestCase):
    def prepared(self, skill="resume-tailor", resume=RESUME, jd=JD):
        return plan({**JOB, "jd": jd}, resume, skill)

    def test_strict_schemas_and_ten_questions(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(set(node["properties"]), set(node["required"]))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        for schema in (TailorOutput, InterviewOutput):
            walk(schema.model_json_schema())
        value = interview_output(self.prepared("interview-prep")["payload"])
        value["questions"].pop()
        with self.assertRaises(ValidationError):
            InterviewOutput.model_validate(value)
        value["unexpected"] = True
        with self.assertRaises(ValidationError):
            InterviewOutput.model_validate(value)

    def test_privacy_profile_education_work_and_inline_contacts_are_not_sent(self):
        resume = "姓名：人工候选人\n邮箱：person@example.org\n电话：13800000000\n" + RESUME + "\n- 联系方式：person@example.org\n## 工作经历\n某公司实习\n"
        prepared = self.prepared(resume=resume)
        raw = json.dumps(prepared["payload"], ensure_ascii=False)
        for value in ("人工候选人", "person@example.org", "13800000000", "能源与动力", "某公司实习"):
            self.assertNotIn(value, raw)
        self.assertTrue(any("Python" in fact["text"] for fact in prepared["payload"]["resume_facts"]))
        self.assertNotIn("教育背景", raw)

    def test_jd_contacts_and_paths_are_redacted(self):
        prepared = self.prepared(jd=JD + "\n联系 person@example.org，电话13800000000，文件 /Users/example/private.pdf")
        raw = json.dumps(prepared["payload"], ensure_ascii=False)
        self.assertIn("已脱敏", raw)
        for value in ("person@example.org", "13800000000", "/Users/example"):
            self.assertNotIn(value, raw)

    def test_inline_education_and_work_headings_do_not_leak_as_project_facts(self):
        resume = RESUME + "\n工作经历：某单位的实习经历\n负责内部资料整理\n教育背景：某学院本科在读\n"
        prepared = self.prepared(resume=resume)
        payload = json.dumps(prepared["payload"], ensure_ascii=False)
        for value in ("某单位", "内部资料整理", "某学院"):
            self.assertNotIn(value, payload)
        data = checked_tailor(TailorOutput.model_validate(tailor_output(prepared["payload"])), prepared, JOB, resume)
        self.assertIn("某单位的实习经历", data["tailored_resume"])
        self.assertIn("某学院本科在读", data["tailored_resume"])

    def test_semantic_rewrite_assembled_into_full_resume_and_separate_explanations(self):
        prepared = self.prepared()
        value = tailor_output(prepared["payload"])
        data = checked_tailor(TailorOutput.model_validate(value), prepared, JOB, RESUME)
        self.assertIn(value["revisions"][0]["after"], data["tailored_resume"])
        for heading in ("教育背景", "技术技能", "项目经历"):
            self.assertIn("## " + heading, data["tailored_resume"])
        self.assertEqual(data["project_order"], value["project_order"])
        self.assertIn("能源与动力工程", data["tailored_resume"])
        self.assertNotIn("修改前", data["tailored_resume"])
        self.assertEqual(data["changes"][0]["before"], value["revisions"][0]["source_quote"])

    def test_weak_evidence_and_goals_cannot_be_rewritten_as_mastery(self):
        prepared = self.prepared()
        for fact in prepared["payload"]["resume_facts"]:
            if "基础" not in fact["text"] and "MCP" not in fact["text"]:
                continue
            value = tailor_output(prepared["payload"])
            value["revisions"] = [{"fact_id": fact["id"], "source_quote": fact["source"],
                                   "after": "熟练掌握 MCP 与 LLM API 实战。", "reason": "不可信", "jd_ids": ["J5"]}]
            with self.subTest(fact=fact["id"]), self.assertRaisesRegex(LLMError, "受保护"):
                checked_tailor(TailorOutput.model_validate(value), prepared, JOB, RESUME)
        goal = self.prepared(resume="目标：AI应用开发实习\nPython基础")
        self.assertEqual(goal["payload"]["projects"], [])
        self.assertFalse(any(fact["editable"] for fact in goal["payload"]["resume_facts"]))

    def test_new_tech_metrics_education_contribution_and_cross_project_claims_rejected(self):
        prepared = self.prepared()
        for after in ("使用 Python、RAG 实现工具调用，本地测试。", "通过本地测试，准确率提升50%。",
                      "独立负责 Python 工具调用与本地测试。", "硕士毕业，Python 本地测试。",
                      "使用 LangGraph 实现 Python 本地测试。", "Job Agent 使用 Python 实现工具调用并本地测试。"):
            value = tailor_output(prepared["payload"])
            value["revisions"][0]["after"] = after
            with self.subTest(after=after), self.assertRaises(LLMError):
                checked_tailor(TailorOutput.model_validate(value), prepared, JOB, RESUME)

    def test_ai_assistance_and_local_boundary_cannot_disappear(self):
        prepared = self.prepared()
        fact = next(fact for fact in prepared["payload"]["resume_facts"] if "辅助" in fact["text"] and fact["editable"])
        value = tailor_output(prepared["payload"])
        value["revisions"] = [{"fact_id": fact["id"], "source_quote": fact["source"],
            "after": "整理会话与权限边界，保留运行入口。", "reason": "不可信", "jd_ids": ["J4"]}]
        with self.assertRaisesRegex(LLMError, "限定"):
            checked_tailor(TailorOutput.model_validate(value), prepared, JOB, RESUME)

    def test_bad_quote_id_order_and_injected_sections_rejected(self):
        prepared = self.prepared()
        original = tailor_output(prepared["payload"])
        for mutation in (lambda x: x["revisions"][0].update(source_quote="不存在的原文"),
                         lambda x: x["revisions"][0].update(fact_id="F999"),
                         lambda x: x["revisions"][0].update(jd_ids=["J999"]),
                         lambda x: x["project_order"].append("虚构项目"),
                         lambda x: x["skill_order"].pop(),
                         lambda x: x["revisions"][0].update(after="本地 Python 测试\n## 虚构工作经历")):
            value = deepcopy(original)
            mutation(value)
            with self.assertRaises(LLMError):
                checked_tailor(TailorOutput.model_validate(value), prepared, JOB, RESUME)

    def test_empty_semantic_revision_does_not_pretend_to_be_optimization(self):
        prepared = self.prepared()
        value = tailor_output(prepared["payload"])
        value["revisions"] = []
        with self.assertRaisesRegex(LLMError, "没有提供"):
            checked_tailor(TailorOutput.model_validate(value), prepared, JOB, RESUME)

    def test_inline_project_description_can_be_rewritten_without_changing_identity(self):
        resume = "本科在读\n项目：AI Agent Platform，使用 Python 实现工具调用与输入校验，通过本地测试验证异常处理。"
        prepared = self.prepared(resume=resume)
        value = tailor_output(prepared["payload"])
        self.assertTrue(value["revisions"])
        data = checked_tailor(TailorOutput.model_validate(value), prepared, JOB, resume)
        self.assertIn("### AI Agent Platform", data["tailored_resume"])
        self.assertIn(value["revisions"][0]["after"], data["tailored_resume"])

    def test_learning_mcp_and_api_basics_stay_partial_or_unconfirmed(self):
        prepared = self.prepared("interview-prep", resume="Python基础\nAPI基础\n正在学习或尚未熟练：MCP",
                                 jd="要求：掌握MCP、LLM API、Python")
        output = InterviewOutput.model_validate(interview_output(prepared["payload"]))
        data = checked_interview(output, prepared)
        statuses = {q.get("skill"): q["status"] for q in data["questions"]}
        self.assertEqual(statuses["MCP"], "learning")
        self.assertEqual(statuses["Python"], "learning")
        self.assertIn(statuses["LLM API"], ("learning", "needs_confirmation", "not_evidenced"))
        self.assertEqual(next(q["status"] for q in self.prepared("interview-prep")["local"]["questions"]
                              if q.get("skill") == "Python"), "learning")

    def test_markdown_learning_heading_retains_context_for_following_skills(self):
        resume = "## 技术技能\n## 正在学习或尚未熟练\nMCP\nRAG\n## 项目经历\n### Job Agent\n- 使用 SQLite 保存岗位记录。"
        prepared = self.prepared(resume=resume)
        weak = next(fact for fact in prepared["payload"]["resume_facts"] if "MCP" in fact["text"])
        self.assertIn("正在学习", weak["source"])
        self.assertFalse(weak["editable"])
        value = tailor_output(prepared["payload"])
        value["revisions"] = [{"fact_id": weak["id"], "source_quote": weak["source"],
                               "after": "掌握 MCP、RAG", "reason": "不可信", "jd_ids": ["J5"]}]
        with self.assertRaisesRegex(LLMError, "受保护"):
            checked_tailor(TailorOutput.model_validate(value), prepared, JOB, resume)

    def test_interview_preserves_readiness_and_real_project_evidence(self):
        prepared = self.prepared("interview-prep")
        data = checked_interview(InterviewOutput.model_validate(interview_output(prepared["payload"])), prepared)
        self.assertEqual(len(data["questions"]), 10)
        self.assertEqual([q["status"] for q in data["questions"]], [q["status"] for q in prepared["local"]["questions"]])
        projects = [q for q in data["questions"] if q["type"] == "project"]
        self.assertEqual({q["project"] for q in projects}, {"AI Agent Platform", "Job Agent", "Jieqi Agent"})
        self.assertTrue(all(q["resume_evidence"] and len(q["follow_ups"]) >= 2 for q in projects))
        python = next(q for q in data["questions"] if q.get("skill") == "Python")
        self.assertEqual(python["status"], "learning")

    def test_interview_invalid_refs_duplicate_topics_answers_and_fake_personal_claims_rejected(self):
        prepared = self.prepared("interview-prep")
        original = interview_output(prepared["payload"])
        project_index = next(i for i, topic in enumerate(prepared["payload"]["topics"]) if topic["type"] == "project")
        other = next(topic for topic in prepared["payload"]["topics"] if topic["type"] == "project" and topic["id"] != original["questions"][project_index]["topic_id"])
        for mutation in (lambda x: x["questions"][0].update(jd_ids=["J999"]),
                         lambda x: x["questions"][0].update(topic_id="T999"),
                         lambda x: x["questions"][project_index].update(resume_ids=other["resume_ids"]),
                         lambda x: x["questions"][project_index].update(follow_ups=[]),
                         lambda x: x["questions"][1].update(question=x["questions"][0]["question"]),
                         lambda x: x["questions"][1].update(reference_answer=x["questions"][0]["reference_answer"]),
                         lambda x: x["questions"][0].update(reference_answer="我已经实现了所有生产特性，并且熟练掌握这些技能，可以承担完整研发工作。"),
                         lambda x: x["questions"][0].update(question="无关的旅游天气应该如何查询呢？")):
            value = deepcopy(original)
            mutation(value)
            with self.assertRaises(LLMError):
                checked_interview(InterviewOutput.model_validate(value), prepared)

    def test_distinct_jds_change_approved_payload(self):
        first = self.prepared("interview-prep")
        second = self.prepared("interview-prep", jd="岗位职责：开发RAG检索\n要求：熟悉RAG和MCP")
        self.assertNotEqual(first["payload"]["jd_segments"], second["payload"]["jd_segments"])
        self.assertNotEqual(first["payload"]["topics"], second["payload"]["topics"])


class PreparationLLMRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        shutil.copytree(PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        self.runtimes, self.requests, self.events = [], [], []
        self.failure = None
        patches = [patch.dict(os.environ, {"OPENAI_API_KEY": "offline-prep-placeholder", "OPENAI_BASE_URL": "https://example.org/v1", "OPENAI_MODEL": "gpt-6-sol"}),
                   patch.object(socket, "getaddrinfo", side_effect=AssertionError("禁止真实网络")),
                   patch("job_agent.llm.AsyncOpenAI", side_effect=self.factory)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.close()

    def factory(self, **kwargs):
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertEqual(kwargs["timeout"], 120)
        return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler), trust_env=False), **kwargs)

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertFalse(body["store"])
        self.assertEqual(body["max_output_tokens"], 6144 if json.loads(body["input"][1]["content"])["skill"] == "resume-tailor" else 8192)
        self.assertEqual(body["reasoning"]["effort"], "medium")
        self.assertEqual(body["input"][0]["content"], PROMPT)
        task = json.loads(body["input"][1]["content"])
        if self.failure:
            return self.failure(request, task)
        value = tailor_output(task["materials"]) if task["skill"] == "resume-tailor" else interview_output(task["materials"])
        return httpx2.Response(200, json=api_response(value))

    def runtime(self, **kwargs):
        runtime = JobRuntime(self.root, self.events.append, mode="llm", approve_llm=AsyncMock(return_value=True), **kwargs)
        self.runtimes.append(runtime)
        record = runtime.workspace.memory.remember(JD, analyze_jd(JD), "人工验收")
        identity = runtime.workspace.memory.library.linked("jd", record["jd_id"])
        return runtime, identity

    async def test_both_skills_use_real_sdk_mock_http_exactly_once_each(self):
        runtime, identity = self.runtime()
        for skill in ("resume-tailor", "interview-prep"):
            data = await runtime.prepare_job(identity, RESUME, skill)
            self.assertEqual(data["mode"], "llm")
            self.assertTrue(data["evidence_checked"])
            self.assertEqual(data["model_requests"], 1)
            self.assertEqual(data["input_tokens"], 1000)
            self.assertEqual(data["output_tokens"], 200)
            self.assertAlmostEqual(data["estimated_rmb"], .006)
            self.assertEqual([call["name"] for call in runtime.recorder.tool_calls], ["load_skill", skill.replace("-", "_")])
            saved = json.loads(Path(data["files"]["report.json"]).read_text())
            self.assertEqual(saved["mode"], "llm")
            self.assertIn("实际模式：llm", Path(data["files"]["report.md"]).read_text())
            # SDK 工具输出不携带简历或生成正文。
            self.assertNotIn("tailored_resume", json.dumps(runtime.recorder.tool_calls))
        self.assertEqual(len(self.requests), 2)

    async def test_declined_or_missing_consent_never_sends_or_saves(self):
        runtime, identity = self.runtime()
        for approval in (AsyncMock(return_value=False), None):
            runtime.approve_llm = approval
            self.assertIsNone(await runtime.prepare_job(identity, RESUME, "resume-tailor"))
        self.assertEqual(self.requests, [])
        self.assertFalse((runtime.workspace.root / "preparations").exists())

    async def test_approved_scope_and_estimate_match_actual_wire_payload(self):
        runtime, identity = self.runtime()
        approvals = []
        async def approve(workspace, estimate, config):
            approvals.append((deepcopy(workspace.preparation_plan["payload"]), estimate.copy()))
            self.assertEqual(config.model, "gpt-6-sol")
            self.assertEqual(estimate["calls"], 2)
            return True
        runtime.approve_llm = approve
        await runtime.prepare_job(identity, RESUME, "resume-tailor")
        wire = json.loads(self.requests[0]["input"][1]["content"])
        self.assertEqual(wire["materials"], approvals[0][0])
        self.assertAlmostEqual(approvals[0][1]["projected_rmb"], .683808)
        raw = json.dumps(self.requests, ensure_ascii=False)
        for private in ("人工候选人", "能源与动力工程", "offline-prep-placeholder", str(self.root)):
            self.assertNotIn(private, raw)

    async def test_material_change_during_confirmation_cancels_request(self):
        runtime, identity = self.runtime()
        async def approve(workspace, *_):
            workspace.resume += "\n新的能力"
            return True
        runtime.approve_llm = approve
        with self.assertRaisesRegex(ValueError, "材料发生变化"):
            await runtime.prepare_job(identity, RESUME, "resume-tailor")
        self.assertEqual(self.requests, [])

    async def test_each_preparation_requires_new_confirmation(self):
        runtime, identity = self.runtime()
        runtime.approve_llm = AsyncMock(side_effect=[True, False])
        await runtime.prepare_job(identity, RESUME, "resume-tailor")
        self.assertIsNone(await runtime.prepare_job(identity, RESUME, "interview-prep"))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(runtime.approve_llm.await_count, 2)

    async def test_pending_confirmation_blocks_other_analysis_from_replacing_resume(self):
        runtime, identity = self.runtime()
        waiting, release = asyncio.Event(), asyncio.Event()
        async def approve(*_):
            waiting.set()
            await release.wait()
            return True
        runtime.approve_llm = approve
        task = asyncio.create_task(runtime.prepare_job(identity, RESUME, "resume-tailor"))
        try:
            await waiting.wait()
            with self.assertRaisesRegex(ValueError, "已有分析"):
                await runtime.analyze(JD, "另一份背景")
            release.set()
            self.assertEqual((await task)["mode"], "llm")
        finally:
            release.set()
            await task
        self.assertEqual(len(self.requests), 1)

    async def test_repeated_tool_cannot_reuse_single_request_approval(self):
        from agent_platform.demo import current_turn, tool, answer
        from job_agent.runtime import JobModel
        class Repeated(JobModel):
            async def get_response(self, system_instructions, input, *args, **kwargs):
                _, outputs = current_turn(input)
                if not outputs:
                    return tool("load_skill", {"names": ["resume-tailor"]})
                if len(outputs) <= 2:
                    return tool("resume_tailor", {})
                return answer("结束")
        runtime, identity = self.runtime(fallback="none")
        with patch.object(runtime, "demo_model", return_value=Repeated()):
            self.assertIsNone(await runtime.prepare_job(identity, RESUME, "resume-tailor"))
        self.assertEqual(len(self.requests), 1)

    async def test_budget_and_request_caps_reject_before_approval_or_http(self):
        for requests, tokens in ((6, 0), (0, 400000)):
            runtime, identity = self.runtime(fallback="none")
            runtime.llm.requests, runtime.llm.input_tokens = requests, tokens
            self.assertIsNone(await runtime.prepare_job(identity, RESUME, "resume-tailor"))
            runtime.approve_llm.assert_not_called()
        self.assertEqual(self.requests, [])

    async def test_http_failure_falls_back_without_retry_and_stops_later_requests(self):
        self.failure = lambda request, task: httpx2.Response(429, json={"error": {"message": "PRIVATE PROVIDER BODY", "type": "rate_limit_error"}})
        runtime, identity = self.runtime()
        first = await runtime.prepare_job(identity, RESUME, "resume-tailor")
        second = await runtime.prepare_job(identity, RESUME, "interview-prep")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(first["mode"], "local")
        self.assertEqual(first["model_requests"], 1)
        self.assertIsNone(first["estimated_rmb"])
        self.assertIn("HTTP 429", first["fallback_reason"])
        self.assertEqual(second["model_requests"], 0)
        self.assertNotIn("PRIVATE PROVIDER BODY", str(self.events))
        self.assertIn("未知", Path(first["files"]["report.md"]).read_text())

    async def test_invalid_structure_falls_back_once_with_known_usage(self):
        self.failure = lambda request, task: httpx2.Response(200, json=api_response({"made_up": True}))
        runtime, identity = self.runtime()
        data = await runtime.prepare_job(identity, RESUME, "resume-tailor")
        self.assertEqual(data["mode"], "local")
        self.assertIn("JSON", data["fallback_reason"])
        self.assertAlmostEqual(data["estimated_rmb"], .006)
        self.assertEqual(len(self.requests), 1)

    async def test_fabricated_metric_evidence_fails_and_no_fallback_saves_nothing(self):
        def bad(request, task):
            value = tailor_output(task["materials"])
            value["revisions"][0]["after"] = "Python 本地测试提升准确率99%。"
            return httpx2.Response(200, json=api_response(value))
        self.failure = bad
        runtime, identity = self.runtime(fallback="none")
        self.assertIsNone(await runtime.prepare_job(identity, RESUME, "resume-tailor"))
        self.assertTrue(runtime.llm.failed)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((runtime.workspace.root / "preparations").exists())

    async def test_missing_usage_stops_future_calls_and_is_not_reported_as_free(self):
        self.failure = lambda request, task: httpx2.Response(200, json=api_response(tailor_output(task["materials"]), usage=False))
        runtime, identity = self.runtime()
        data = await runtime.prepare_job(identity, RESUME, "resume-tailor")
        self.assertIsNone(data["estimated_rmb"])
        self.assertTrue(runtime.llm.failed)
        await runtime.prepare_job(identity, RESUME, "resume-tailor")
        self.assertEqual(len(self.requests), 1)

    async def test_connection_timeout_never_retries_paid_request(self):
        def timeout(request, task):
            raise httpx2.ReadTimeout("offline timeout", request=request)
        self.failure = timeout
        runtime, identity = self.runtime()
        data = await runtime.prepare_job(identity, RESUME, "resume-tailor")
        self.assertEqual(data["mode"], "local")
        self.assertIsNone(data["estimated_rmb"])
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(runtime.llm.failed)

    async def test_original_file_library_and_state_preserved_and_exports_are_new(self):
        runtime, identity = self.runtime()
        library = runtime.workspace.memory.library
        library.set_status(identity, "interview")
        library.add_note(identity, "人工验收备注")
        before = library.get(identity)
        original = runtime.workspace.root / "resume.md"
        original.write_text(RESUME)
        first = await runtime.prepare_job(identity, original.read_text(), "resume-tailor")
        second = await runtime.prepare_job(identity, original.read_text(), "resume-tailor")
        self.assertNotEqual(first["files"], second["files"])
        self.assertEqual(original.read_text(), RESUME)
        self.assertEqual(library.get(identity), before)
        self.assertEqual(Path(first["files"]["tailored-resume.md"]).stat().st_mode & 0o777, 0o600)

    async def test_report_terminal_discloses_actual_llm_cost(self):
        runtime, identity = self.runtime()
        data = await runtime.prepare_job(identity, RESUME, "resume-tailor")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            print_preparation(data)
        self.assertIn("本次模型请求 1", buffer.getvalue())
        self.assertIn("0.006000", buffer.getvalue())
        self.assertNotIn("费用 0 元", buffer.getvalue())

    async def test_cli_confirms_each_skill_with_full_scope_and_price(self):
        runtime, identity = self.runtime()
        # CLI 自己打开同一个测试库，不用测试 Runtime 的 approve 替代实际确认入口。
        resume_file = self.root / "artificial-resume.md"
        resume_file.write_text(RESUME)
        args = Namespace(live=False, list_jobs=False, jd=None, resume=str(resume_file), skill="all", mode="llm",
                         model="gpt-6-sol", fallback="none", llm_dry_run=False, state_dir=None, session="default")
        lines = [f"/tailor {identity}", "确认 LLM 分析", f"/interview-prep {identity}", "取消", "/quit"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("job_agent.cli.PROJECT", self.root), \
             patch("job_agent.cli.sys.stdin.isatty", return_value=True), patch("job_agent.cli.sys.stdout.isatty", return_value=True), \
             patch("job_agent.cli.read_line", new=AsyncMock(side_effect=lines)):
            self.assertEqual(await run_job(args), 0)
        self.assertEqual(len(self.requests), 1)
        text = output.getvalue()
        for label in ("https://example.org/v1", "gpt-6-sol", "发送范围", "0.6838", "本 Skill 最多 2 次", '"resume_facts"'):
            self.assertIn(label, text)
        self.assertIn("未确认", text)
        self.assertNotIn("offline-prep-placeholder", text)


if __name__ == "__main__":
    unittest.main()
