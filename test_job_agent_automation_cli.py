"""实际命令参数入口离线验收，无真实资料、付费、网络或系统调度安装。"""

import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_platform.terminal_ui import PROJECT
from job_agent.automation import __main__ as cli


class AutomationCLITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        shutil.copytree(PROJECT / "job_agent/skills", self.root / "job_agent/skills")
        self.guards = [patch.object(cli, "PROJECT", self.root), patch.dict(os.environ, {}, clear=True)]
        for name in ("connect", "connect_ex"):
            self.guards.append(patch.object(socket.socket, name, side_effect=AssertionError("CLI离线测试禁止网络")))
        for guard in self.guards:
            guard.start()

    async def asyncTearDown(self):
        for guard in self.guards:
            guard.stop()
        self.directory.cleanup()

    async def call(self, *argv):
        return await cli.run(cli.parser().parse_args(argv))

    async def test_mock_demo_and_repeat_use_same_real_entry(self):
        first = await self.call("demo", "--mode", "mock-llm")
        self.assertEqual(first["mode"], "mock_llm")
        self.assertEqual(first["paid_api_calls"], 0)
        self.assertEqual(first["http_requests"], 0)
        self.assertEqual(first["estimated_fee_rmb"], 0)
        self.assertEqual(first["applications"][0]["state"], "simulated_submitted")
        second = await self.call("demo", "--mode", "mock-llm")
        self.assertEqual(second["new_analyses"], 0)
        self.assertEqual(second["applications"][0]["state"], "already_recorded")

    async def test_local_demo_also_produces_materials_without_llm(self):
        with patch("job_agent.runtime.LLMConfig.from_env", side_effect=AssertionError("不应读取API配置")):
            result = await self.call("demo")
        self.assertEqual(result["mode"], "local")
        self.assertEqual(result["paid_api_calls"], 0)
        self.assertEqual(result["applications"][0]["state"], "simulated_submitted")

    async def test_demo_cannot_enable_paid_mode_or_real_resume(self):
        for args in (("--mode", "llm"), ("--resume", "private.pdf"), ("--allow-network",)):
            with self.subTest(args=args), self.assertRaises(PermissionError):
                await self.call("demo", *args)

    async def test_check_without_consent_cannot_load_keys_or_send_data(self):
        resume = self.root / "test.md"
        resume.write_text("人工2027届本科在读")
        with patch.object(cli.LLMConfig, "from_env", side_effect=AssertionError("未授权不可读取密钥")):
            with self.assertRaises(PermissionError):
                await self.call("check", "--mode", "llm", "--resume", str(resume))

    async def test_stop_status_and_confirmed_resume(self):
        await self.call("stop")
        self.assertTrue((await self.call("status"))["stopped"])
        with self.assertRaises(ValueError):
            await self.call("resume")
        await self.call("resume", "--confirm-resume")
        self.assertFalse((await self.call("status"))["stopped"])

    async def test_invalid_candidate_limit_has_clear_error_before_start(self):
        with self.assertRaisesRegex(ValueError, "候选岗位上限"):
            await self.call("demo", "--search-limit", "100")

    async def test_invalid_personal_resume_path_does_not_start_search(self):
        with self.assertRaises(ValueError):
            await self.call("check", "--resume", str(self.root / "missing.pdf"))


if __name__ == "__main__":
    unittest.main()
