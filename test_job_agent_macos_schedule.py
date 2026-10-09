"""系统部署离线验收；不调用launchctl，不安装系统任务，不访问网络。"""

import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from job_agent.macos_schedule import (LABEL, checkpoint, dispatch_lock, install, launch_plist,
                                     prepare, read_config, run_once, uninstall, validate)
from job_agent.memory import JobMemory
from job_agent.search import JobCandidate
from job_agent.update_store import UpdateStore


class MacOSScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        shutil.copytree(Path(__file__).parent / "job_agent/skills", self.root / "job_agent/skills")
        (self.root / ".venv/bin").mkdir(parents=True)
        (self.root / ".venv/bin/python").write_text("offline-placeholder")
        (self.root / ".venv/bin/python").chmod(0o700)
        self.resume = self.root / "profile.md"
        self.resume.write_text("2027届本科在读，能源与动力工程。\nPython基础。\n项目：使用Python、Git完成Job Agent工具调用。")
        self.sources = self.root / "sources.json"
        self.sources.write_text(json.dumps([{"name": "人工公司", "url": "https://careers.example.com/", "kind": "html", "enabled": True, "search_city": "广州"}]))
        self.config = dict(version=1, resume=str(self.resume), sources=str(self.sources),
                           state_dir=str(self.root / ".agent-platform/jobs"), daily_at="09:00", search_limit=8,
                           analyze_limit=2, city_min=2)
        self.directory = self.root / ".agent-platform/deployment"
        self.config_path = prepare(self.config, self.directory, self.root)
        self.now = datetime.fromisoformat("2026-10-09T08:00:00+08:00")
        self.jd = "工作地点：广州\n岗位职责：开发AI Agent应用、工具调用和自动测试。\n任职要求：2027届本科在读，专业不限；熟悉Python。\n加分项：了解RAG。"
        self.search = AsyncMock(side_effect=self.search_result)
        for guard in (patch.dict(os.environ, {}, clear=True),
                      patch("job_agent.runtime.LLMAnalyzer", side_effect=AssertionError("不得付费")),
                      patch.object(socket.socket, "connect", side_effect=AssertionError("不得联网")),
                      patch.object(socket.socket, "connect_ex", side_effect=AssertionError("不得联网"))):
            guard.start()
            self.addCleanup(guard.stop)

    async def search_result(self, args):
        candidate = JobCandidate(company="人工科技", title="AI Agent开发实习", city="广州", recruitment_type="实习",
                                 jd=self.jd, source="人工来源", url="https://careers.example.com/1",
                                 detail_url="https://careers.example.com/1", discovered_at=self.now.isoformat())
        return {"candidates": [candidate], "issues": [], "http_requests": 0, "source_checks": []}

    async def run_dispatch(self):
        return await run_once(self.config, self.directory, self.root, clock=lambda: self.now, search=self.search)

    async def due(self):
        await self.run_dispatch()
        self.now += timedelta(hours=1)
        return await self.run_dispatch()

    def memory(self):
        return JobMemory(Path(self.config["state_dir"]) / "jobs.sqlite")

    def test_prepare_is_private_and_does_not_install(self):
        self.assertEqual(read_config(self.config_path, self.root), self.config)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        plist = plistlib.loads((self.directory / (LABEL + ".plist")).read_bytes())
        self.assertEqual(plist["StartCalendarInterval"], [{"Minute": i} for i in range(0, 60, 5)])
        self.assertTrue(plist["RunAtLoad"])
        self.assertNotIn("KeepAlive", plist)
        self.assertNotIn("EnvironmentVariables", plist)
        self.assertNotIn("--watch", plist["ProgramArguments"])
        self.assertEqual(plist["StandardOutPath"], "/dev/null")
        self.assertFalse((self.root / "Library").exists())

    def test_prepare_refuses_overwrite_and_outside_private_tree(self):
        with self.assertRaises(ValueError):
            prepare(self.config, self.directory, self.root)
        with self.assertRaises(ValueError):
            prepare(self.config, self.root / "public", self.root)

    def test_invalid_config_cannot_enable_llm_or_arbitrary_command(self):
        for changes in ({"mode": "llm"}, {"command": "echo secret"}, {"daily_at": "25:00"}, {"analyze_limit": 99}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate({**self.config, **changes}, self.root)

    def test_symlink_config_and_public_permissions_rejected(self):
        link = self.directory / "linked.json"
        link.symlink_to(self.config_path)
        with self.assertRaises(ValueError):
            read_config(link, self.root)
        self.config_path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_config(self.config_path, self.root)

    def test_status_can_read_config_after_resume_was_moved(self):
        self.resume.unlink()
        self.assertEqual(read_config(self.config_path, self.root, materials=False), self.config)
        with self.assertRaises(ValueError):
            read_config(self.config_path, self.root)

    async def test_before_due_does_not_search(self):
        self.assertEqual((await self.run_dispatch())["reason"], "not_due")
        self.search.assert_not_awaited()

    async def test_at_beijing_nine_checks_and_persists_summary(self):
        result = await self.due()
        self.assertEqual((result["status"], result["new_count"], result["analyzed_count"]), ("completed", 1, 1))
        self.assertEqual(result["model_requests"], 0)
        self.assertEqual(json.loads((self.directory / "dispatch.json").read_text()), result)
        memory = self.memory()
        self.addCleanup(memory.close)
        self.assertEqual(len(memory.library.list()), 1)
        self.assertEqual(UpdateStore(memory).schedule()["next_run_at"], "2026-10-10T01:00:00+00:00")

    async def test_duplicate_dispatch_does_not_search_again_same_day(self):
        await self.due()
        self.assertEqual((await self.run_dispatch())["reason"], "not_due")
        self.assertEqual(self.search.await_count, 1)

    async def test_sleep_many_days_catches_up_once_and_retains_status_notes(self):
        await self.due()
        memory = self.memory()
        memory.library.set_status(1, "interview")
        memory.library.add_note(1, "真实状态模拟：待二面")
        before = memory.library.get(1)
        memory.close()
        self.now += timedelta(days=4, hours=3)
        result = await self.run_dispatch()
        self.assertEqual(result["analyzed_count"], 0)
        self.assertEqual(self.search.await_count, 2)
        memory = self.memory()
        self.addCleanup(memory.close)
        self.assertEqual(len(memory.library.list()), 1)
        after = memory.library.get(1)
        for field in ("job_id", "status", "notes", "first_discovered_at", "status_updated_at"):
            self.assertEqual(before[field], after[field])
        self.assertEqual((await self.run_dispatch())["reason"], "not_due")

    async def test_changed_jd_reanalyzed_without_resetting_application(self):
        await self.due()
        memory = self.memory()
        memory.library.set_status(1, "applied")
        memory.library.add_note(1, "已投递")
        memory.close()
        self.jd += "\n任职要求：熟悉SQL。"
        self.now += timedelta(days=1)
        result = await self.run_dispatch()
        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(result["analyzed_count"], 1)
        memory = self.memory()
        self.addCleanup(memory.close)
        self.assertEqual(memory.library.get(1)["status"], "applied")
        self.assertEqual(memory.library.get(1)["notes"], "已投递")

    async def test_network_error_stops_until_next_day_and_redacts_logs(self):
        await self.run_dispatch()
        self.now += timedelta(hours=1)
        self.search.side_effect = RuntimeError("PRIVATE_RESUME_CONTENT secret-test-value")
        self.assertEqual((await self.run_dispatch())["status"], "failed")
        self.assertEqual((await self.run_dispatch())["reason"], "not_due")
        self.assertEqual(self.search.await_count, 1)
        text = (self.directory / "events.jsonl").read_text() + (self.directory / "dispatch.json").read_text()
        self.assertNotIn("PRIVATE_RESUME_CONTENT", text)
        self.assertNotIn("secret-test-value", text)

    async def test_restart_recovers_interrupted_record_without_network_retry(self):
        await self.run_dispatch()
        memory = self.memory()
        store = UpdateStore(memory)
        store.begin("scheduled", self.now)
        memory.close()
        checkpoint(self.directory, {"status": "running"})
        self.assertEqual((await self.run_dispatch())["reason"], "not_due")
        self.search.assert_not_awaited()
        memory = self.memory()
        self.addCleanup(memory.close)
        self.assertEqual(UpdateStore(memory).last_check()["status"], "interrupted")
        self.assertEqual(json.loads((self.directory / "dispatch.json").read_text())["status"], "interrupted")

    async def test_overlapping_system_dispatch_is_skipped(self):
        with dispatch_lock(self.directory):
            self.assertEqual((await self.run_dispatch())["reason"], "another_dispatch")
        self.search.assert_not_awaited()

    def test_install_requires_confirmation_and_rejects_tampered_plist(self):
        with patch("job_agent.macos_schedule.subprocess.run") as command:
            with self.assertRaises(ValueError):
                install(self.config_path, project=self.root, home=self.root)
            command.assert_not_called()
            path = self.directory / (LABEL + ".plist")
            data = plistlib.loads(path.read_bytes());data["KeepAlive"] = True
            path.write_bytes(plistlib.dumps(data))
            with self.assertRaises(ValueError):
                install(self.config_path, confirmed=True, project=self.root, home=self.root)
            command.assert_not_called()

    def test_mock_install_and_uninstall_only_own_task_preserves_data(self):
        with patch("job_agent.macos_schedule.sys.platform", "darwin"), patch("job_agent.macos_schedule.subprocess.run") as command:
            command.return_value.returncode = 0
            target = install(self.config_path, confirmed=True, home=self.root, project=self.root)
            self.assertTrue(target.exists())
            with self.assertRaises(ValueError):
                install(self.config_path, confirmed=True, home=self.root, project=self.root)
            with self.assertRaises(ValueError):
                uninstall(self.config_path, home=self.root, project=self.root)
            self.resume.unlink()
            uninstall(self.config_path, confirmed=True, home=self.root, project=self.root)
            self.assertFalse(target.exists())
            self.assertTrue(self.config_path.exists())
            self.assertEqual(command.call_args.args[0][1], "bootout")


if __name__ == "__main__":
    unittest.main()
