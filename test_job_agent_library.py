"""v4 离线验收：岗位库、旧数据导入、CLI 和真实 local 工作流。"""

import contextlib
import io
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_platform.terminal_ui import PROJECT
from job_agent.analysis import ANALYSIS_VERSION, analyze_jd, fingerprint, match_resume
from job_agent.cli import interactive
from job_agent.discovery import DiscoveryStore, analyze_candidates, rank_job
from job_agent.job_library import JOB_STATUSES
from job_agent.memory import JobMemory
from job_agent.runtime import JobRuntime
from job_agent.search import JobCandidate


JD = "工作地点：广州\n岗位职责：参与AI Agent应用开发。\n任职要求：2027届本科在读，熟悉Python。\n加分项：了解RAG。"
RESUME = "2027届本科在读，能源与动力工程\n项目：使用Python完成Job Agent。\n正在学习RAG。"


def candidate(**changes):
    return JobCandidate(**{**dict(company="人工验收公司", title="AI Agent开发实习生", city="广州",
        recruitment_type="实习", jd=JD, source="人工公开来源", url="https://careers.example.com/jobs/1",
        detail_url="https://careers.example.com/jobs/1", discovered_at="2026-10-08T00:00:00+00:00"), **changes})


def report(job):
    jd = analyze_jd(job.analysis_text())
    return {"mode": "local_rules", "jd_analysis": {"analysis": jd},
            "resume_match": {"matching": match_resume(jd, RESUME)}}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "jobs.sqlite"
        self.memory = JobMemory(self.path)
        self.library = self.memory.library
        self.store = DiscoveryStore(self.memory)
        self.identity, _, _ = self.store.remember(candidate())
        self.job_id = self.library.linked("candidate", self.identity)

    def tearDown(self):
        self.memory.close()

    def progress(self):
        self.library.set_status(self.job_id, "applied")
        self.library.set_status(self.job_id, "interview")
        self.library.add_note(self.job_id, "已收到一面邀请；等待时间确认。")
        return self.library.get(self.job_id)

    def test_all_statuses_and_application_time_preserved_after_interview(self):
        applied_at = None
        for status in JOB_STATUSES:
            with self.subTest(status=status):
                job = self.library.set_status(self.job_id, status)
                self.assertEqual(job["status"], status)
                if status == "applied":
                    applied_at = job["applied_at"]
                    self.assertIsNotNone(applied_at)
                self.assertEqual(job["applied_at"], applied_at)

    def test_same_status_does_not_change_timestamps(self):
        before = self.library.set_status(self.job_id, "applied")
        self.assertEqual(self.library.set_status(self.job_id, "applied"), before)

    def test_can_skip_stages_and_correct_manual_status(self):
        self.assertIsNone(self.library.set_status(self.job_id, "interview")["applied_at"])
        self.library.set_status(self.job_id, "withdrawn")
        self.assertEqual(self.library.set_status(self.job_id, "shortlisted")["status"], "shortlisted")

    def test_invalid_status_rejected_without_any_write(self):
        before = self.library.get(self.job_id)
        for invalid in ("APPLIED", "投递", "", "applied; DROP TABLE job_library"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "无效投递状态"):
                self.library.set_status(self.job_id, invalid)
        self.assertEqual(self.library.get(self.job_id), before)

    def test_database_also_rejects_invalid_status(self):
        with self.assertRaises(sqlite3.IntegrityError), self.memory.connection:
            self.memory.connection.execute("UPDATE job_library SET status='invalid'")
        self.assertEqual(self.library.get(self.job_id)["status"], "discovered")

    def test_missing_and_invalid_job_ids_rejected(self):
        for invalid in (0, -1, "abc", "1 OR 1=1", "١", "1.0", "9" * 100, 9999):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.library.get(invalid)

    def test_duplicate_keeps_status_notes_and_dates(self):
        before = self.progress()
        identity, _, duplicate = self.store.remember(replace(candidate(), discovered_at="2026-10-10T00:00:00+00:00"))
        self.assertTrue(duplicate)
        self.assertEqual(identity, self.identity)
        self.assertEqual(len(self.library.list()), 1)
        for field in ("status", "notes", "applied_at", "status_updated_at", "first_discovered_at"):
            self.assertEqual(self.library.get(self.job_id)[field], before[field])

    def test_changed_jd_keeps_progress_and_clears_stale_score(self):
        old = candidate()
        result = report(old)
        self.library.record_result(self.job_id, fingerprint(old.analysis_text()), result, rank_job(old, result))
        before = self.progress()
        new = replace(old, jd=JD + "\n必须熟悉Docker。", discovered_at="2026-10-10T00:00:00+00:00")
        new_id, _, duplicate = self.store.remember(new)
        self.assertFalse(duplicate)  # v3 分析版本需要重新分析。
        self.assertEqual(self.library.linked("candidate", new_id), self.job_id)
        self.assertEqual(len(self.library.list()), 1)
        updated = self.library.get(self.job_id)
        self.assertEqual(updated["jd"], new.jd)
        self.assertIsNone(updated["overall_match"])
        self.assertIsNone(updated["recommendation"])
        for field in ("status", "notes", "applied_at", "status_updated_at", "first_discovered_at"):
            self.assertEqual(updated[field], before[field])

    def test_old_jd_report_cannot_overwrite_new_score(self):
        old = candidate()
        new = replace(old, jd=JD + "\n必须掌握Docker。")
        self.store.remember(new)
        new_report = report(new)
        self.library.record_result(self.job_id, fingerprint(new.analysis_text()), new_report, rank_job(new, new_report))
        before = self.library.get(self.job_id)
        self.library.record_result(self.job_id, fingerprint(old.analysis_text()), report(old), rank_job(old, report(old)))
        self.assertEqual(self.library.get(self.job_id), before)

    def test_different_detail_links_are_separate_positions(self):
        self.store.remember(candidate(url="https://careers.example.com/jobs/2", detail_url="https://careers.example.com/jobs/2",
                                      jd=JD + "\n要求熟悉Docker。"))
        self.assertEqual(len(self.library.list()), 2)

    def test_other_source_alias_still_same_position(self):
        self.store.remember(candidate(source="人工第二来源", url="https://jobs.example.com/7", detail_url="https://jobs.example.com/7"))
        self.assertEqual(len(self.library.list()), 1)
        self.assertEqual(len(self.store.sources(self.identity)), 2)

    def test_source_site_city_not_used_as_work_city(self):
        job = candidate(city="深圳", source="深圳高校公开就业网", url="https://sz.example.com/jobs/9")
        identity, _, _ = self.store.remember(job)
        self.assertEqual(self.library.get(self.library.linked("candidate", identity))["city"], "广州")

    def test_notes_append_and_sql_like_text_is_literal(self):
        self.library.add_note(self.job_id, "一面等待中")
        updated = self.library.add_note(self.job_id, "备注：'); DROP TABLE job_library; --")
        self.assertEqual(updated["notes"], "一面等待中\n备注：'); DROP TABLE job_library; --")
        self.assertEqual(updated["status"], "discovered")

    def test_empty_and_credential_notes_rejected(self):
        for note in ("  ", "OPENAI_API_KEY=sk-" + "a" * 40):
            with self.subTest(note_kind="空白" if not note.strip() else "人工凭据"), self.assertRaises(ValueError):
                self.library.add_note(self.job_id, note)
        self.assertEqual(self.library.get(self.job_id)["notes"], "")

    def test_state_filter_and_zero_counts_funnel(self):
        self.library.set_status(self.job_id, "interview")
        for status, company in (("shortlisted", "人工乙"), ("applied", "人工丙"), ("offer", "人工丁")):
            identity, _, _ = self.store.remember(candidate(company=company))
            self.library.set_status(self.library.linked("candidate", identity), status)
        self.assertEqual(len(self.library.list()), 4)
        for status in ("interview", "shortlisted", "applied", "offer"):
            self.assertEqual([job["status"] for job in self.library.list(status)], [status])
        self.assertEqual(self.library.list("rejected"), [])
        funnel = self.library.funnel()
        self.assertEqual(funnel["total"], 4)
        self.assertEqual(sum(funnel["counts"].values()), 4)
        self.assertEqual(funnel["counts"]["interview"], 1)
        self.assertEqual(funnel["counts"]["discovered"], 0)
        self.assertEqual(funnel["counts"]["written_test"], 0)
        with self.assertRaises(ValueError):
            self.library.list("invalid")

    def test_progress_survives_database_reopen(self):
        before = self.progress()
        self.memory.close()
        self.memory = JobMemory(self.path)
        self.library = self.memory.library
        self.assertEqual(self.library.get(self.job_id), before)


class LegacyMigrationTests(unittest.TestCase):
    def legacy_database(self, path, job, include_discovery=True):
        data = report(job)
        jd_id = fingerprint(job.analysis_text())
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE jobs (jd_id TEXT PRIMARY KEY, company TEXT, title TEXT, report_json TEXT NOT NULL, source_label TEXT NOT NULL, analyzed_at TEXT NOT NULL, analysis_version INTEGER NOT NULL)")
            db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (jd_id, job.company, job.title, json.dumps(data["jd_analysis"]["analysis"]), job.source, job.discovered_at, ANALYSIS_VERSION))
            if include_discovery:
                db.execute("CREATE TABLE discovered_jobs (candidate_id TEXT PRIMARY KEY, candidate_json TEXT NOT NULL, sources_json TEXT NOT NULL)")
                db.execute("CREATE TABLE discovery_analyses (candidate_id TEXT, context_key TEXT, state TEXT, report_json TEXT, PRIMARY KEY(candidate_id, context_key))")
                db.execute("INSERT INTO discovered_jobs VALUES ('old-id', ?, '[]')", (json.dumps(job.to_dict()),))
                db.execute("INSERT INTO discovery_analyses VALUES ('old-id', 'old-context', 'completed', ?)", (json.dumps(data),))

    def test_v3_jobs_and_scores_import_once_without_deleting_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite"
            self.legacy_database(path, candidate())
            memory = JobMemory(path)
            self.assertEqual(len(memory.library.list()), 1)
            job = memory.library.list()[0]
            self.assertEqual(job["jd"], candidate().jd)
            self.assertIsNotNone(job["overall_match"])
            self.assertEqual(job["recommendation"], rank_job(candidate(), report(candidate()))["category"])
            memory.library.set_status(job["job_id"], "applied")
            memory.library.add_note(job["job_id"], "旧岗位继续跟进")
            memory.close()
            memory = JobMemory(path)
            self.assertEqual(len(memory.library.list()), 1)
            self.assertEqual(memory.library.get(job["job_id"])["status"], "applied")
            self.assertEqual(memory.connection.execute("SELECT COUNT(*) FROM discovery_analyses").fetchone()[0], 1)
            self.assertEqual(len(memory.list_jobs()), 1)
            memory.close()

    def test_v1_missing_jd_is_marked_then_same_material_fills_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite"
            self.legacy_database(path, candidate(), include_discovery=False)
            memory = JobMemory(path)
            try:
                job = memory.library.list()[0]
                self.assertEqual(job["jd_kind"], "legacy_missing")
                self.assertEqual(job["jd"], "")
                memory.library.set_status(job["job_id"], "interview")
                memory.library.remember_analysis(candidate().analysis_text(), analyze_jd(candidate().analysis_text()), "人工材料")
                self.assertEqual(len(memory.library.list()), 1)
                self.assertEqual(memory.library.get(job["job_id"])["status"], "interview")
                self.assertEqual(memory.library.get(job["job_id"])["jd"], candidate().analysis_text())
                self.assertEqual(memory.library.get(job["job_id"])["city"], "广州")
            finally:
                memory.close()


class LibraryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        shutil.copytree(PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        for method in ("connect", "connect_ex"):
            guard = patch.object(socket.socket, method, side_effect=AssertionError("禁止真实网络"))
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.dict(os.environ, {}, clear=True)
        guard.start()
        self.addCleanup(guard.stop)
        self.runtime = JobRuntime(self.root, lambda event: None, demo=True)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_repeated_pipeline_keeps_interview_and_notes_without_new_analysis(self):
        first = await analyze_candidates(self.runtime, [candidate()], RESUME)
        job_id = first["ranked"][0]["job_id"]
        library = self.runtime.workspace.memory.library
        library.set_status(job_id, "applied")
        library.set_status(job_id, "interview")
        library.add_note(job_id, "待二面")
        before = library.get(job_id)
        with patch.object(self.runtime, "analyze", side_effect=AssertionError("重复不应分析")):
            second = await analyze_candidates(self.runtime, [candidate()], RESUME)
        self.assertEqual(second["new_analyses"], 0)
        self.assertEqual(second["ranked"][0]["job_id"], job_id)
        self.assertEqual(len(library.list()), 1)
        for field in ("status", "notes", "applied_at", "status_updated_at"):
            self.assertEqual(library.get(job_id)[field], before[field])

    async def test_substantive_jd_change_reanalyzes_but_keeps_same_job_and_application(self):
        first = await analyze_candidates(self.runtime, [candidate()], RESUME)
        job_id = first["ranked"][0]["job_id"]
        library = self.runtime.workspace.memory.library
        before = library.set_status(job_id, "applied")
        changed = candidate(jd=JD + "\n必须掌握Docker。")
        second = await analyze_candidates(self.runtime, [changed], RESUME)
        self.assertEqual(second["new_analyses"], 1)
        self.assertEqual(second["ranked"][0]["job_id"], job_id)
        after = library.get(job_id)
        self.assertEqual(after["status"], "applied")
        self.assertEqual(after["applied_at"], before["applied_at"])
        self.assertEqual(after["overall_match"], second["ranked"][0]["ranking"]["overall_match"])
        self.assertLess(after["overall_match"], first["ranked"][0]["ranking"]["overall_match"])
        self.assertEqual(len(library.list()), 1)

    async def test_single_jd_entry_provides_job_id_score_and_recommendation(self):
        text = candidate().analysis_text()
        data = await self.runtime.analyze(text, RESUME)
        library = self.runtime.workspace.memory.library
        job = library.get(data["job_id"])
        self.assertEqual(job["status"], "discovered")
        self.assertEqual(job["city"], "广州")
        self.assertIsNotNone(job["overall_match"])
        self.assertIsNotNone(job["recommendation"])
        library.set_status(job["job_id"], "applied")
        again = await self.runtime.analyze(text + "\n要求熟悉Docker。", RESUME)
        self.assertEqual(again["job_id"], data["job_id"])
        self.assertEqual(library.get(data["job_id"])["status"], "applied")

    async def test_search_only_records_discovered_without_score_or_analysis(self):
        with patch.object(self.runtime, "analyze", side_effect=AssertionError("仅搜索不分析")):
            await analyze_candidates(self.runtime, [candidate()], None, search_only=True)
        job = self.runtime.workspace.memory.library.list()[0]
        self.assertEqual(job["status"], "discovered")
        self.assertIsNone(job["overall_match"])

    async def test_city_override_explanation_is_saved_after_sorting(self):
        from test_job_agent_search import RankingTests
        helper = RankingTests()
        resume = "学历：本科在读\n掌握Python、Git\n正在学习MCP、SQL"
        gz = helper.ranking_row("广州", resume, good=False)
        sh = helper.ranking_row("上海", resume, skills="Python、Git")
        result = await analyze_candidates(self.runtime, [JobCandidate(**gz["candidate"]), JobCandidate(**sh["candidate"])], resume)
        self.assertEqual(result["ranked"][0]["candidate"]["city"], "上海")
        saved = self.runtime.workspace.memory.library.get(result["ranked"][0]["job_id"])
        self.assertEqual(saved["ranking"], result["ranked"][0]["ranking"])
        self.assertTrue(saved["ranking"]["city_override"]["overtaken"])

    async def test_management_cli_works_without_resume_or_skill_execution(self):
        DiscoveryStore(self.runtime.workspace.memory).remember(candidate())
        lines = ["/jobs", "/job 1", "/status 1 shortlisted", "/status 1 applied", "/status 1 interview",
                 "/note 1 已参加一面，等待反馈", "/jobs interview", "/funnel", "/quit"]
        output = io.StringIO()
        with patch("job_agent.cli.read_line", AsyncMock(side_effect=lines)), contextlib.redirect_stdout(output), \
                patch.object(self.runtime, "analyze", side_effect=AssertionError("管理不运行Skill")):
            await interactive(self.runtime)
        job = self.runtime.workspace.memory.library.get(1)
        self.assertEqual(job["status"], "interview")
        self.assertIsNotNone(job["applied_at"])
        self.assertEqual(job["notes"], "已参加一面，等待反馈")
        self.assertIn("筛选：面试（interview）", output.getvalue())
        self.assertIn("面试：1", output.getvalue())
        self.assertIn(candidate().jd, output.getvalue())

    async def test_cli_bad_command_arguments_and_unknown_ids_have_clear_errors(self):
        DiscoveryStore(self.runtime.workspace.memory).remember(candidate())
        lines = ["/status 1", "/status 1 nope", "/job 999", "/jobs nope", "/note 1", "/funnel extra", "/quit"]
        output = io.StringIO()
        with patch("job_agent.cli.read_line", AsyncMock(side_effect=lines)), contextlib.redirect_stdout(output):
            await interactive(self.runtime)
        self.assertEqual(output.getvalue().count("输入未处理："), 6)
        self.assertEqual(self.runtime.workspace.memory.library.get(1)["status"], "discovered")
        self.assertEqual(self.runtime.workspace.memory.library.get(1)["notes"], "")


if __name__ == "__main__":
    unittest.main()
