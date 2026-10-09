"""自动化离线验收：真实SDK工具循环/SQLite，禁止网络、付费与真实申请。"""

import asyncio
import contextlib
import copy
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock

from agent_platform.terminal_ui import PROJECT
from job_agent.analysis import analyze_jd, fingerprint, match_resume
from job_agent.memory import JobMemory
from job_agent.runtime import JobRuntime
from job_agent.update_store import CHINA
from job_agent.automation.adapters import Field, ManualHandoff, MockATS, Receipt
from job_agent.automation.applications import ApplicationService
from job_agent.automation.decisions import decide
from job_agent.automation.engine import AutomationRuntime, AutomationUpdates, check_candidates
from job_agent.automation.mock import RESUME, MockResponsesClient, candidates
from job_agent.automation.policy import LLMConsent, Limits, digest
from job_agent.automation.store import AutomationStore, GuardError, micro
from job_agent.llm import LLMConfig


class Base(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        shutil.copytree(PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        self.now = datetime(2026, 10, 10, 8, tzinfo=CHINA)
        self.clock = lambda: self.now
        self.limits = Limits(2, 12, 2, 2)
        self.config = LLMConfig("offline-placeholder", "https://api.example.invalid/v1", "gpt-6-sol", .3, 1.5, max_requests=12, budget_rmb=2)
        self.consent = LLMConsent.for_materials(RESUME, self.config, self.limits, "2099-01-01T00:00:00+00:00",
                    confirmed_data_transfer=True, confirmed_paid_calls=True)
        for name in ("connect", "connect_ex"):
            guard = patch.object(socket.socket, name, side_effect=AssertionError("本测试禁止真实网络"))
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.dict(os.environ, {}, clear=True)
        guard.start()
        self.addCleanup(guard.stop)
        self.memory = JobMemory(self.root / "ledger.sqlite")
        self.addCleanup(self.memory.close)
        self.store = AutomationStore(self.memory, self.clock)


class PolicyBudgetTests(Base):
    def test_defaults_cannot_enable_paid_analysis(self):
        with self.assertRaises(PermissionError):
            self.consent.check(RESUME, self.config, Limits(), self.now)

    def test_consent_binds_materials_endpoint_model_prices_limits_and_expiry(self):
        cases = [(RESUME + "新内容", self.config, self.limits),
                 (RESUME, replace(self.config, base_url="https://different.example.invalid/v1"), self.limits),
                 (RESUME, replace(self.config, model="other"), self.limits),
                 (RESUME, replace(self.config, output_price=2), self.limits),
                 (RESUME, self.config, replace(self.limits, daily_rmb=3))]
        for resume, config, limits in cases:
            with self.subTest(config=config.model), self.assertRaises(PermissionError):
                self.consent.check(resume, config, limits, self.now)
        with self.assertRaises(PermissionError):
            replace(self.consent, expires_at=self.now.isoformat()).check(RESUME, self.config, self.limits, self.now)

    def test_data_transfer_and_payment_are_separate_opt_ins(self):
        for field in ("confirmed_paid_calls", "confirmed_data_transfer"):
            with self.subTest(field=field), self.assertRaises(PermissionError):
                replace(self.consent, **{field: False}).check(RESUME, self.config, self.limits, self.now)

    def test_invalid_limits_and_fractional_requests_rejected(self):
        for kwargs in ({"daily_rmb": float("nan")}, {"daily_rmb": -1}, {"daily_requests": 1.5}, {"daily_requests": True}, {"jobs_per_run": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Limits(**kwargs)

    def test_micro_currency_rounds_up_not_down(self):
        self.assertEqual(micro(.0000001), 1)

    def test_full_plan_exceeding_budget_is_rejected_before_request(self):
        with self.assertRaises(GuardError):
            self.store.reserve_plan("one", 2.1, 3, self.limits)
        self.assertEqual(self.store.status()["reserved_or_used_requests"], 0)

    def test_request_quota_counts_reserved_plan_not_only_success(self):
        self.store.reserve_plan("first", .1, 9, self.limits)
        with self.assertRaises(GuardError):
            self.store.reserve_plan("second", .1, 4, self.limits)

    def test_duplicate_work_key_survives_restart(self):
        self.store.reserve_plan("same", .1, 3, self.limits)
        other = JobMemory(self.root / "ledger.sqlite")
        try:
            with self.assertRaises(GuardError):
                AutomationStore(other, self.clock).reserve_plan("same", .1, 3, self.limits)
        finally:
            other.close()

    def test_atomic_budget_across_connections(self):
        self.store.reserve_plan("first", 1.2, 3, self.limits)
        other = JobMemory(self.root / "ledger.sqlite")
        try:
            with self.assertRaises(GuardError):
                AutomationStore(other, self.clock).reserve_plan("second", 1.2, 3, self.limits)
        finally:
            other.close()

    def test_known_cost_settles_and_unused_capacity_is_released(self):
        plan = self.store.reserve_plan("one", .5, 3, self.limits)
        request = self.store.begin_request(plan, .1, self.limits)
        self.store.settle_request(request, input_tokens=10, output_tokens=20, cost=.001)
        self.store.finish_plan(plan, True)
        self.assertEqual(self.store.status()["reserved_or_spent_rmb"], .001)
        self.assertEqual(self.store.status()["reserved_or_used_requests"], 1)
        with self.assertRaises(GuardError):
            self.store.settle_request(request, input_tokens=0, output_tokens=0, cost=0)

    def test_unknown_cost_blocks_new_calls_even_next_day(self):
        plan = self.store.reserve_plan("one", .5, 3, self.limits)
        request = self.store.begin_request(plan, .1, self.limits)
        self.store.settle_request(request, input_tokens=0, output_tokens=0, cost=None)
        self.store.finish_plan(plan, False)
        self.now += timedelta(days=1)
        with self.assertRaises(GuardError):
            self.store.reserve_plan("two", .5, 3, self.limits)

    def test_crash_inflight_request_is_not_implicitly_retried(self):
        plan = self.store.reserve_plan("one", .5, 3, self.limits)
        self.store.begin_request(plan, .1, self.limits)
        self.store.finish_plan(plan, False)
        self.assertEqual(self.store.status()["unresolved_requests"], 1)
        with self.assertRaises(GuardError):
            self.store.reserve_plan("two", .5, 3, self.limits)

    def test_midnight_plan_does_not_reuse_previous_day_allowance(self):
        plan = self.store.reserve_plan("one", .5, 3, self.limits)
        self.now += timedelta(days=1)
        with self.assertRaises(GuardError):
            self.store.begin_request(plan, .1, self.limits)

    def test_abrupt_crash_without_finally_blocks_new_plan(self):
        plan = self.store.reserve_plan("one", .5, 3, self.limits)
        self.store.begin_request(plan, .1, self.limits)
        self.now += timedelta(days=1)
        with self.assertRaises(GuardError):
            AutomationStore(self.memory, self.clock).reserve_plan("two", .5, 3, self.limits)

    def test_provider_exceeding_estimate_trips_emergency_stop(self):
        plan = self.store.reserve_plan("one", .1, 1, self.limits)
        request = self.store.begin_request(plan, .1, self.limits)
        self.store.settle_request(request, input_tokens=100, output_tokens=100, cost=.2)
        self.assertTrue(self.store.stopped())

    def test_stop_survives_restart_and_resume_needs_confirmation(self):
        self.store.stop()
        store = AutomationStore(self.memory, self.clock)
        self.assertTrue(store.stopped())
        with self.assertRaises(GuardError):
            store.resume()
        with self.assertRaises(GuardError):
            store.reserve_plan("one", .1, 1, self.limits)
        store.resume(confirmed=True)
        self.assertFalse(store.stopped())


class DecisionTests(Base):
    def decision(self, **changes):
        job = replace(candidates(self.now)[0], **changes)
        jd = analyze_jd(job.analysis_text())
        report = {"jd_analysis": {"analysis": jd}, "resume_match": {"matching": match_resume(jd, RESUME)}}
        return decide(job, report, {"status": "discovered"}, now=self.now)

    def test_true_2027_bachelor_internship_is_eligible(self):
        self.assertEqual(self.decision()["action"], "eligible")

    def test_campus_role_is_not_internship_even_high_matching(self):
        decision = self.decision(recruitment_type="校招")
        self.assertEqual(decision["action"], "skip")

    def test_unknown_graduation_is_human_review(self):
        jd = candidates(self.now)[0].jd.replace("2027届", "")
        self.assertEqual(self.decision(jd=jd)["action"], "review")

    def test_expired_role_skipped(self):
        self.assertEqual(self.decision(valid_through="2020-01-01T00:00:00+08:00")["action"], "skip")

    def test_absent_open_status_is_not_invented(self):
        self.assertEqual(self.decision(valid_through=None)["action"], "review")

    def test_other_city_excluded_even_source_is_guangzhou(self):
        job = candidates(self.now)[0]
        self.assertEqual(self.decision(jd=job.jd.replace("广州", "北京"), city="北京")["action"], "skip")

    def test_actual_jd_location_overrides_source_city(self):
        self.assertEqual(self.decision(city="深圳")["city"]["city"], "广州")

    def test_degree_requirement_cannot_be_bypassed_by_score(self):
        job = candidates(self.now)[0]
        decision = self.decision(jd=job.jd.replace("本科在读", "博士在读"))
        self.assertNotEqual(decision["action"], "eligible")

    def test_compulsory_major_is_not_treated_as_preference(self):
        job = candidates(self.now)[0]
        decision = self.decision(jd=job.jd.replace("专业不限", "必须计算机专业"))
        self.assertNotEqual(decision["action"], "eligible")


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        shutil.copytree(PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        self.now = datetime(2026, 10, 10, 8, tzinfo=CHINA)
        self.clock = lambda: self.now
        self.limits = Limits(2, 12, 2, 2)
        self.config = LLMConfig("offline-placeholder", "https://api.example.invalid/v1", "gpt-6-sol", .3, 1.5, max_requests=12, budget_rmb=2)
        self.consent = LLMConsent.for_materials(RESUME, self.config, self.limits, "2099-01-01T00:00:00+00:00",
                    confirmed_data_transfer=True, confirmed_paid_calls=True)
        self.guards = []
        for name in ("connect", "connect_ex"):
            guard = patch.object(socket.socket, name, side_effect=AssertionError("禁止真实网络"))
            guard.start()
            self.guards.append(guard)
        guard = patch.dict(os.environ, {}, clear=True)
        guard.start()
        self.guards.append(guard)
        self.client = MockResponsesClient()
        self.runtime = self.new_runtime()
        self.materials = JobRuntime(self.root, demo=True, state_dir=".agent-platform/automation-test", session_id="materials")
        self.adapter = MockATS(self.runtime.automation)
        self.service = ApplicationService(self.runtime.automation, self.limits, self.adapter)
        self.facts = {"name": {"value": "人工候选人", "quote": "姓名：人工候选人", "confirmed": True},
                      "email": {"value": "candidate@example.invalid", "quote": "邮箱：candidate@example.invalid", "confirmed": True}}

    def new_runtime(self, **changes):
        options = dict(limits=self.limits, llm_config=self.config, consent=self.consent, resume=RESUME,
                       client_factory=self.client.factory, clock=self.clock, simulation=True)
        options.update(changes)
        return AutomationRuntime(self.root, lambda *_: None, state_dir=".agent-platform/automation-test", **options)

    async def asyncTearDown(self):
        await self.materials.close()
        await self.runtime.close()
        for guard in self.guards:
            guard.stop()
        self.directory.cleanup()

    async def analyze(self, jobs=None):
        return await check_candidates(self.runtime, jobs or candidates(self.now), RESUME)

    async def packet(self):
        result = await self.analyze()
        row = result["discovery"]["ranked"][0]
        from job_agent.search import JobCandidate
        job = JobCandidate(**row["candidate"])
        prepared = await self.service.prepare(self.materials, row["job_id"], job, row["report"], RESUME, self.facts)
        return prepared, job, row["report"]

    async def test_full_mock_chain_uses_three_real_skill_tools_and_structured_guards(self):
        prepared, job, report = await self.packet()
        self.assertEqual(len(self.client.calls), 3)
        self.assertTrue(report["mock_checks_complete"])
        self.assertFalse(report["semantic_complete"])
        self.assertEqual(prepared["state"], "ready")
        self.assertEqual(self.client.calls[1]["reasoning"]["effort"], "medium")
        self.assertGreaterEqual(self.client.calls[1]["max_output_tokens"], 6144)
        self.assertTrue(all(c["text"]["format"]["strict"] for c in self.client.calls))
        result = await self.service.submit(prepared, job, report, RESUME)
        self.assertEqual(result["state"], "simulated_submitted")
        self.assertEqual(self.runtime.workspace.memory.library.get(result["job_id"])["status"], "discovered")
        self.assertEqual(self.runtime.automation.status()["reserved_or_used_requests"], 3)

    async def test_duplicate_unchanged_jobs_do_not_repeat_llm_calls(self):
        await self.analyze()
        second = await self.analyze()
        self.assertEqual(second["summary"]["new_analyses"], 0)
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(len(self.runtime.workspace.memory.library.list()), 2)

    async def test_restart_reuses_persisted_analysis(self):
        await self.analyze()
        await self.runtime.close()
        self.runtime = self.new_runtime()
        result = await self.analyze()
        self.assertEqual(result["summary"]["new_analyses"], 0)
        self.assertEqual(len(self.client.calls), 3)

    async def test_mock_cache_cannot_masquerade_as_real_llm_cache(self):
        await self.analyze()
        self.assertEqual(len(self.client.calls), 3)
        await self.runtime.close()
        # 仍使用离线客户端，模拟开启真实模式；绝不访问公网。
        self.runtime = self.new_runtime(simulation=False)
        result = await self.analyze()
        self.assertEqual(result["summary"]["new_analyses"], 1)
        self.assertEqual(len(self.client.calls), 6)
        self.assertEqual(result["discovery"]["ranked"][0]["report"]["mode"], "llm")

    async def test_changed_jd_is_reanalyzed_without_losing_id_notes(self):
        first = await self.analyze()
        identity = first["discovery"]["ranked"][0]["job_id"]
        library = self.runtime.workspace.memory.library
        library.add_note(identity, "人工保留备注")
        library.set_status(identity, "shortlisted")
        changed = replace(candidates(self.now)[0], jd=candidates(self.now)[0].jd.replace("Python、Tool Calling", "Python、Tool Calling、RAG"))
        result = await self.analyze([changed])
        self.assertEqual(result["summary"]["new_analyses"], 1)
        self.assertEqual(result["discovery"]["ranked"][0]["job_id"], identity)
        self.assertIn("人工保留备注", library.get(identity)["notes"])
        self.assertEqual(library.get(identity)["status"], "shortlisted")
        self.assertEqual(len(self.client.calls), 6)

    async def test_applied_role_skipped_before_paid_analysis_even_jd_changes(self):
        first = await self.analyze()
        identity = first["discovery"]["ranked"][0]["job_id"]
        self.runtime.workspace.memory.library.set_status(identity, "applied")
        job = candidates(self.now)[0]
        result = await self.analyze([replace(job, jd=job.jd + "\n必须掌握RAG")])
        self.assertEqual(result["summary"]["new_analyses"], 0)
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(self.runtime.workspace.memory.library.get(identity)["status"], "applied")

    async def test_unknown_cost_falls_back_with_review_not_fake_semantic_success(self):
        self.client.failure = "timeout"
        result = await self.analyze()
        self.assertEqual(len(self.client.calls), 1)
        row = result["discovery"]["ranked"][0]
        self.assertTrue(row["report"]["quality_degraded"])
        self.assertFalse(row["report"]["semantic_complete"])
        self.assertEqual(row["decision"]["action"], "review")
        await self.analyze()
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.runtime.automation.status()["unresolved_requests"], 1)

    async def test_invalid_schema_is_not_retried_and_known_tokens_recorded(self):
        self.client.failure = "schema"
        result = await self.analyze()
        self.assertEqual(len(self.client.calls), 1)
        self.assertTrue(result["discovery"]["ranked"][0]["report"]["quality_degraded"])
        self.assertEqual(self.runtime.automation.status()["unresolved_requests"], 0)

    async def test_budget_shortage_does_not_lock_unattempted_job_forever(self):
        # 使用全部计划槽位；预检应阻止发送且不声明岗位已经分析。
        self.runtime.automation.reserve_plan("unrelated", .1, 12, self.limits)
        result = await self.analyze()
        self.assertEqual(len(self.client.calls), 0)
        self.assertEqual(result["summary"]["pending"], 1)
        self.now += timedelta(days=1)
        result = await self.analyze()
        self.assertEqual(result["summary"]["new_analyses"], 1)
        self.assertEqual(len(self.client.calls), 3)

    async def test_resume_cannot_change_after_authorization(self):
        with self.assertRaises(PermissionError):
            await check_candidates(self.runtime, candidates(self.now), RESUME + "新项目")
        self.assertEqual(len(self.client.calls), 0)

    async def test_emergency_stop_blocks_analysis_and_application(self):
        prepared, job, report = await self.packet()
        self.runtime.automation.stop()
        with self.assertRaises(GuardError):
            await self.analyze()
        with self.assertRaises(GuardError):
            await self.service.submit(prepared, job, report, RESUME)
        self.assertEqual(self.adapter.submit_calls, 0)

    async def test_duplicate_application_is_blocked(self):
        prepared, job, report = await self.packet()
        await self.service.submit(prepared, job, report, RESUME)
        with self.assertRaises(GuardError):
            await self.service.submit(prepared, job, report, RESUME)
        self.assertEqual(self.adapter.submit_calls, 1)

    async def test_accepted_then_timeout_reconciles_without_resubmitting_after_restart(self):
        prepared, job, report = await self.packet()
        self.adapter.behavior = "accepted_timeout"
        result = await self.service.submit(prepared, job, report, RESUME)
        self.assertEqual(result["state"], "unknown")
        restored = ApplicationService(self.runtime.automation, self.limits, MockATS(self.runtime.automation))
        result = await restored.reconcile(result["id"])
        self.assertEqual(result["state"], "simulated_submitted")
        self.assertEqual(restored.adapter.submit_calls, 0)
        self.assertEqual(self.adapter.submit_calls, 1)

    async def test_timeout_without_receipt_stays_unknown_no_retry(self):
        prepared, job, report = await self.packet()
        self.adapter.behavior = "timeout"
        result = await self.service.submit(prepared, job, report, RESUME)
        result = await self.service.reconcile(result["id"])
        self.assertEqual(result["state"], "unknown")
        with self.assertRaises(GuardError):
            await self.service.submit(prepared, job, report, RESUME)
        self.assertEqual(self.adapter.submit_calls, 1)

    async def test_wrong_target_receipt_is_not_accepted(self):
        prepared, job, report = await self.packet()
        self.adapter.behavior = "wrong_receipt"
        result = await self.service.submit(prepared, job, report, RESUME)
        self.assertEqual(result["state"], "unknown")

    async def test_explicit_platform_rejection_is_failed_not_unknown(self):
        prepared, job, report = await self.packet()
        self.adapter.behavior = "reject"
        result = await self.service.submit(prepared, job, report, RESUME)
        self.assertEqual(result["state"], "failed")
        with self.assertRaises(GuardError):
            await self.service.submit(prepared, job, report, RESUME)

    async def test_changed_graduation_in_real_evidence_cannot_be_upgraded_by_model(self):
        prepared, job, report = await self.packet()
        # 即使模型匹配行宣称matched，也要独立核查本人届别。
        decision = decide(job, report, {"status": "discovered"}, now=self.now, resume=RESUME.replace("2027届", "2026届"))
        self.assertEqual(decision["action"], "skip")

    async def test_private_automation_directory_is_required(self):
        with self.assertRaises(ValueError):
            AutomationRuntime(self.root, state_dir="public-state", limits=self.limits)
        self.assertFalse((self.root / "public-state").exists())

    async def test_workflow_persists_complete_summary_and_duplicate_application(self):
        from job_agent.automation.workflow import workflow
        search = {"candidates": candidates(self.now), "issues": [], "http_requests": 0}
        first = await workflow(self.runtime, search, RESUME, adapter=self.adapter, facts=self.facts)
        self.assertEqual(first["summary"]["applications"][0]["state"], "simulated_submitted")
        second = await workflow(self.runtime, search, RESUME, adapter=self.adapter, facts=self.facts)
        self.assertEqual(second["summary"]["applications"][0]["state"], "already_recorded")
        self.assertEqual(second["summary"]["new_analyses"], 0)
        self.assertEqual(self.runtime.automation.status()["last_run"]["summary"]["applications"][0]["state"], "already_recorded")
        path = self.runtime.workspace.root / second["summary"]["report_files"]["markdown"]
        self.assertIn("真实API请求：0", path.read_text())
        self.assertNotIn("candidate@example.invalid", path.read_text())
        self.assertEqual(self.adapter.submit_calls, 1)

    async def test_actual_jsonld_search_to_application_chain_without_network(self):
        from job_agent.search import search_public
        from job_agent.automation.workflow import workflow, candidate_available
        job = candidates(self.now)[0]
        posting = {"@type": "JobPosting", "title": job.title, "hiringOrganization": {"name": job.company},
                   "description": job.jd.replace("\n", "<br>"), "jobLocation": {"address": {"addressLocality": "广州"}},
                   "employmentType": "INTERN", "validThrough": job.valid_through, "url": job.url}
        body = '<script type="application/ld+json">' + json.dumps(posting, ensure_ascii=False) + '</script>'
        source = {"name": "本地公开接口替身", "url": job.url, "kind": "html", "search_city": "广州"}
        class Fetcher:
            requests = 0
            async def fetch(inner, url):
                inner.requests += 1
                return body
        search = await search_public([source], Fetcher(), 5, 1, True,
                                    eligible=lambda job: candidate_available(self.runtime, RESUME, job))
        self.assertEqual(len(search["candidates"]), 1)
        self.assertEqual(search["candidates"][0].parsing_evidence["method"], "json_ld_jobposting")
        result = await workflow(self.runtime, search, RESUME, adapter=self.adapter, facts=self.facts)
        self.assertEqual(result["summary"]["applications"][0]["state"], "simulated_submitted")
        self.assertEqual(result["summary"]["source_issue_count"], 0)

    async def test_applied_guangzhou_role_does_not_block_search_fallback(self):
        from job_agent.automation.workflow import candidate_available
        result = await self.analyze()
        identity = result["discovery"]["ranked"][0]["job_id"]
        self.runtime.workspace.memory.library.set_status(identity, "applied")
        self.assertFalse(candidate_available(self.runtime, RESUME, candidates(self.now)[0]))

    async def test_independent_next_day_run_does_not_hit_old_process_request_counter(self):
        first = candidates(self.now)[0]
        other = replace(first, company="第二人工公司", url="https://careers.example.invalid/jobs/5")
        await self.analyze([first, other])
        self.assertEqual(len(self.client.calls), 6)
        self.now += timedelta(days=1)
        third = replace(first, company="第三人工公司", url="https://careers.example.invalid/jobs/6")
        result = await self.analyze([third])
        self.assertEqual(result["summary"]["new_analyses"], 1)
        self.assertEqual(len(self.client.calls), 9)

    async def test_material_tampering_is_blocked(self):
        prepared, job, report = await self.packet()
        for field, value in (("resume", "虚构博士经历"), ("target", "https://evil.example.invalid"), ("job_id", 999)):
            changed = copy.deepcopy(prepared)
            changed["packet"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                await self.service.submit(changed, job, report, RESUME)
        self.assertEqual(self.adapter.submit_calls, 0)

    async def test_unknown_personal_facts_require_human(self):
        self.facts["name"]["confirmed"] = False
        prepared, job, report = await self.packet()
        self.assertEqual(prepared["state"], "review_required")
        with self.assertRaises(GuardError):
            await self.service.submit(prepared, job, report, RESUME)

    async def test_custom_questions_and_consent_require_explicit_per_job_answers(self):
        self.service = ApplicationService(self.runtime.automation, self.limits,
                           MockATS(self.runtime.automation, extra_fields=(Field("privacy_consent", kind="consent"),)))
        prepared, job, report = await self.packet()
        self.assertEqual(prepared["state"], "review_required")
        self.assertIn("申请字段待人工确认：privacy_consent", prepared["packet"]["review_items"])

    async def test_fabricated_personal_fact_without_original_quote_is_blocked(self):
        self.facts["name"] = {"value": "虚构个人", "quote": "姓名：虚构个人", "confirmed": True}
        prepared, *_ = await self.packet()
        self.assertEqual(prepared["state"], "review_required")

    async def test_original_resume_not_overwritten_and_files_are_private(self):
        original = self.root / "original.md"
        original.write_text(RESUME)
        prepared, *_ = await self.packet()
        self.assertEqual(original.read_text(), RESUME)
        path = Path(prepared["packet_path"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        resume_path = Path(prepared["packet"]["materials"]["tailored-resume.md"])
        self.assertTrue(resume_path.is_file())
        self.assertIn("学习", prepared["packet"]["resume"])

    async def test_real_network_adapter_is_rejected(self):
        class External:
            name = "unknown"
        with self.assertRaises(PermissionError):
            ApplicationService(self.runtime.automation, self.limits, External())

    async def test_manual_handoff_does_not_submit_or_change_status(self):
        self.service = ApplicationService(self.runtime.automation, self.limits, ManualHandoff())
        prepared, job, report = await self.packet()
        result = await self.service.submit(prepared, job, report, RESUME)
        self.assertEqual(result["state"], "manual_handoff")
        self.assertEqual(self.runtime.automation.status()["applications"], [])

    async def test_report_and_ledger_do_not_log_resume_or_error_body(self):
        self.client.failure = "timeout"
        await self.analyze()
        log = json.dumps(self.runtime.automation.status(), ensure_ascii=False)
        self.assertNotIn("candidate@example.invalid", log)
        self.assertNotIn("人工超时", log)
        self.assertNotIn("offline-placeholder", log)
        self.assertNotIn("使用Python实现", log)

    async def test_guangzhou_enough_does_not_select_shanghai(self):
        first = candidates(self.now)[0]
        second = replace(first, company="广州第二人工公司", url="https://careers.example.invalid/jobs/3")
        shanghai = replace(first, city="上海", jd=first.jd.replace("广州", "上海"), url="https://careers.example.invalid/jobs/4")
        result = await self.analyze([first, second, shanghai])
        self.assertEqual(result["discovery"]["city_policy"]["stages"], ["广州"])
        self.assertTrue(all(r["city"] == "广州" for r in result["summary"]["jobs"]))

    async def test_guangzhou_shortage_expands_shanghai_before_shenzhen(self):
        first = candidates(self.now)[0]
        shanghai = replace(first, city="上海", jd=first.jd.replace("广州", "上海"), url="https://careers.example.invalid/jobs/3")
        shenzhen = replace(first, city="深圳", jd=first.jd.replace("广州", "深圳"), url="https://careers.example.invalid/jobs/4")
        result = await self.analyze([first, shanghai, shenzhen])
        self.assertEqual(result["discovery"]["city_policy"]["stages"], ["广州", "上海"])

    async def test_daily_submission_limit_counts_failed_attempts(self):
        prepared, job, report = await self.packet()
        self.service.limits = replace(self.limits, daily_applications=1)
        self.adapter.behavior = "timeout"
        await self.service.submit(prepared, job, report, RESUME)
        other = replace(job, company="第二人工公司", url="https://careers.example.invalid/jobs/5")
        result = await self.analyze([other])
        row = result["discovery"]["ranked"][0]
        prepared = await self.service.prepare(self.materials, row["job_id"], other, row["report"], RESUME, self.facts)
        with self.assertRaises(GuardError):
            await self.service.submit(prepared, other, row["report"], RESUME)
        self.assertEqual(self.adapter.submit_calls, 1)

    async def test_schedule_uses_existing_due_and_background_without_installing(self):
        from test_job_agent_updates import arguments
        (self.root / "resume.md").write_text(RESUME)
        args = arguments(self.root)
        search = AsyncMock(return_value={"candidates": candidates(self.now), "issues": [], "http_requests": 0})
        updates = AutomationUpdates(self.materials, args, analysis_runtime=self.runtime, clock=self.clock, search=search)
        try:
            updates.configure("09:00")
            self.assertFalse(await updates.tick())
            self.now += timedelta(hours=1)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(await updates.tick())
                done = await updates.wait_current()
            self.assertEqual(done["status"], "completed")
            self.assertEqual(len(self.client.calls), 3)
            self.assertFalse(await updates.tick())
            self.assertIsNotNone(updates.store.last_check())
            self.assertTrue(updates.store.today_jobs(self.now))
            self.assertIsNotNone(updates.store.schedule()["next_run_at"])
        finally:
            await updates.close()


if __name__ == "__main__":
    unittest.main()
