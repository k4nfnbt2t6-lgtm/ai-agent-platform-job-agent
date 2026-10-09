"""可选真实Chromium离线端到端测试；无第三方数据传输、真实申请或付费。"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from agent_platform.terminal_ui import PROJECT
from job_agent.automation.applications import ApplicationService
from job_agent.automation.engine import AutomationRuntime, check_candidates
from job_agent.automation.mock import RESUME, candidates
from job_agent.automation.policy import Limits
from job_agent.automation.store import GuardError
from job_agent.automation.workflow import workflow
from job_agent.browser.adapter import OfflineBrowserATS
from job_agent.browser.offline import offline_page, render_snapshot, fetch_rendered_source
from job_agent.browser.__main__ import synthetic_page
from job_agent.search import parse_public_page
from job_agent.runtime import JobRuntime
from job_agent.update_store import CHINA


class BrowserBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_source_rejected_before_browser_launch(self):
        with self.assertRaises(ValueError):
            await render_snapshot("<p>test</p>", {}, "https://127.0.0.1/")

    async def test_large_snapshot_rejected_before_browser_launch(self):
        with self.assertRaises(ValueError):
            async with offline_page("a" * 1_000_001):
                self.fail("must not launch")

    async def test_non_text_snapshot_rejected(self):
        with self.assertRaises(ValueError):
            async with offline_page(b"test"):
                self.fail("must not launch")

    async def test_browser_adapter_not_an_arbitrary_plugin_registration(self):
        class UnknownAdapter:
            name = "real-ats"
        with self.assertRaises(PermissionError):
            ApplicationService(None, Limits(), UnknownAdapter())


@unittest.skipUnless(os.environ.get("JOB_AGENT_BROWSER_TESTS") == "1", "可选Chromium验收需JOB_AGENT_BROWSER_TESTS=1")
class BrowserE2ETests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        shutil.copytree(PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        self.now = datetime(2026, 10, 10, 8, tzinfo=CHINA)
        self.limits = Limits()
        self.runtime = AutomationRuntime(self.root, lambda *_: None, state_dir=".agent-platform/browser-test", limits=self.limits,
                                         clock=lambda: self.now)
        self.materials = JobRuntime(self.root, demo=True, state_dir=".agent-platform/browser-test", session_id="materials")
        self.facts = {"name": {"value": "人工候选人", "quote": "姓名：人工候选人", "confirmed": True},
                      "email": {"value": "candidate@example.invalid", "quote": "邮箱：candidate@example.invalid", "confirmed": True}}

    async def asyncTearDown(self):
        await self.materials.close()
        await self.runtime.close()
        self.directory.cleanup()

    def adapter(self, **kwargs):
        adapter = OfflineBrowserATS(self.runtime.automation, **kwargs)
        return adapter, ApplicationService(self.runtime.automation, self.limits, adapter)

    async def prepared(self, service, facts=None):
        job = candidates(self.now)[0]
        result = await check_candidates(self.runtime, [job], RESUME, guangzhou_only=True)
        row = result["discovery"]["ranked"][0]
        prepared = await service.prepare(self.materials, row["job_id"], job, row["report"], RESUME, facts or self.facts)
        return prepared, job, row["report"]

    async def test_dynamic_json_ld_rendered_then_existing_parser_reused(self):
        url = "https://careers.example.invalid/jobs"
        source = {"name": "人工招聘", "company": "人工科技", "url": url}
        html = synthetic_page(self.now)
        self.assertEqual(parse_public_page(html, source, url)[0], [])
        parsed = await render_snapshot(html, source, url, ready_selector='#jobs[data-ready="true"]')
        self.assertEqual(len(parsed["jobs"]), 1)
        self.assertEqual(parsed["jobs"][0].city, "广州")
        self.assertEqual(parsed["jobs"][0].parsing_evidence["method"], "json_ld_jobposting")
        self.assertEqual(parsed["rendering"]["source_url"], url)
        self.assertNotEqual(parsed["rendering"]["snapshot_sha256"], parsed["rendering"]["rendered_sha256"])

    async def test_public_fetcher_transport_and_robots_still_guard_rendering(self):
        import httpx2
        import socket
        from job_agent.search import PublicFetcher
        seen = []
        source = {"name": "人工来源", "kind": "html", "url": "https://careers.example.invalid/jobs"}

        def response(request):
            seen.append(str(request.url))
            return httpx2.Response(200, headers={"content-type": "text/html"},
                                   text="User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else synthetic_page(self.now))

        resolver = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        fetcher = PublicFetcher([source], allow_network=True, transport=httpx2.MockTransport(response), resolver=resolver, delay=0)
        parsed = await fetch_rendered_source(fetcher, source)
        self.assertEqual(len(parsed["jobs"]), 1)
        self.assertEqual(seen, ["https://careers.example.invalid/robots.txt", source["url"]])
        denied = PublicFetcher([source], allow_network=True,
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200, headers={"content-type": "text/plain"}, text="User-agent: *\nDisallow: /")),
            resolver=resolver, delay=0)
        with self.assertRaisesRegex(ValueError, "robots"):
            await fetch_rendered_source(denied, source)
        self.assertEqual(denied.requests, 1)

    async def test_dynamic_cards_keep_city_and_detail_link_evidence(self):
        html = '''<div id="cards"></div><script>document.querySelector('#cards').innerHTML=`
        <article class="job-card"><h2>AI应用开发实习生</h2><a class="detail-link" href="/jobs/42">详情</a>
        <p>工作地点：广州</p><p>岗位职责：开发Agent工具</p><p>任职要求：2027届本科，Python基础</p></article>`;</script>'''
        source = {"name": "人工深圳网站", "company": "人工广州科技", "url": "https://careers.example.invalid/"}
        parsed = await render_snapshot(html, source, source["url"], ready_selector=".job-card")
        self.assertEqual(len(parsed["jobs"]), 1)
        self.assertEqual(parsed["jobs"][0].city, "广州")
        self.assertEqual(parsed["jobs"][0].detail_url, "https://careers.example.invalid/jobs/42")
        self.assertEqual(parsed["jobs"][0].parsing_evidence["method"], "html_card")

    async def test_external_scripts_fetch_iframe_and_websocket_cannot_send(self):
        html = '''<script src="https://outside.example.invalid/script"></script>
        <iframe src="https://outside.example.invalid/frame"></iframe><script>
        window.wsClosed=false;
        fetch('https://outside.example.invalid/collect', {method:'POST', body:'synthetic-only'}).catch(()=>{});
        const ws=new WebSocket('wss://outside.example.invalid/socket');ws.onclose=()=>window.wsClosed=true;
        </script>'''
        async with offline_page(html) as (page, blocked):
            await page.wait_for_function("window.wsClosed === true")
            self.assertGreaterEqual(len(blocked), 3)
            self.assertFalse(await page.evaluate("'serviceWorker' in navigator && navigator.serviceWorker.controller !== null"))

    async def test_external_spa_and_login_page_explicitly_fail_no_guess(self):
        parsed = await render_snapshot('<h1>请登录或完成验证码</h1><script src="https://outside.example.invalid/app.js"></script>',
                 {"name": "人工来源"}, "https://careers.example.invalid/")
        self.assertEqual(parsed["jobs"], [])
        self.assertTrue(any("未解析到岗位" in issue for issue in parsed["issues"]))
        self.assertEqual(parsed["rendering"]["blocked_requests"], 1)

    async def test_real_browser_form_text_select_checkbox_and_memory_file(self):
        adapter, service = self.adapter(require_availability=True, require_consent=True)
        result = await check_candidates(self.runtime, [candidates(self.now)[0]], RESUME)
        row = result["discovery"]["ranked"][0]
        for key, value in (("availability", "6个月"), ("agree", "同意")):
            self.facts[key] = {"value": value, "confirmed": True, "source": "user_answer", "job_id": row["job_id"]}
        prepared, job, report = await self.prepared(service)
        attempt = await service.submit(prepared, job, report, RESUME)
        self.assertEqual(attempt["state"], "simulated_submitted")
        self.assertEqual(adapter.last_audit["mock_submit_requests"], 1)
        self.assertTrue(adapter.last_audit["file_uploaded"])
        self.assertEqual(self.runtime.workspace.memory.library.get(row["job_id"])["status"], "discovered")
        with self.assertRaises(GuardError):
            await service.submit(prepared, job, report, RESUME)
        self.assertEqual(adapter.backend.submit_calls, 1)

    async def test_unconfirmed_question_blocks_browser_before_launch(self):
        adapter, service = self.adapter(require_availability=True)
        prepared, job, report = await self.prepared(service)
        self.assertEqual(prepared["state"], "review_required")
        with self.assertRaises(GuardError):
            await service.submit(prepared, job, report, RESUME)
        self.assertEqual(adapter.backend.submit_calls, 0)

    async def test_accepted_timeout_restores_via_original_receipt_without_resend(self):
        adapter, service = self.adapter(behavior="accepted_timeout")
        prepared, job, report = await self.prepared(service)
        attempt = await service.submit(prepared, job, report, RESUME)
        self.assertEqual(attempt["state"], "unknown")
        new_adapter, new_service = self.adapter()
        recovered = await new_service.reconcile(attempt["id"])
        self.assertEqual(recovered["state"], "simulated_submitted")
        self.assertEqual(adapter.backend.submit_calls, 1)
        self.assertEqual(new_adapter.backend.submit_calls, 0)

    async def test_network_failure_stays_unknown_and_stop_blocks_recovery(self):
        adapter, service = self.adapter(behavior="timeout")
        prepared, job, report = await self.prepared(service)
        attempt = await service.submit(prepared, job, report, RESUME)
        self.assertEqual(attempt["state"], "unknown")
        self.assertEqual((await service.reconcile(attempt["id"]))["state"], "unknown")
        self.runtime.automation.stop()
        with self.assertRaises(GuardError):
            await service.reconcile(attempt["id"])
        self.assertEqual(adapter.backend.submit_calls, 1)

    async def test_explicit_platform_rejection_is_failed_not_unknown(self):
        adapter, service = self.adapter(behavior="reject")
        prepared, job, report = await self.prepared(service)
        attempt = await service.submit(prepared, job, report, RESUME)
        self.assertEqual(attempt["state"], "failed")
        self.assertEqual(adapter.backend.submit_calls, 1)

    async def test_wrong_receipt_does_not_count_as_success(self):
        adapter, service = self.adapter(behavior="wrong_receipt")
        prepared, job, report = await self.prepared(service)
        attempt = await service.submit(prepared, job, report, RESUME)
        self.assertEqual(attempt["state"], "unknown")

    async def test_tampered_packet_is_blocked_before_browser_request(self):
        adapter, service = self.adapter()
        prepared, job, report = await self.prepared(service)
        prepared["packet"]["fields"]["name"] = "未经确认的姓名"
        with self.assertRaises(GuardError):
            await service.submit(prepared, job, report, RESUME)
        self.assertEqual(adapter.backend.submit_calls, 0)

    async def test_end_to_end_render_analyze_prepare_form_store_then_dedup(self):
        source = {"name": "人工来源", "url": "https://careers.example.invalid/"}
        parsed = await render_snapshot(synthetic_page(self.now), source, source["url"])
        search = {"candidates": parsed["jobs"], "issues": parsed["issues"], "http_requests": 0}
        adapter, _ = self.adapter()
        first = await workflow(self.runtime, search, RESUME, adapter=adapter, facts=self.facts, guangzhou_only=True)
        self.assertEqual(first["summary"]["applications"][0]["state"], "simulated_submitted")
        second = await workflow(self.runtime, search, RESUME, adapter=adapter, facts=self.facts, guangzhou_only=True)
        self.assertEqual(second["summary"]["applications"][0]["state"], "already_recorded")
        self.assertEqual(adapter.backend.submit_calls, 1)
        self.assertEqual(second["summary"]["real_api_requests"], 0)


if __name__ == "__main__":
    unittest.main()
