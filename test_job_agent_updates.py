"""v5 离线验收：可控北京时间、真实 SQLite/local Agent、mock 公开搜索。"""

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_platform.terminal_ui import PROJECT
from background_tools import BackgroundCallbacks
from scheduled_tools import ScheduledCallbacks
from job_agent.cli import handle_update_command, run
from job_agent.runtime import JobRuntime
from job_agent.scheduler import DailyUpdates, check_lock, validate_update_args
from job_agent.search import JobCandidate, PublicFetcher
from job_agent.update_store import CHINA, daily_time, next_daily
from job_agent.source_health import SourceHealthStore, source_check


JD = "工作地点：广州\n岗位职责：开发 AI Agent 应用，实现工具调用、工作流和自动测试。\n任职要求：2027届本科在读，专业不限；熟悉Python、Git。\n加分项：了解RAG。\n成长：导师带教，技术分享。"
RESUME = "学历：2027届本科在读，能源与动力工程\n项目：使用Python、Git完成Job Agent。\n正在学习RAG。"


def job(**changes):
    return JobCandidate(**{**dict(company="人工更新科技", title="AI Agent开发实习生", city="广州",
        recruitment_type="实习", jd=JD, source="人工公开来源", url="https://careers.example.com/jobs/1",
        detail_url="https://careers.example.com/jobs/1", discovered_at="2026-10-08T00:00:00+00:00"), **changes})


def arguments(root, **changes):
    return Namespace(**{**dict(live=False, mode="local", model=None, fallback="local", llm_dry_run=False,
        jd=None, list_jobs=False, skill="all", state_dir=None, session="default", resume=str(root / "resume.md"),
        search=False, search_demo=False, search_fixture=str(root / "jobs.json"), sources=None,
        allow_network=False, search_only=False, search_limit=8, analyze_limit=4, city_min=2,
        guangzhou_only=False, watch=False, check_now=False, daily_at=None), **changes})


class Clock:
    def __init__(self, value="2026-10-08T08:00:00+08:00"):
        self.value = datetime.fromisoformat(value)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class DailyTimeTests(unittest.TestCase):
    def test_daily_time_rejects_invalid_values(self):
        for value in ("9:00", "24:00", "09:60", "09:00:00", "00:00\n", "", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "HH:MM"):
                daily_time(value)

    def test_next_daily_uses_beijing_and_rolls_to_next_day_at_exact_time(self):
        before = datetime.fromisoformat("2026-10-08T00:59:59+00:00")
        self.assertEqual(next_daily(before, "09:00"), datetime.fromisoformat("2026-10-08T01:00:00+00:00"))
        self.assertEqual(next_daily(before + timedelta(seconds=1), "09:00"), datetime.fromisoformat("2026-10-09T01:00:00+00:00"))


class DailyUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        shutil.copytree(PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        (self.root / "resume.md").write_text(RESUME)
        (self.root / "jobs.json").write_text(json.dumps([job().to_dict()], ensure_ascii=False))
        for name in ("connect", "connect_ex"):
            guard = patch.object(socket.socket, name, side_effect=AssertionError("禁止真实网络"))
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.dict(os.environ, {}, clear=True)
        guard.start()
        self.addCleanup(guard.stop)
        guard = patch("job_agent.runtime.LLMAnalyzer", side_effect=AssertionError("v5 禁止实例化付费分析器"))
        guard.start()
        self.addCleanup(guard.stop)
        self.output = io.StringIO()
        guard = contextlib.redirect_stdout(self.output)
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        self.runtime = JobRuntime(self.root, demo=True)
        self.args, self.clock = arguments(self.root), Clock()
        self.candidates, self.issues = [job()], []
        self.search = AsyncMock(side_effect=self.search_result)
        self.updates = DailyUpdates(self.runtime, self.args, clock=self.clock, search=self.search)

    async def asyncTearDown(self):
        await self.updates.close()
        await self.runtime.close()

    async def search_result(self, args):
        return {"candidates": self.candidates, "issues": self.issues, "http_requests": 0}

    async def check(self):
        self.assertTrue(self.updates.start_check()["ok"])
        done = await self.updates.wait_current()
        self.assertEqual(done["status"], "completed")
        return done["result"]

    async def test_not_due_does_not_search_or_analyze(self):
        self.updates.configure("09:00")
        self.assertFalse(await self.updates.tick())
        self.search.assert_not_called()
        self.assertIsNone(self.updates.store.last_check())

    async def test_source_verification_is_persisted_in_same_job_database_and_last_check(self):
        check = source_check(dict(name="人工来源", url=job().url, kind="html"), [job()], [job().url], [], self.clock().isoformat())
        self.search.side_effect = None
        self.search.return_value = {"candidates": self.candidates, "issues": [], "http_requests": 0, "source_checks": [check]}
        summary = await self.check()
        self.assertEqual(summary["source_checks"][0]["status"], "verified")
        self.assertEqual(self.updates.store.last_check()["summary"]["source_health"][0]["last_success_at"], check["checked_at"])
        self.assertEqual(SourceHealthStore(self.runtime.workspace.memory).list()[0]["eligible_count"], 1)

    async def test_exact_due_triggers_once_and_saves_next_daily(self):
        self.updates.configure("09:00")
        self.clock.advance(hours=1)
        self.assertTrue(await self.updates.tick())
        self.assertFalse(await self.updates.tick())
        await self.updates.wait_current()
        self.assertEqual(self.search.await_count, 1)
        self.assertEqual(self.updates.store.last_check()["origin"], "scheduled")
        self.assertEqual(datetime.fromisoformat(self.updates.store.schedule()["next_run_at"]).astimezone(CHINA).hour, 9)
        self.assertEqual(datetime.fromisoformat(self.updates.store.schedule()["next_run_at"]).day, 9)

    async def test_existing_scheduled_manager_dispatches_due_check(self):
        self.updates.configure("09:00")
        self.clock.advance(hours=1)
        self.updates.start_watch()  # 计算出 0 秒延迟，使用真实旧计时器触发宿主 callback。
        for _ in range(8):
            if self.updates.background.workers:
                break
            await asyncio.sleep(0)
        self.assertTrue(self.updates.background.workers)
        await self.updates.wait_current()
        self.assertEqual(self.search.await_count, 1)
        self.assertTrue(self.updates.watching)
        self.assertEqual(len(self.updates.timer.jobs), 1)

    async def test_not_due_watch_installs_timer_without_starting_work(self):
        self.updates.configure("09:00")
        self.updates.start_watch()
        await asyncio.sleep(0)
        self.assertFalse(self.updates.running)
        self.search.assert_not_called()

    async def test_scheduler_failure_stops_instead_of_retrying_due_tick_forever(self):
        self.updates.configure("09:00")
        self.clock.advance(hours=1)
        with patch.object(self.updates.store, "claim_due", side_effect=OSError("人工调度故障")) as claim:
            self.updates.start_watch()
            for _ in range(8):
                await asyncio.sleep(0)
            self.assertFalse(self.updates.watching)
            self.assertEqual(claim.call_count, 1)
        self.search.assert_not_called()
        self.assertEqual(self.updates.store.schedule()["daily_at"], "09:00")

    async def test_new_job_saved_and_summary_has_all_required_fields(self):
        summary = await self.check()
        self.assertEqual(summary["new_count"], 1)
        self.assertEqual(summary["analyzed_count"], 1)
        self.assertEqual(summary["estimated_rmb"], 0)
        self.assertEqual(summary["mode"], "local")
        row = summary["jobs"][0]
        for field in ("company", "title", "city", "overall_match", "recommendation", "url"):
            self.assertIsNotNone(row[field])
        saved = self.runtime.workspace.memory.library.get(row["job_id"])
        self.assertEqual(saved["status"], "discovered")
        self.assertEqual(saved["jd"], job().jd)
        self.assertEqual(sum(summary["recommendations"].values()), 1)
        self.assertIn("本次新增岗位：1", self.output.getvalue())
        self.assertEqual(len(self.updates.store.today_jobs(self.clock())), 1)

    async def test_repeat_and_alias_do_not_analyze_again_or_save_twice(self):
        first = await self.check()
        library = self.runtime.workspace.memory.library
        identity = first["jobs"][0]["job_id"]
        library.set_status(identity, "interview")
        library.add_note(identity, "等待二面")
        self.candidates.append(job(source="另一人工来源", url="https://other.example.com/job/2", detail_url="https://other.example.com/job/2"))
        with patch.object(JobRuntime, "analyze", side_effect=AssertionError("重复 JD 不得重新分析")):
            second = await self.check()
        self.assertEqual(second["new_count"], 0)
        self.assertEqual(second["analyzed_count"], 0)
        self.assertEqual(second["unchanged_count"], 1)
        self.assertEqual(len(library.list()), 1)
        self.assertEqual(library.get(identity)["status"], "interview")
        self.assertEqual(library.get(identity)["notes"], "等待二面")

    async def test_resume_change_alone_does_not_trigger_unchanged_jd_analysis(self):
        await self.check()
        (self.root / "resume.md").write_text(RESUME + "\n正在学习Docker。")
        with patch.object(JobRuntime, "analyze", side_effect=AssertionError("每日检查不重复分析未变化 JD")):
            self.assertEqual((await self.check())["analyzed_count"], 0)

    async def test_completed_analysis_without_technical_requirements_is_not_repeated(self):
        self.candidates = [job(jd="工作地点：广州\n岗位职责：参与AI应用工作。\n任职要求：2027届本科在读，专业不限；对Agent感兴趣。")]
        first = await self.check()
        self.assertIsNone(first["jobs"][0]["overall_match"])
        self.assertEqual(first["pending_count"], 0)
        with patch.object(JobRuntime, "analyze", side_effect=AssertionError("不适用分数不是未完成")):
            second = await self.check()
        self.assertEqual(second["analyzed_count"], 0)
        self.assertEqual(second["unchanged_count"], 1)

    async def test_interrupted_local_analysis_can_resume_next_check_without_paid_requests(self):
        with patch.object(JobRuntime, "analyze", side_effect=ValueError("人工分析中断")):
            await self.check()
        self.assertEqual(self.updates.store.last_check()["status"], "failed")
        resumed = await self.check()
        self.assertEqual(resumed["new_count"], 0)
        self.assertEqual(resumed["analyzed_count"], 1)
        self.assertEqual(resumed["pending_count"], 0)

    async def test_empty_successful_search_is_distinct_from_source_failure(self):
        self.candidates = []
        summary = await self.check()
        self.assertEqual(summary["new_count"], 0)
        self.assertEqual(self.updates.store.last_check()["status"], "completed")
        self.assertEqual(summary["issues"], [])

    async def test_substantive_jd_change_reanalyzes_without_resetting_progress(self):
        first = await self.check()
        identity = first["jobs"][0]["job_id"]
        library = self.runtime.workspace.memory.library
        before = library.set_status(identity, "applied")
        library.add_note(identity, "已提交作品")
        self.candidates = [job(jd=JD + "\n必须熟悉Docker。")]
        second = await self.check()
        self.assertEqual(second["new_count"], 0)
        self.assertEqual(second["updated_count"], 1)
        self.assertEqual(second["analyzed_count"], 1)
        self.assertEqual(len(library.list()), 1)
        after = library.get(identity)
        self.assertEqual(after["status"], "applied")
        self.assertEqual(after["applied_at"], before["applied_at"])
        self.assertEqual(after["notes"], "已提交作品")
        self.assertLess(after["overall_match"], first["jobs"][0]["overall_match"])

    async def test_analysis_limit_leaves_pending_then_completes_without_duplicates(self):
        self.args.analyze_limit = 1
        self.candidates = [job(company="人工甲"), job(company="人工乙")]
        first = await self.check()
        self.assertEqual(first["new_count"], 2)
        self.assertEqual(first["analyzed_count"], 1)
        self.assertEqual(first["pending_count"], 1)
        second = await self.check()
        self.assertEqual(second["new_count"], 0)
        self.assertEqual(second["analyzed_count"], 1)
        self.assertEqual(len(self.runtime.workspace.memory.library.list()), 2)

    async def test_manual_check_keeps_daily_due_and_timer(self):
        self.updates.configure("09:00")
        self.updates.start_watch()
        due = self.updates.store.schedule()["next_run_at"]
        handle = next(iter(self.updates.timer.timers.values()))
        await self.check()
        self.assertEqual(self.updates.store.schedule()["next_run_at"], due)
        self.assertFalse(handle.cancelled())
        self.assertTrue(self.updates.watching)

    async def test_manual_crossing_due_does_not_lose_scheduled_check(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocking(args):
            entered.set()
            await release.wait()
            return await self.search_result(args)
        self.updates.search = blocking
        self.updates.configure("09:00")
        self.updates.start_check()
        await entered.wait()
        self.clock.advance(hours=1)
        self.assertFalse(await self.updates.tick())
        self.assertEqual(datetime.fromisoformat(self.updates.store.schedule()["next_run_at"]).astimezone(CHINA).day, 8)
        release.set()
        await self.updates.wait_current()
        self.assertTrue(await self.updates.tick())
        await self.updates.wait_current()
        self.assertEqual(self.updates.store.last_check()["summary"]["analyzed_count"], 0)

    async def test_duplicate_manual_start_returns_busy(self):
        self.updates.start_check()
        self.assertFalse(self.updates.start_check()["ok"])
        await self.updates.wait_current()
        self.assertEqual(self.search.await_count, 1)

    async def test_process_lock_prevents_search_by_another_controller(self):
        with check_lock(self.runtime.workspace.root) as acquired:
            self.assertTrue(acquired)
            self.updates.start_check()
            result = await self.updates.wait_current()
        self.assertTrue(result["result"]["skipped"])
        self.search.assert_not_called()
        self.assertIsNone(self.updates.store.last_check())

    async def test_reopen_restores_schedule_timestamps_summary_and_today_jobs(self):
        self.updates.configure("09:00")
        await self.check()
        before = self.updates.store.schedule()
        last = self.updates.store.last_check()
        await self.updates.close()
        await self.runtime.close()
        self.runtime = JobRuntime(self.root, demo=True)
        self.updates = DailyUpdates(self.runtime, self.args, clock=self.clock, search=self.search)
        self.assertEqual(self.updates.store.schedule(), before)
        self.assertEqual(self.updates.configure("09:00"), before)
        self.assertEqual(self.updates.store.last_check(), last)
        self.assertEqual(len(self.updates.store.today_jobs(self.clock())), 1)
        self.assertFalse(self.updates.watching)  # 保存计划本身不会偷偷启动长任务。

    async def test_overdue_days_catch_up_once_then_next_future_day(self):
        self.updates.configure("09:00")
        self.clock.advance(days=3, hours=2)
        self.assertTrue(await self.updates.tick())
        await self.updates.wait_current()
        self.assertFalse(await self.updates.tick())
        due = datetime.fromisoformat(self.updates.store.schedule()["next_run_at"]).astimezone(CHINA)
        self.assertEqual(due.day, 12)
        self.assertEqual(self.search.await_count, 1)

    async def test_interrupted_run_recovered_without_automatic_retry(self):
        identity = self.updates.store.begin("scheduled", self.clock())
        reopened = DailyUpdates(self.runtime, self.args, clock=self.clock, search=self.search)
        try:
            self.assertEqual(reopened.store.last_check()["run_id"], identity)
            self.assertEqual(reopened.store.last_check()["status"], "interrupted")
            self.assertIn("被中断", reopened.store.last_check()["summary"]["issues"][0]["reason"])
            self.search.assert_not_called()
        finally:
            await reopened.close()

    async def test_last_found_changes_only_for_new_jobs(self):
        await self.check()
        first_found = self.updates.store.schedule()["last_found_at"]
        self.clock.advance(hours=1)
        await self.check()
        self.assertEqual(self.updates.store.schedule()["last_found_at"], first_found)
        self.candidates = [job(jd=JD + "\n必须熟悉Docker。")]
        await self.check()
        self.assertEqual(self.updates.store.schedule()["last_found_at"], first_found)
        self.candidates.append(job(company="人工新公司"))
        await self.check()
        self.assertNotEqual(self.updates.store.schedule()["last_found_at"], first_found)

    async def test_today_boundary_uses_beijing_and_deduplicates_checks(self):
        self.clock.value = datetime.fromisoformat("2026-10-08T15:59:59+00:00")
        await self.check()
        await self.check()
        self.assertEqual(len(self.updates.store.today_jobs(self.clock())), 1)
        self.clock.advance(seconds=1)
        self.assertEqual(self.updates.store.today_jobs(self.clock()), [])

    async def test_search_crossing_midnight_counts_day_of_discovery_not_run_start(self):
        self.clock.value = datetime.fromisoformat("2026-10-08T23:59:59+08:00")
        previous_day = self.clock()
        async def crosses_midnight(args):
            self.clock.advance(seconds=2)
            return await self.search_result(args)
        self.updates.search = crosses_midnight
        await self.check()
        self.assertEqual(self.updates.store.today_jobs(previous_day), [])
        self.assertEqual(len(self.updates.store.today_jobs(self.clock())), 1)

    async def test_all_sources_failed_are_not_reported_as_successful_zero_new(self):
        self.candidates = []
        self.issues = [{"reason": "来源要求登录，已跳过。"}]
        summary = await self.check()
        self.assertEqual(self.updates.store.last_check()["status"], "failed")
        self.assertEqual(summary["issues"], self.issues)
        self.assertIsNone(self.updates.store.schedule()["last_found_at"])

    async def test_unexpected_failure_records_error_once_and_keeps_next_due(self):
        self.updates.search = AsyncMock(side_effect=OSError("人工故障正文不写日志"))
        self.updates.configure("09:00")
        before = self.updates.store.schedule()["next_run_at"]
        summary = await self.check()
        self.assertEqual(self.updates.search.await_count, 1)
        self.assertEqual(self.updates.store.last_check()["status"], "failed")
        self.assertNotIn("人工故障正文", json.dumps(summary, ensure_ascii=False))
        self.assertEqual(self.updates.store.schedule()["next_run_at"], before)

    async def test_shutdown_cancels_background_and_keeps_persistent_plan(self):
        started = asyncio.Event()
        async def slow(args):
            started.set()
            await asyncio.Event().wait()
        self.updates.search = slow
        self.updates.configure("09:00")
        self.updates.start_watch()
        self.updates.start_check()
        await started.wait()
        await self.updates.close()
        self.assertTrue(all(worker.done() for worker in self.updates.background.workers.values()))
        self.assertEqual(self.updates.store.last_check()["status"], "interrupted")
        self.assertEqual(self.updates.store.schedule()["daily_at"], "09:00")

    async def test_city_strategy_filters_source_location_and_stops_expansion(self):
        self.candidates = [job(company="人工广州甲", city="深圳", source="深圳高校网"), job(company="人工广州乙"),
                           job(company="人工上海", city="上海", jd=JD.replace("工作地点：广州", "工作地点：上海")),
                           job(company="人工深圳", city="深圳", jd=JD.replace("工作地点：广州", "工作地点：深圳"))]
        summary = await self.check()
        self.assertEqual([row["city"] for row in summary["jobs"]], ["广州", "广州"])
        self.assertEqual(summary["city_policy"]["stages"], ["广州"])

    async def test_update_cli_reads_history_without_search_or_resume(self):
        await self.check()
        self.assertTrue(handle_update_command(self.runtime, "/last-check", ""))
        self.assertTrue(handle_update_command(self.runtime, "/new-jobs", "", self.updates))
        with self.assertRaisesRegex(ValueError, "--watch"):
            handle_update_command(self.runtime, "/check-now", "")
        self.assertEqual(self.search.await_count, 1)
        self.assertIn("今日新增岗位：1", self.output.getvalue())
        self.assertIn("未启动", self.output.getvalue())

    async def test_update_cli_manual_dispatch_does_not_change_plan(self):
        self.updates.configure("09:00")
        due = self.updates.store.schedule()["next_run_at"]
        self.assertTrue(handle_update_command(self.runtime, "/check-now", "", self.updates))
        await self.updates.wait_current()
        self.assertEqual(self.updates.store.schedule()["next_run_at"], due)

    async def test_cli_rejects_llm_before_initializing_client_or_reading_resume(self):
        invalid = arguments(self.root, watch=True, daily_at="09:00", mode="llm", resume="不存在.pdf")
        with self.assertRaisesRegex(ValueError, "只允许 local"):
            await run(invalid)

    async def test_bad_config_and_network_without_authorization_rejected(self):
        for changes in ({"watch": True, "daily_at": "25:00"}, {"check_now": True, "daily_at": "09:00"},
                        {"watch": True, "search_fixture": None, "sources": "unknown.json"},
                        {"watch": True, "search_only": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_update_args(arguments(self.root, **changes))
        self.search.assert_not_called()

    async def test_public_html_search_through_existing_parser_and_local_pipeline(self):
        import httpx2
        from job_agent.search_cli import collect_search
        source = dict(name="人工公司公开招聘页", kind="html", url="https://careers.example.com/jobs", enabled=True, search_city="广州")
        sources_path = self.root / "sources.json"
        sources_path.write_text(json.dumps([source]))
        self.args.search_fixture, self.args.sources, self.args.allow_network = None, str(sources_path), True
        self.updates.search = collect_search
        requests = []
        posting = {"@type": "JobPosting", "title": job().title, "hiringOrganization": {"name": job().company},
                   "jobLocation": {"address": {"addressLocality": "广州"}}, "employmentType": "INTERN",
                   "description": JD.replace("\n", "<br>"), "url": job().url}
        body = '<script type="application/ld+json">' + json.dumps(posting, ensure_ascii=False) + '</script>'
        def handler(request):
            requests.append(request.url.path)
            return httpx2.Response(200, text="User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else body,
                                   headers={"content-type": "text/plain" if request.url.path == "/robots.txt" else "text/html"})
        def factory(sources, allow_network):
            return PublicFetcher(sources, allow_network=allow_network, transport=httpx2.MockTransport(handler), delay=0,
                                 resolver=lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
        with patch("job_agent.search_cli.PublicFetcher", side_effect=factory):
            first = await self.check()
            second = await self.check()
        self.assertEqual(first["new_count"], 1)
        self.assertEqual(first["analyzed_count"], 1)
        self.assertEqual(first["http_requests"], 2)
        self.assertEqual(second["analyzed_count"], 0)
        self.assertEqual(requests, ["/robots.txt", "/jobs", "/robots.txt", "/jobs"])

    async def test_host_background_callback_reuses_status_and_cleans_failed_worker(self):
        pool = BackgroundCallbacks()
        async def fail():
            raise RuntimeError("人工敏感故障不应该出现在快照")
        submitted = pool.start_callback(fail)
        identity = submitted["task"]["task_id"]
        self.assertEqual(submitted["task"]["status"], "pending")
        await pool.workers[identity]
        result = await pool.get_result(identity)
        self.assertEqual(result["task"]["error"], "RuntimeError")
        self.assertNotIn("人工敏感故障", json.dumps(pool.snapshot(), ensure_ascii=False))
        await pool.close()
        self.assertTrue(pool.workers[identity].done())
        self.assertIsInstance(self.updates.timer, ScheduledCallbacks)


if __name__ == "__main__":
    unittest.main()
