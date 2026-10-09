"""官方API格式MockTransport→mockLLM→真实隔离Chromium→SQLite。全部人工资料。"""

import json
import os
import shutil
import socket
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx2

from agent_platform.terminal_ui import PROJECT
from job_agent.analysis import analyze_jd
from job_agent.automation.adapters import Field
from job_agent.automation.applications import ApplicationService
from job_agent.automation.engine import AutomationRuntime, check_candidates
from job_agent.automation.mock import MockResponsesClient, RESUME
from job_agent.automation.policy import LLMConsent, Limits
from job_agent.automation.preflight import plan
from job_agent.automation.store import GuardError
from job_agent.automation.workflow import workflow
from job_agent.browser.lever import LeverSnapshotATS
from job_agent.channels import import_jd, private_json
from job_agent.lever import parse_lever
from job_agent.llm import LLMConfig
from job_agent.runtime import JobRuntime
from job_agent.search import PublicFetcher, load_sources, search_public
from job_agent.source_health import SourceHealthStore

POSTING = "11111111-1111-4111-8111-111111111111"
TARGET = "https://jobs.lever.co/artificial/" + POSTING
FORM = (PROJECT / "tests/fixtures/application-integration/lever-form.html").read_text()
EXTRAS = (Field("availability", kind="answer"), Field("agree", kind="consent"))
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def sources(region="global"):
    return load_sources(json.dumps([{"name": "人工Lever接口", "company": "人工接口科技",
        "kind": "lever", "tenant": "artificial", "region": region, "enabled": True}]))


def response_rows():
    return [{"id": POSTING, "text": "AI Agent开发实习生", "hostedUrl": TARGET, "applyUrl": TARGET + "/apply",
             "categories": {"location": "广州", "commitment": "Intern"},
             "descriptionPlain": "工作地点：广州\n岗位职责：实现工具调用和错误处理。",
             "lists": [{"text": "任职要求", "content": "<p>2027届本科在读，专业不限</p><p>Python、Tool Calling</p>"},
                       {"text": "加分项", "content": "<p>LLM API、MCP</p>"}]}]


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.source = sources()[0]

    def test_official_get_configuration_cannot_override_api_host(self):
        self.assertEqual(self.source["url"], "https://api.lever.co/v0/postings/artificial?mode=json&limit=100")
        self.assertTrue(sources("eu")[0]["url"].startswith("https://api.eu.lever.co/"))
        for tenant in ("../other", "https://evil.invalid", "a?key=test"):
            with self.subTest(tenant=tenant), self.assertRaises(ValueError):
                load_sources(json.dumps([{**self.source, "tenant": tenant}]))

    def test_lever_preserves_location_links_and_preferred_sections(self):
        jobs, issues = parse_lever(json.dumps(response_rows()), self.source)
        self.assertEqual(issues, [])
        self.assertEqual(jobs[0].city, "广州")
        self.assertEqual(jobs[0].recruitment_type, "实习")
        self.assertIsNone(jobs[0].valid_through)
        self.assertEqual(jobs[0].parsing_evidence["apply_url"], TARGET + "/apply")
        analysis = analyze_jd(jobs[0].analysis_text())
        levels = {r["skill"]: r["level"] for r in analysis["technical_requirements"]}
        self.assertEqual(levels["MCP"], "preferred")
        self.assertEqual(levels["Python"], "required")

    def test_cross_company_posting_or_application_link_is_rejected(self):
        for key, value in (("hostedUrl", TARGET.replace("artificial", "other")),
                           ("applyUrl", TARGET.replace(POSTING, "22222222-2222-4222-8222-222222222222") + "/apply"),
                           ("hostedUrl", "https://evil.invalid/jobs/1")):
            rows = response_rows()
            rows[0][key] = value
            jobs, issues = parse_lever(json.dumps(rows), self.source)
            self.assertEqual(jobs, [])
            self.assertTrue(issues)

    def test_invalid_api_payload_reports_failure(self):
        with self.assertRaises(ValueError):
            parse_lever('{"jobs": []}', self.source)
        self.assertTrue(parse_lever('[{}]', self.source)[1])

    def test_manual_import_retains_provenance_and_does_not_invent_eligibility(self):
        jd = self.root / "job.txt"
        jd.write_text("工作地点：广州\n岗位职责：开发AI应用\n技术要求：Python")
        output = self.root / ".agent-platform/imports/jobs.json"
        job = import_jd(jd_file=jd, url=TARGET, company="人工科技", title="AI应用开发", city="未知",
                        output=output, project=self.root)
        self.assertEqual(job.recruitment_type, "未知")
        self.assertIsNone(job.valid_through)
        self.assertFalse(job.parsing_evidence["live_fetch"])
        self.assertEqual(json.loads(output.read_text())[0]["url"], TARGET)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            private_json([], output, self.root)
        self.assertEqual(len(json.loads(output.read_text())), 1)

    def test_private_export_refuses_public_paths_and_links(self):
        with self.assertRaises(ValueError):
            private_json({}, self.root / "public.json", self.root)
        directory = self.root / ".agent-platform"
        directory.mkdir()
        (directory / "link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            private_json({}, directory / "link/data.json", self.root)

    def test_llm_plan_stays_disabled_and_keeps_quality_policy(self):
        config = LLMConfig("offline-placeholder", "https://api.example.invalid/v1", "gpt-6-sol", .3, 1.5)
        with patch("job_agent.llm.AsyncOpenAI", side_effect=AssertionError("计划不能建立客户端")):
            result = plan(RESUME, "人工JD：Python", config, Limits(), now=NOW)
        self.assertFalse(result["enabled"])
        self.assertFalse(result["fits_daily_budget"])
        self.assertFalse(result["consent_template"]["confirmed_data_transfer"])
        self.assertFalse(result["consent_template"]["confirmed_paid_calls"])
        self.assertEqual(result["policies"]["match-resume"]["max_output_tokens"], 6144)
        self.assertEqual(result["policies"]["match-resume"]["reasoning_effort"], "medium")
        self.assertEqual(result["paid_requests"], 0)
        self.assertNotIn("offline-placeholder", json.dumps(result))
        self.assertEqual(result["max_batch_requests"], 6)
        self.assertEqual(result["max_batch_estimate_rmb"], result["estimate_per_job"]["projected_rmb"] * 2)


class LeverPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_page_without_target_jobs_does_not_hide_later_guangzhou_job(self):
        source = sources()
        seen = []
        def respond(request):
            seen.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx2.Response(200, headers={"content-type": "text/plain"}, text="User-agent: *\nAllow: /")
            rows = response_rows() if request.url.params.get("skip") == "100" else [
                {**response_rows()[0], "text": "行政岗位"} for _ in range(100)]
            return httpx2.Response(200, headers={"content-type": "application/json"}, text=json.dumps(rows))
        resolver = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        result = await search_public(source, PublicFetcher(source, allow_network=True,
            transport=httpx2.MockTransport(respond), resolver=resolver, delay=0), guangzhou_only=True)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0].city, "广州")
        self.assertEqual(len(seen), 3)
        self.assertIn("skip=100", seen[-1])


@unittest.skipUnless(os.environ.get("JOB_AGENT_BROWSER_TESTS") == "1", "需显式启用已安装的隔离浏览器测试")
class IntegratedBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        shutil.copytree(PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        self.limits = Limits(2, 12, 2, 2)
        self.config = LLMConfig("offline-placeholder", "https://api.example.invalid/v1", "gpt-6-sol", .3, 1.5, max_requests=12, budget_rmb=2)
        self.consent = LLMConsent.for_materials(RESUME, self.config, self.limits, "2099-01-01T00:00:00+00:00",
                     confirmed_data_transfer=True, confirmed_paid_calls=True)
        self.mock = MockResponsesClient()
        self.runtime = self.make_runtime()
        self.materials = JobRuntime(self.root, demo=True, state_dir=".agent-platform/integration", session_id="materials")
        self.facts = {"name": {"value": "人工候选人", "quote": "姓名：人工候选人", "confirmed": True},
                      "email": {"value": "candidate@example.invalid", "quote": "邮箱：candidate@example.invalid", "confirmed": True},
                      "availability": {"value": "6个月", "confirmed": True, "source": "user_answer", "job_id": 1},
                      "agree": {"value": "同意", "confirmed": True, "source": "user_answer", "job_id": 1}}

    def make_runtime(self):
        return AutomationRuntime(self.root, lambda *_: None, state_dir=".agent-platform/integration", limits=self.limits,
            llm_config=self.config, consent=self.consent, resume=RESUME,
            client_factory=self.mock.factory, simulation=True, clock=lambda: NOW)

    async def asyncTearDown(self):
        await self.materials.close()
        await self.runtime.close()
        self.directory.cleanup()

    async def search(self):
        source = sources()
        seen = []
        def respond(request):
            seen.append((request.method, str(request.url)))
            return httpx2.Response(200, headers={"content-type": "application/json"},
                text="User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else json.dumps(response_rows()))
        resolver = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        result = await search_public(source, PublicFetcher(source, allow_network=True, transport=httpx2.MockTransport(respond), resolver=resolver, delay=0), limit=5, guangzhou_only=True)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual([r[0] for r in seen], ["GET", "GET"])
        self.assertEqual(result["input_kind"], "mock_transport")
        self.assertFalse(result["source_checks"][0]["verified_real_search"])
        return result

    async def prepared(self, *, behavior="success", html=FORM):
        search = await self.search()
        # 人工fixture明确提供截止日期。真实Lever GET不提供期限时保留人工复核，不能自动通过。
        job = replace(search["candidates"][0], valid_through="2099-12-31T00:00:00+00:00")
        result = await check_candidates(self.runtime, [job], RESUME, guangzhou_only=True)
        row = result["discovery"]["ranked"][0]
        adapter = LeverSnapshotATS(self.runtime.automation, html=html, target=TARGET, extra_fields=EXTRAS, behavior=behavior)
        service = ApplicationService(self.runtime.automation, self.limits, adapter)
        prepared = await service.prepare(self.materials, row["job_id"], job, row["report"], RESUME, self.facts)
        return service, adapter, prepared, job, row["report"]

    async def test_get_mock_llm_native_form_sqlite_and_dedup_full_chain(self):
        search = await self.search()
        search["candidates"] = [replace(search["candidates"][0], valid_through="2099-12-31T00:00:00+00:00")]
        adapter = LeverSnapshotATS(self.runtime.automation, html=FORM, target=TARGET, extra_fields=EXTRAS)
        first = await workflow(self.runtime, search, RESUME, adapter=adapter, facts=self.facts, guangzhou_only=True)
        self.assertEqual(first["summary"]["applications"][0]["state"], "simulated_submitted")
        self.assertEqual(len(self.mock.calls), 3)
        self.assertTrue(adapter.last_audit["pre_submit_validated"])
        self.assertEqual(adapter.last_audit["external_submissions"], 0)
        self.assertEqual(adapter.backend.submit_calls, 1)
        job = self.runtime.workspace.memory.library.get(1)
        self.assertEqual(job["status"], "discovered")
        self.runtime.workspace.memory.library.add_note(1, "人工备注保留")
        second = await workflow(self.runtime, search, RESUME, adapter=adapter, facts=self.facts, guangzhou_only=True)
        self.assertEqual(second["summary"]["new_analyses"], 0)
        self.assertEqual(second["summary"]["applications"][0]["state"], "already_recorded")
        self.assertEqual(len(self.mock.calls), 3)
        self.assertEqual(len(self.runtime.workspace.memory.library.list()), 1)
        self.assertEqual(self.runtime.workspace.memory.library.get(1)["notes"], "人工备注保留")
        self.assertEqual(first["summary"]["real_api_requests"], 0)
        self.assertEqual(first["summary"]["source_input_kind"], "mock_transport")
        self.assertEqual(SourceHealthStore(self.runtime.workspace.memory).list(), [])

    async def test_missing_open_deadline_requires_review_not_fake_automatic_success(self):
        result = await check_candidates(self.runtime, (await self.search())["candidates"], RESUME)
        self.assertEqual(result["summary"]["jobs"][0]["decision"], "review")
        self.assertTrue(any("仍在招聘" in s for s in result["summary"]["jobs"][0]["review_items"]))

    async def test_schema_drift_unknown_question_captcha_pdf_and_wrong_target_stop(self):
        variants = [FORM.replace('name="email"', 'name="new_email"'),
                    FORM.replace("</form>", '<input name="unknown" required></form>'),
                    FORM.replace("</form>", '<div class="g-recaptcha"></div></form>'),
                    FORM.replace('accept=".md"', 'accept=".pdf"'),
                    FORM.replace(POSTING + "/apply", "22222222-2222-4222-8222-222222222222/apply")]
        for html in variants:
            with self.subTest(html=html[:25]), self.assertRaises(GuardError):
                LeverSnapshotATS(self.runtime.automation, html=html, target=TARGET, extra_fields=EXTRAS)

    async def test_untrusted_page_scripts_cannot_replace_confirmed_data_or_contact_sites(self):
        html = FORM + '<script>document.querySelector("[name=name]").value="forged";fetch("https://outside.example.invalid/")</script>'
        service, adapter, packet, job, report = await self.prepared(html=html)
        self.assertEqual((await service.submit(packet, job, report, RESUME))["state"], "simulated_submitted")
        self.assertEqual(adapter.last_audit["mock_submit_requests"], 1)

    async def test_resume_mismatch_and_wrong_job_block_before_browser(self):
        service, adapter, packet, job, report = await self.prepared()
        with self.assertRaises(GuardError):
            await service.submit(packet, job, report, RESUME + "未经重新核验的新资料")
        with self.assertRaises(GuardError):
            await service.submit(packet, replace(job, title="其他岗位"), report, RESUME)
        self.assertEqual(adapter.backend.submit_calls, 0)

    async def test_timeout_recovery_after_runtime_restart_does_not_resubmit(self):
        service, adapter, packet, job, report = await self.prepared(behavior="accepted_timeout")
        attempt = await service.submit(packet, job, report, RESUME)
        self.assertEqual(attempt["state"], "unknown")
        await self.runtime.close()
        self.runtime = self.make_runtime()
        recovered_adapter = LeverSnapshotATS(self.runtime.automation, html=FORM, target=TARGET, extra_fields=EXTRAS)
        restored = await ApplicationService(self.runtime.automation, self.limits, recovered_adapter).reconcile(attempt["id"])
        self.assertEqual(restored["state"], "simulated_submitted")
        self.assertEqual(adapter.backend.submit_calls, 1)
        self.assertEqual(recovered_adapter.backend.submit_calls, 0)

    async def test_network_failure_and_emergency_stop_do_not_resend(self):
        service, adapter, packet, job, report = await self.prepared(behavior="timeout")
        attempt = await service.submit(packet, job, report, RESUME)
        self.assertEqual(attempt["state"], "unknown")
        self.assertEqual((await service.reconcile(attempt["id"]))["state"], "unknown")
        self.runtime.automation.stop()
        with self.assertRaises(GuardError):
            await service.reconcile(attempt["id"])
        self.assertEqual(adapter.backend.submit_calls, 1)

    async def test_ineligible_graduation_and_formal_job_never_call_llm_or_browser(self):
        job = (await self.search())["candidates"][0]
        jobs = [replace(job, jd=job.jd.replace("2027", "2026")),
                replace(job, title="AI Agent校招工程师", recruitment_type="校招", url=TARGET + "?different=1")]
        result = await check_candidates(self.runtime, jobs, RESUME)
        self.assertEqual(result["summary"]["local_skipped"], 2)
        self.assertEqual(len(self.mock.calls), 0)

    async def test_llm_failure_preserves_review_and_no_paid_retry(self):
        self.mock.failure = "timeout"
        job = (await self.search())["candidates"][0]
        first = await check_candidates(self.runtime, [job], RESUME)
        second = await check_candidates(self.runtime, [job], RESUME)
        self.assertEqual(len(self.mock.calls), 1)
        self.assertEqual(second["summary"]["new_analyses"], 0)
        self.assertTrue(first["summary"]["pending"] or any(row["decision"] == "review" for row in first["summary"]["jobs"]))

    async def test_daily_budget_blocks_llm_before_request_without_lowering_quality(self):
        self.runtime.limits = replace(self.limits, daily_rmb=.01)
        self.runtime.llm.limits = self.runtime.limits
        self.runtime.llm.consent = LLMConsent.for_materials(RESUME, self.config, self.runtime.limits, "2099-01-01T00:00:00+00:00",
                confirmed_data_transfer=True, confirmed_paid_calls=True)
        result = await check_candidates(self.runtime, (await self.search())["candidates"], RESUME)
        self.assertEqual(len(self.mock.calls), 0)
        self.assertEqual(result["summary"]["pending"], 1)
        self.assertEqual(self.config.policy("match-resume")["max_output_tokens"], 6144)

    async def test_same_cli_can_run_mock_llm_with_browser_then_reuse_database(self):
        from job_agent.automation import __main__ as cli
        args = cli.parser().parse_args(["demo", "--mode", "mock-llm", "--application-mode", "browser-mock",
                                        "--state-dir", ".agent-platform/cli-browser"])
        with patch.object(cli, "PROJECT", self.root):
            first = await cli.run(args)
            second = await cli.run(args)
        self.assertEqual(first["applications"][0]["state"], "simulated_submitted")
        self.assertEqual(second["applications"][0]["state"], "already_recorded")
        self.assertEqual(second["new_analyses"], 0)
        self.assertEqual(first["paid_api_calls"], 0)


if __name__ == "__main__":
    unittest.main()
