"""一次性验收脚本的离线测试；真实SDK+MockTransport，不访问中转站。"""

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
from unittest.mock import patch

import httpx2
from openai import AsyncOpenAI

import accept_job_v6_api as acceptance
from job_agent.llm import LLMAnalyzer, LLMConfig, LLMError
from test_job_agent_llm import api_response
from test_job_agent_preparation_llm import interview_output, tailor_output


class AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        shutil.copytree(acceptance.PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        shutil.copytree(acceptance.PROJECT / "job_agent/examples", self.root / "job_agent/examples")
        self.directory = self.root / ".agent-platform/acceptance"
        acceptance.safe_directory(self.directory)
        self.journal = acceptance.Journal(self.directory)
        self.jd, self.resume = acceptance.artificial_materials(self.root)
        self.prepared = acceptance.prepared_materials(self.jd, self.resume)
        self.config = acceptance.acceptance_config(LLMConfig("offline-acceptance-placeholder", acceptance.RECORDED_ENDPOINT, acceptance.MODEL, .30, 1.50))
        self.requests = []
        self.failure = None
        for p in (patch.dict(os.environ, {"OPENAI_API_KEY": self.config.api_key, "OPENAI_BASE_URL": self.config.base_url}),
                  patch.object(socket, "getaddrinfo", side_effect=AssertionError("禁止真实网络")),
                  patch("job_agent.llm.AsyncOpenAI", side_effect=self.factory)):
            p.start()
            self.addCleanup(p.stop)

    def factory(self, **kwargs):
        self.assertEqual(kwargs["max_retries"], 0)
        return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler), trust_env=False), **kwargs)

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertEqual(body["max_output_tokens"], 4096)
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertFalse(body["store"])
        task = json.loads(body["input"][1]["content"])
        if self.failure:
            return self.failure(request, task)
        value = tailor_output(task["materials"]) if task["skill"] == "resume-tailor" else interview_output(task["materials"])
        return httpx2.Response(200, json=api_response(value))

    async def run_acceptance(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return await acceptance.execute(self.config, self.prepared, self.jd, self.resume, self.journal, self.root)

    async def test_two_skills_one_call_each_schema_evidence_and_cost_reported(self):
        self.assertEqual(await self.run_acceptance(), 0)
        data = self.journal.data
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([json.loads(r["input"][1]["content"])["skill"] for r in self.requests], list(acceptance.SKILLS))
        self.assertEqual(data["real_api_requests"], 2)
        self.assertEqual(data["usage"]["input_tokens"], 2000)
        self.assertEqual(data["usage"]["output_tokens"], 400)
        self.assertAlmostEqual(data["usage"]["estimated_rmb"], .012)
        self.assertIsNone(data["billed_rmb"])
        self.assertTrue(all(item["schema_validation"] == "通过" for item in data["attempts"]))
        self.assertTrue(all(item["evidence_validation"] == "通过" for item in data["results"]))
        self.assertTrue(all(item["original_materials_and_job_unchanged"] for item in data["results"]))
        self.assertIn("仍待人工核对", data["quality_conclusion"])
        self.assertTrue((self.directory / "summary.md").is_file())
        self.assertEqual((self.directory / "acceptance.json").stat().st_mode & 0o777, 0o600)

    async def test_default_preview_has_no_http_or_acceptance_record(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(acceptance, "DIRECTORY", self.directory):
            self.assertEqual(acceptance.main([]), 0)
        self.assertIn("仅预览", output.getvalue())
        self.assertIn("0.302880", output.getvalue())
        self.assertEqual(self.requests, [])
        self.assertFalse(self.journal.path.exists())

    async def test_missing_key_does_not_call_or_write_charge_record(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch.object(acceptance, "DIRECTORY", self.directory), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(acceptance.main(["--run"]), 1)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.journal.path.exists())

    async def test_changed_fixture_cannot_be_replaced_with_real_resume(self):
        (self.root / "job_agent/examples/preparation-resume.example.md").write_text("真实个人资料")
        with self.assertRaisesRegex(ValueError, "拒绝替换"):
            acceptance.artificial_materials(self.root)
        self.assertEqual(self.requests, [])

    async def test_budget_checks_entire_two_skill_flow_before_request(self):
        values, total = acceptance.checked_total(LLMAnalyzer(self.config), self.prepared)
        self.assertAlmostEqual(total, .30288)
        self.assertEqual(len(values), 2)
        expensive = LLMConfig(self.config.api_key, self.config.base_url, acceptance.MODEL, 1, 2)
        with self.assertRaisesRegex(LLMError, "完整流程"):
            acceptance.checked_total(LLMAnalyzer(expensive), self.prepared)
        self.assertEqual(self.requests, [])

    async def test_per_request_budget_and_count_are_tighter_than_business_defaults(self):
        self.journal.start(self.config, self.prepared)
        backend = acceptance.AcceptanceLLM(self.config, self.journal)
        for requests, projected in ((2, .1), (0, .351)):
            backend.requests = requests
            with self.assertRaisesRegex(LLMError, "2.*次"):
                backend.preflight({"calls": 1, "projected_rmb": projected})
        self.assertEqual(self.requests, [])

    async def test_same_skill_cannot_make_second_paid_attempt(self):
        self.journal.start(self.config, self.prepared)
        backend = acceptance.AcceptanceLLM(self.config, self.journal)
        value = self.prepared["resume-tailor"]
        await backend.ask("resume-tailor", value["payload"], value["schema"])
        with self.assertRaisesRegex(LLMError, "已尝试"):
            await backend.ask("resume-tailor", value["payload"], value["schema"])
        self.assertEqual(len(self.requests), 1)

    async def test_http_failure_stops_before_second_skill_no_retry_and_unknown_fee(self):
        self.failure = lambda request, task: httpx2.Response(429, json={"error": {"message": "PRIVATE PROVIDER BODY", "type": "rate_limit_error"}})
        self.assertEqual(await self.run_acceptance(), 1)
        self.assertEqual(len(self.requests), 1)
        data = self.journal.data
        self.assertEqual(data["status"], "failed")
        self.assertIsNone(data["usage"]["estimated_rmb"])
        self.assertNotIn("PRIVATE PROVIDER BODY", json.dumps(data))
        self.assertIn("未知", (self.directory / "summary.md").read_text())

    async def test_invalid_schema_keeps_usage_and_stops_without_second_call(self):
        self.failure = lambda request, task: httpx2.Response(200, json=api_response({"invalid": True}))
        self.assertEqual(await self.run_acceptance(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertAlmostEqual(self.journal.data["usage"]["estimated_rmb"], .006)
        self.assertNotEqual(self.journal.data["attempts"][0]["schema_validation"], "通过")

    async def test_hallucinated_metric_schema_passes_but_evidence_fails(self):
        def fabricated(request, task):
            value = tailor_output(task["materials"])
            value["revisions"][0]["after"] = "Python 本地测试准确率提升99%。"
            return httpx2.Response(200, json=api_response(value))
        self.failure = fabricated
        self.assertEqual(await self.run_acceptance(), 1)
        self.assertEqual(len(self.requests), 1)
        data = self.journal.data
        self.assertEqual(data["attempts"][0]["schema_validation"], "通过")
        self.assertEqual(data["results"][0]["evidence_validation"], "未通过/未完成")
        self.assertFalse((self.directory / "runtime/preparations").exists())

    async def test_rerunning_finished_session_only_displays_report(self):
        await self.run_acceptance()
        output = io.StringIO()
        with patch.object(acceptance, "DIRECTORY", self.directory), contextlib.redirect_stdout(output):
            self.assertEqual(acceptance.main(["--run"]), 0)
        self.assertEqual(len(self.requests), 2)
        self.assertIn("不再次调用API", output.getvalue())

    async def test_crash_record_blocks_automatic_resume_even_with_missing_key(self):
        self.journal.start(self.config, self.prepared)
        self.journal.data["attempts"].append({"skill": "resume-tailor", "status": "in_flight"})
        self.journal.save()
        output = io.StringIO()
        with patch.object(acceptance, "DIRECTORY", self.directory), patch.dict(os.environ, {"OPENAI_API_KEY": ""}), contextlib.redirect_stdout(output):
            self.assertEqual(acceptance.main(["--run"]), 0)
        self.assertIn("未知", output.getvalue())
        self.assertEqual(self.requests, [])

    async def test_concurrent_run_rejected_before_any_request(self):
        with acceptance.exclusive(self.directory):
            with self.assertRaisesRegex(ValueError, "已有验收进程"):
                with acceptance.exclusive(self.directory):
                    self.fail("不应获得第二把锁")
        self.assertEqual(self.requests, [])

    async def test_altered_payload_cannot_use_previous_confirmation(self):
        self.journal.start(self.config, self.prepared)
        backend = acceptance.AcceptanceLLM(self.config, self.journal)
        value = self.prepared["resume-tailor"]
        with self.assertRaisesRegex(LLMError, "不一致"):
            await backend.ask("resume-tailor", {**value["payload"], "extra": "别的资料"}, value["schema"])
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
