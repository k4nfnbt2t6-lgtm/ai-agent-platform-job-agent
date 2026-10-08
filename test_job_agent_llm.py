"""v2 离线验证：真实 SDK + 模拟 HTTP；明确禁止真实网络与付费。"""

import asyncio
import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx2
from openai import AsyncOpenAI

from agent_platform.runtime import PlatformRuntime
from agent_platform.terminal_ui import PROJECT
from job_agent import llm
from job_agent.cli import run as run_job
from job_agent.runtime import JobRuntime
from job_agent.schemas import GapOutput, JDOutput, MatchOutput


JD = "公司：人工科技\n岗位：AI应用实习\n职责：开发智能助手\n任职要求：Python、MCP、LLM API、AI应用项目经验"
RESUME = "目标：AI应用开发实习\n项目：AI Agent Platform v1.0 / Job Agent v1\n技能：Python基础\n正在学习或尚未熟练：MCP\nAPI基础"
TECHS = ("Python", "MCP", "LLM API", "AI 应用开发")


def jd_output(names=TECHS):
    return {"company": {"text": "人工科技", "evidence": "公司:人工科技"},
            "title": {"text": "AI应用实习", "evidence": "岗位:AI应用实习"},
            "responsibilities": [{"text": "开发智能助手", "evidence": "开发智能助手"}],
            "technical_requirements": [{"skill": name, "level": "required", "evidence": "要求:Python、MCP、LLM API、AI应用项目经验"} for name in names],
            "education_requirements": [], "experience_requirements": [], "other_requirements": [],
            "keywords": list(names), "limitations": []}


def match_output(segments, names=TECHS):
    selectors = {"Python": "Python", "MCP": "MCP", "LLM API": "API基础", "AI 应用开发": "AI Agent Platform"}
    items = []
    for name in names:
        refs = [x["id"] for x in segments if selectors.get(name, name) in x["text"]]
        items.append({"skill": name, "status": "matched", "evidence_ids": refs[:1], "reason": "模型根据该条材料判断相关能力。"})
    return {"technical_matches": items, "education": [], "experience": [], "limitations": []}


def api_response(value, *, usage=True, status="completed", raw=False):
    return {"id": "offline-response", "object": "response", "created_at": 0, "status": status,
            "model": "gpt-6-sol", "output": [{"type": "message", "id": "offline-message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": value if raw else json.dumps(value, ensure_ascii=False), "annotations": []}]}],
            "usage": {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200} if usage else None}


class LLMSchemaTests(unittest.TestCase):
    def test_json_schemas_forbid_extra_fields_and_require_every_field(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                    self.assertEqual(set(node.get("properties", {})), set(node.get("required", [])))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        for model in (JDOutput, MatchOutput, GapOutput):
            walk(model.model_json_schema())

    def test_fabricated_jd_quote_is_rejected(self):
        value = jd_output()
        value["company"]["evidence"] = "不存在的公司原文"
        with self.assertRaisesRegex(llm.LLMError, "原文引用"):
            llm.checked_jd(JDOutput.model_validate(value), JD.replace("：", ":"))

    def test_learning_mcp_python_basics_and_api_basics_never_become_mastery(self):
        resume = RESUME.replace("：", ":")
        value = MatchOutput.model_validate(match_output(llm.evidence_segments(resume)))
        jd = llm.checked_jd(JDOutput.model_validate(jd_output()), JD.replace("：", ":"))
        result = llm.checked_match(value, jd, resume)
        statuses = {x["skill"]: x["status"] for x in result["technical_matches"]}
        self.assertEqual(statuses, {"Python": "learning", "MCP": "learning", "LLM API": "learning", "AI 应用开发": "matched"})
        self.assertEqual(result["required_coverage"]["matched"], 1)
        self.assertEqual(result["match_scores"]["required"]["partial"], 3)
        self.assertEqual(result["match_scores"]["overall"]["percent"], 62.5)
        self.assertTrue(result["evidence_checks"])

    def test_llm_scores_are_computed_after_evidence_guards_with_bonus_weight(self):
        resume = "项目：AI Agent Platform v1.0 / Job Agent v1\n正在学习或尚未熟练：MCP\n目标：AI应用开发实习"
        jd = llm.checked_jd(JDOutput.model_validate(jd_output(("MCP", "AI 应用开发", "RAG"))), JD.replace("：", ":"))
        jd["technical_requirements"][1]["level"] = "preferred"
        value = match_output(llm.evidence_segments(resume), ("MCP", "AI 应用开发", "RAG"))
        result = llm.checked_match(MatchOutput.model_validate(value), jd, resume)
        self.assertEqual(result["match_scores"]["required"], {"matched": 0, "partial": 1, "unmatched": 1,
                                                               "total": 2, "points": .5, "percent": 25.0})
        self.assertEqual(result["match_scores"]["preferred"]["percent"], 100)
        self.assertEqual(result["match_scores"]["overall"]["percent"], 40)
        self.assertNotIn("目标", " ".join(result["technical_matches"][1]["resume_evidence"]))

    def test_goal_only_is_never_capability_or_project_evidence(self):
        resume = "目标是AI应用开发实习"
        value = match_output(llm.evidence_segments(resume), names=("AI 应用开发",))
        value["technical_matches"][0]["evidence_ids"] = ["R1"]
        jd = llm.checked_jd(JDOutput.model_validate(jd_output(("AI 应用开发",))), JD.replace("：", ":"))
        result = llm.checked_match(MatchOutput.model_validate(value), jd, resume)
        self.assertEqual(result["technical_matches"][0]["status"], "not_evidenced")
        self.assertEqual(result["technical_matches"][0]["resume_evidence"], [])

    def test_positive_keyword_does_not_override_another_learning_section(self):
        resume = "掌握MCP\n正在学习或尚未熟练：\nMCP"
        jd = llm.checked_jd(JDOutput.model_validate(jd_output(("MCP",))), JD.replace("：", ":"))
        value = MatchOutput.model_validate(match_output(llm.evidence_segments(resume), ("MCP",)))
        self.assertEqual(llm.checked_match(value, jd, resume)["technical_matches"][0]["status"], "learning")

    def test_unknown_evidence_id_and_missing_requirements_are_rejected(self):
        resume = "掌握MCP"
        jd = llm.checked_jd(JDOutput.model_validate(jd_output(("MCP",))), JD.replace("：", ":"))
        for value in ({"technical_matches": [], "education": [], "experience": [], "limitations": []},
                      {"technical_matches": [{"skill": "MCP", "status": "matched", "evidence_ids": ["R999"], "reason": "虚构"}], "education": [], "experience": [], "limitations": []}):
            with self.subTest(value=value), self.assertRaises(llm.LLMError):
                llm.checked_match(MatchOutput.model_validate(value), jd, resume)

    def test_semantic_relation_can_match_without_identical_skill_keyword(self):
        resume = "项目：给问答助手检索公司知识库，把文档与出处交给模型后生成答案。"
        jd = llm.checked_jd(JDOutput.model_validate(jd_output(("RAG",))), JD.replace("：", ":"))
        value = {"technical_matches": [{"skill": "RAG", "status": "matched", "evidence_ids": ["R1"], "reason": "项目描述具有检索后增强生成和引用来源的实践。"}],
                 "education": [], "experience": [], "limitations": []}
        result = llm.checked_match(MatchOutput.model_validate(value), jd, resume)
        self.assertEqual(result["technical_matches"][0]["status"], "matched")
        self.assertIn("公司知识库", result["technical_matches"][0]["resume_evidence"][0])

    def test_gap_model_cannot_add_fake_skills_or_promote_learning_strengths(self):
        jd = llm.checked_jd(JDOutput.model_validate(jd_output()), JD.replace("：", ":"))
        resume = RESUME.replace("：", ":")
        matching = llm.checked_match(MatchOutput.model_validate(match_output(llm.evidence_segments(resume))), jd, resume)
        plans = [{"skill": name, "preparation": "做真实的岗位相关练习。", "acceptance": "展示实际结果。"} for name in TECHS[:3]]
        data = llm.checked_gaps(GapOutput.model_validate({"preparations": plans, "note": "确认项目证据。"}), matching)
        self.assertEqual([x["skill"] for x in data["strengths"]], ["AI 应用开发"])
        self.assertEqual(data["priority_actions"][0]["priority"], "P1")
        plans.append({"skill": "不存在的技能", "preparation": "假的", "acceptance": "假的"})
        with self.assertRaises(llm.LLMError):
            llm.checked_gaps(GapOutput.model_validate({"preparations": plans, "note": ""}), matching)

    def test_project_name_alone_cannot_invent_specific_stack_or_work_experience(self):
        resume = "项目：AI Agent Platform v1.0 / Job Agent v1"
        jd = llm.checked_jd(JDOutput.model_validate(jd_output(("MCP",))), JD.replace("：", ":"))
        jd["experience_requirements"] = ["一年实习经验"]
        value = {"technical_matches": [{"skill": "MCP", "status": "matched", "evidence_ids": ["R1"], "reason": "模型不当猜测项目用了MCP。"}],
                 "education": [], "experience": [{"requirement": "一年实习经验", "status": "matched", "evidence_ids": ["R1"], "reason": "模型不当推断。"}], "limitations": []}
        result = llm.checked_match(MatchOutput.model_validate(value), jd, resume)
        self.assertEqual(result["technical_matches"][0]["status"], "not_evidenced")
        self.assertEqual(result["experience"][0]["status"], "needs_confirmation")


class LLMRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        shutil.copytree(PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        self.events, self.requests, self.runtimes = [], [], []
        for name in ("connect", "connect_ex"):
            blocker = patch.object(socket.socket, name, side_effect=AssertionError("禁止真实网络"))
            blocker.start()
            self.addCleanup(blocker.stop)
        env = patch.dict(os.environ, {"OPENAI_API_KEY": "offline-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        client = patch.object(llm, "AsyncOpenAI", side_effect=self.factory)
        client.start()
        self.addCleanup(client.stop)
        self.failure = None

    async def asyncTearDown(self):
        for runtime in self.runtimes:
            await runtime.close()

    def make_runtime(self, **kwargs):
        runtime = JobRuntime(self.root, self.events.append, mode="llm", approve_llm=AsyncMock(return_value=True), **kwargs)
        self.runtimes.append(runtime)
        return runtime

    def factory(self, **kwargs):
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertEqual(kwargs["timeout"], 120.0)
        return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler), trust_env=False), **kwargs)

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertIs(body["text"]["format"]["strict"], True)
        self.assertIs(body["store"], False)
        self.assertEqual(body["max_output_tokens"], llm.LLMConfig("", "", "gpt-6-sol", .3, 1.5).policy(json.loads(body["input"][1]["content"])["skill"])["max_output_tokens"])
        task = json.loads(body["input"][1]["content"])
        if self.failure:
            return self.failure(request, task)
        if task["skill"] == "analyze-jd":
            value = jd_output()
        elif task["skill"] == "match-resume":
            value = match_output(task["materials"]["resume_segments"])
        else:
            matching = task["materials"]["matching"]
            value = {"preparations": [{"skill": x["skill"], "preparation": "结合岗位做最小实现。", "acceptance": "运行并解释测试结果。"}
                                       for x in matching["technical_matches"] if x["status"] != "matched"], "note": "证据与准备建议分开。"}
        return httpx2.Response(200, json=api_response(value))

    async def test_real_sdk_mock_http_runs_all_three_skills_and_saves_structured_report(self):
        runtime = self.make_runtime()
        result = await runtime.analyze(JD, RESUME)
        self.assertEqual(result["mode"], "llm")
        self.assertIs(JobRuntime.execute, PlatformRuntime.execute)
        self.assertIs(JobRuntime.run_task, PlatformRuntime.run_task)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual([json.loads(x["input"][1]["content"])["skill"] for x in self.requests], ["analyze-jd", "match-resume", "skill-gap-analysis"])
        self.assertEqual(runtime.usage()["input_tokens"], 3000)
        self.assertAlmostEqual(runtime.usage()["estimated_rmb"], .018)
        self.assertEqual(runtime.workspace.memory.list_reports(JD)[0]["mode"], "llm")
        saved = runtime.workspace.memory.list_reports(JD)[0]
        self.assertEqual(saved["resume_match"]["matching"]["match_scores"], result["resume_match"]["matching"]["match_scores"])
        raw = json.dumps(self.requests, ensure_ascii=False)
        self.assertNotIn("offline-test-placeholder", raw)
        self.assertNotIn(str(self.root), raw)
        self.assertNotIn("历史会话", raw.split('"role": "user"')[-1])

    async def test_duplicate_jd_reuses_only_llm_extraction_and_changed_resume_is_fresh(self):
        runtime = self.make_runtime()
        await runtime.analyze(JD, RESUME)
        result = await runtime.analyze(JD, "目标：AI应用开发实习")
        self.assertEqual(len(self.requests), 5)
        self.assertTrue(result["jd_analysis"]["cache_hit"])
        self.assertTrue(all(x["status"] == "not_evidenced" for x in result["resume_match"]["matching"]["technical_matches"]))
        self.assertEqual(len(runtime.workspace.memory.list_jobs()), 1)
        self.assertEqual(len(runtime.workspace.memory.list_reports(JD)), 2)

    async def test_local_and_llm_jd_caches_do_not_contaminate_each_other(self):
        runtime = self.make_runtime()
        await runtime.analyze(JD, RESUME)
        self.assertIsNone(runtime.workspace.memory.get(JD))
        local = JobRuntime(self.root, self.events.append)
        self.runtimes.append(local)
        data = await local.analyze(JD, RESUME)
        self.assertEqual(data["jd_analysis"]["analysis"]["method"], "local_rules_v3")
        self.assertEqual(runtime.workspace.memory.get_llm(JD, "gpt-6-sol")["analysis"]["method"], "llm_semantic_v1")
        self.assertEqual(len(runtime.workspace.memory.list_jobs()), 1)
        self.assertIsNone(runtime.workspace.memory.get_llm(JD, "another-model"))

    async def test_existing_local_job_does_not_falsely_claim_llm_cache_hit(self):
        local = JobRuntime(self.root, self.events.append)
        self.runtimes.append(local)
        await local.analyze(JD, RESUME)
        runtime = self.make_runtime()
        data = await runtime.analyze(JD, RESUME)
        self.assertEqual(len(self.requests), 3)
        self.assertFalse(data["jd_analysis"]["cache_hit"])
        self.assertEqual(len(runtime.workspace.memory.list_jobs()), 1)

    async def test_authorization_refusal_or_absence_makes_no_request(self):
        runtime = self.make_runtime()
        runtime.approve_llm = AsyncMock(return_value=False)
        self.assertIsNone(await runtime.analyze(JD, RESUME))
        runtime.approve_llm = None
        self.assertIsNone(await runtime.analyze(JD, RESUME))
        self.assertEqual(self.requests, [])
        self.assertEqual(runtime.workspace.memory.list_jobs(), [])

    async def test_http_401_falls_back_once_then_disables_llm_for_remaining_jobs(self):
        self.failure = lambda request, task: httpx2.Response(401, json={"error": {"message": "PRIVATE BODY offline-test-placeholder", "type": "invalid_request_error"}})
        runtime = self.make_runtime()
        first = await runtime.analyze(JD, RESUME)
        second = await runtime.analyze(JD, RESUME)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(first["mode"], "local_rules")
        self.assertEqual(second["mode"], "local_rules")
        self.assertTrue(first["fallback_reason"])
        self.assertTrue(runtime.usage()["unknown_usage"])
        self.assertIsNone(runtime.usage()["estimated_rmb"])
        events = "\n".join(x.text for x in self.events)
        self.assertIn("HTTP 401", events)
        self.assertNotIn("PRIVATE BODY", events)
        self.assertNotIn("offline-test-placeholder", events)

    async def test_timeout_falls_back_without_retry(self):
        def fail(request, task):
            raise httpx2.ReadTimeout("private request detail", request=request)
        self.failure = fail
        runtime = self.make_runtime()
        self.assertEqual((await runtime.analyze(JD, RESUME))["mode"], "local_rules")
        self.assertEqual(len(self.requests), 1)

    async def test_fallback_none_stops_without_saving_partial_final_report(self):
        self.failure = lambda request, task: httpx2.Response(503, json={"error": {"message": "unavailable"}})
        runtime = self.make_runtime(fallback="none")
        self.assertIsNone(await runtime.analyze(JD, RESUME))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(runtime.workspace.memory.list_reports(JD), [])

    async def test_invalid_json_and_incomplete_output_fall_back(self):
        for status in ("completed", "incomplete"):
            runtime = self.make_runtime()
            self.failure = lambda request, task: httpx2.Response(200, json=api_response("not JSON", status=status, raw=True))
            data = await runtime.analyze(JD, RESUME)
            self.assertEqual(data["mode"], "local_rules")
            self.assertTrue(runtime.llm.failed)
            self.assertEqual(runtime.llm.requests, 1)

    async def test_missing_usage_stops_and_does_not_claim_zero_cost(self):
        self.failure = lambda request, task: httpx2.Response(200, json=api_response(jd_output(), usage=False))
        runtime = self.make_runtime()
        self.assertEqual((await runtime.analyze(JD, RESUME))["mode"], "local_rules")
        self.assertTrue(runtime.usage()["unknown_usage"])
        self.assertTrue(runtime.llm.failed)
        self.assertEqual(len(self.requests), 1)

    async def test_request_and_budget_limits_block_before_sending(self):
        for limit in ("requests", "budget"):
            runtime = self.make_runtime()
            if limit == "requests":
                runtime.llm.requests = 6
            else:
                runtime.llm.input_tokens = 400_000
            result = await runtime.analyze(JD, RESUME)
            self.assertIsNone(result)  # 预算不足先停，不自动降低质量
            runtime.approve_llm.assert_not_called()
        self.assertEqual(self.requests, [])

    async def test_model_config_and_prices_use_env_without_keys_in_representation(self):
        with patch.dict(os.environ, {"OPENAI_MODEL": "other-model", "JOB_AGENT_INPUT_PRICE_USD_PER_MILLION": "0.5", "JOB_AGENT_OUTPUT_PRICE_USD_PER_MILLION": "2"}):
            config = llm.LLMConfig.from_env()
            self.assertEqual(config.model, "other-model")
            self.assertNotIn("offline-test-placeholder", repr(config))
            self.assertEqual(llm.LLMConfig.from_env("explicit-model").model, "explicit-model")
        with patch.dict(os.environ, {"OPENAI_MODEL": "other-model"}):
            with self.assertRaisesRegex(llm.LLMError, "单价|PRICE"):
                llm.LLMConfig.from_env()

    async def test_single_skill_modes_make_only_required_requests(self):
        for selected, expected in (("analyze-jd", 1), ("match-resume", 1), ("skill-gap-analysis", 2)):
            runtime = self.make_runtime()
            before = len(self.requests)
            data = await runtime.analyze(JD, None if selected == "analyze-jd" else RESUME, selected)
            self.assertEqual(data["mode"], "llm")
            self.assertEqual(len(self.requests) - before, expected)

    async def test_cli_dry_run_has_no_client_or_state_writes(self):
        jd, resume = self.root / "jd.md", self.root / "resume.md"
        jd.write_text(JD)
        resume.write_text(RESUME)
        args = Namespace(live=False, list_jobs=False, jd=str(jd), resume=str(resume), skill="all", mode="llm", model=None,
                         fallback="local", llm_dry_run=True, state_dir=None, session="default")
        output = io.StringIO()
        with patch("job_agent.cli.JobRuntime") as runtime, patch.object(llm, "AsyncOpenAI") as client, contextlib.redirect_stdout(output):
            self.assertEqual(await run_job(args), 0)
            runtime.assert_not_called()
            client.assert_not_called()
        self.assertIn("只预览", output.getvalue())
        self.assertNotIn("offline-test-placeholder", output.getvalue())

    async def test_cli_noninteractive_llm_cannot_bypass_approval(self):
        args = Namespace(live=False, list_jobs=False, jd=None, resume=None, skill="all", mode="llm", model=None, fallback="local", llm_dry_run=False)
        with patch("job_agent.cli.sys.stdin.isatty", return_value=False), self.assertRaisesRegex(ValueError, "交互式"):
            await run_job(args)
        self.assertEqual(self.requests, [])

    async def test_second_stage_bad_evidence_falls_back_with_isolated_valid_jd_cache(self):
        def bad_match(request, task):
            if task["skill"] == "analyze-jd":
                return httpx2.Response(200, json=api_response(jd_output()))
            value = match_output(task["materials"]["resume_segments"])
            value["technical_matches"][0]["evidence_ids"] = ["invented-evidence"]
            return httpx2.Response(200, json=api_response(value))
        self.failure = bad_match
        runtime = self.make_runtime()
        result = await runtime.analyze(JD, RESUME)
        self.assertEqual(result["mode"], "local_rules")
        self.assertEqual(len(self.requests), 2)
        self.assertIn("证据编号", result["fallback_reason"])
        self.assertIsNotNone(runtime.workspace.memory.get_llm(JD, "gpt-6-sol"))
        self.assertEqual(runtime.workspace.memory.list_reports(JD)[0]["mode"], "local_rules")
        self.assertEqual(len(runtime.workspace.memory.list_jobs()), 1)

    async def test_cli_mock_confirmed_llm_uses_real_entry_with_file_materials(self):
        jd, resume = self.root / "jd.md", self.root / "resume.md"
        jd.write_text(JD)
        resume.write_text(RESUME)
        args = Namespace(live=False, list_jobs=False, jd=str(jd), resume=str(resume), skill="all", mode="llm", model=None,
                         fallback="local", llm_dry_run=False, state_dir=None, session="default")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("job_agent.cli.PROJECT", self.root), patch("job_agent.cli.sys.stdin.isatty", return_value=True), \
             patch("job_agent.cli.sys.stdout.isatty", return_value=True), patch("job_agent.cli.read_line", new=AsyncMock(return_value="确认 LLM 分析")):
            self.assertEqual(await run_job(args), 0)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("分析模式：llm", output.getvalue())
        self.assertIn("原文证据", output.getvalue())
        self.assertNotIn("offline-test-placeholder", output.getvalue())

    async def test_pending_confirmation_cannot_be_replaced_by_concurrent_materials(self):
        runtime = self.make_runtime()
        waiting = asyncio.Event()
        entered = asyncio.Event()
        async def approve(*args):
            entered.set()
            await waiting.wait()
            return False
        runtime.approve_llm = approve
        task = asyncio.create_task(runtime.analyze(JD, RESUME))
        await entered.wait()
        with self.assertRaisesRegex(ValueError, "材料确认"):
            await runtime.analyze("另一个JD", "另一份简历")
        waiting.set()
        self.assertIsNone(await task)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
