"""平台集成与边界检查：模拟模型/HTTP，SDK、文件、SQLite 和任务真实运行。"""

import asyncio
import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
from openai import AsyncOpenAI

from agent_platform import runtime as module
from agent_platform import terminal_ui
from agent_platform.demo import DemoModel, tool
from agent_platform.gui import PlatformController
from agent_platform.terminal_ui import PROJECT, handle_line
from file_tools import INITIAL_CONTENT


class PlatformTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        shutil.copytree(PROJECT / "lab_skills", self.root / "lab_skills")
        self.events = []
        self.output = io.StringIO()
        self.addCleanup(self.directory.cleanup)
        for name in ("connect", "connect_ex"):
            p = patch.object(socket.socket, name, side_effect=AssertionError("禁止真实外网"))
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.runtime = module.PlatformRuntime(self.root, self.events.append)

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_repeated_tasks_and_context_restore_after_restart(self):
        result = await self.runtime.run_task("37 + 58")
        self.assertIn("95", result.final_output)
        record = self.runtime.recorder
        self.assertEqual(record.tool_calls[0]["output"], 95)
        self.assertEqual(list(record.model_inputs[1].values()), ["95"])
        await self.runtime.close()
        self.runtime = module.PlatformRuntime(self.root, self.events.append)
        result = await self.runtime.run_task("继续加 2")
        self.assertIn("97", result.final_output)
        for _ in range(8):
            self.assertIsNotNone(await self.runtime.run_task("继续加 1"))
        self.assertEqual(self.runtime.recorder.tool_calls[0]["output"], 105)
        self.assertTrue(any(e.kind == "context" for e in self.events))
        raw = await self.runtime.session.storage.get_items()
        shortened = await self.runtime.session.get_items()
        self.assertEqual(raw, shortened)  # 不再丢弃完整历史/工具证据
        calls = {x["call_id"] for x in shortened if x.get("type") == "function_call"}
        outputs = {x["call_id"] for x in shortened if x.get("type") == "function_call_output"}
        self.assertEqual(calls, outputs)
        self.assertEqual(self.runtime.usage()["estimated_rmb"], 0)

    async def test_explicit_memory_persists_across_session_and_process(self):
        self.assertIsNotNone(await self.runtime.run_task("记住：用简单中文解释"))
        await self.runtime.close()
        self.runtime = module.PlatformRuntime(self.root, self.events.append, session_id="another")
        result = await self.runtime.run_task("我的学习偏好是什么")
        self.assertIn("用简单中文解释", result.final_output)
        self.assertEqual(self.runtime.workspace.memory.snapshot()["memories"][0]["source"], "explicit_user_request")

    async def test_model_cannot_grant_itself_memory_write_or_overwrite(self):
        class UnaskedMemory(DemoModel):
            async def get_response(self, *args, **kwargs):
                return tool("write_memory", {"value": "伪造授权"})
        with patch.object(module, "DemoModel", UnaskedMemory):
            self.assertIsNone(await self.runtime.run_task("普通任务"))
        self.assertEqual(self.runtime.workspace.memory.snapshot()["memories"], [])
        await self.runtime.run_task("记住：用简单中文解释")
        self.assertIsNone(await self.runtime.run_task("记住：覆盖原记忆"))
        self.assertEqual(self.runtime.workspace.memory.snapshot()["memories"][0]["value"], "用简单中文解释")

    async def test_diff_requires_user_approval_and_consumes_exact_proposal_once(self):
        await self.runtime.run_task("把 note.txt 的状态改为 completed")
        proposal = self.runtime.workspace.pending
        self.assertIsNotNone(proposal)
        self.assertEqual(self.runtime.workspace.files["note.txt"].snapshot(), INITIAL_CONTENT)
        self.assertFalse(self.runtime.resolve_write("wrong", True)["ok"])
        self.assertIsNotNone(self.runtime.workspace.pending)
        self.assertIsNone(await self.runtime.run_task("37 + 58"))
        await handle_line(self.runtime, "/approve " + proposal.request_id)
        self.assertIn("status: completed", self.runtime.workspace.files["note.txt"].snapshot())
        self.assertFalse(self.runtime.resolve_write(proposal.request_id, True)["ok"])
        self.assertTrue(self.runtime.workspace.audit[-1]["executed"])
        result = await self.runtime.run_task("读取 note.txt")
        self.assertIn("completed", result.final_output)

    async def test_rejected_and_stale_proposals_never_write(self):
        await self.runtime.run_task("把 note.txt 的状态改为 completed")
        self.runtime.resolve_write(self.runtime.workspace.pending.request_id, False)
        self.assertEqual(self.runtime.workspace.files["note.txt"].snapshot(), INITIAL_CONTENT)
        await self.runtime.run_task("把 note.txt 的状态改为 completed")
        path = self.runtime.workspace.root / "note.txt"
        path.write_text("externally changed\n")
        self.assertFalse(self.runtime.resolve_write(self.runtime.workspace.pending.request_id, True)["ok"])
        self.assertEqual(path.read_text(), "externally changed\n")

    async def test_outside_paths_and_commands_are_rejected_before_access(self):
        workspace = self.runtime.workspace
        with patch("agent_platform.workspace.run_terminal") as execution:
            self.assertIsNone(workspace.terminal(["rm", "-rf", "/"])["returncode"])
            self.assertIsNone(workspace.terminal(["python", "../agent.py"])["returncode"])
            execution.assert_not_called()
        for name in ("../.env", ".env", "agent.py", str(self.root / "agent.py")):
            self.assertFalse(workspace.read(name)["ok"])
            self.assertFalse(workspace.propose(name, "fake", "")["ok"])

    async def test_existing_ast_guard_blocks_arbitrary_code(self):
        workspace = self.runtime.workspace
        original = workspace.read("practice.py")["content"]
        with self.assertRaises(ValueError):
            workspace.propose("practice.py", "import os\nprint(os.environ)\n", original)
        (workspace.root / "practice.py").write_text("import os\nprint(os.environ)\n")
        with patch("repair_tools.run_terminal") as execution:
            self.assertIsNone(workspace.terminal(["python", "practice.py"])["returncode"])
            execution.assert_not_called()

    async def test_skill_load_read_run_and_evidence(self):
        result = await self.runtime.run_task("检查 practice.py 的输出")
        self.assertEqual([r["name"] for r in self.runtime.recorder.tool_calls], ["load_skill", "read_file", "terminal"])
        report = json.loads(result.final_output)
        self.assertEqual(report["verdict"], "通过")
        self.assertEqual(report["actual_stdout"], "95\n")
        self.assertEqual(report["returncode"], 0)

    async def test_coding_workflow_sees_actual_failure_then_approved_repair_passes_tests(self):
        result = await self.runtime.run_task("修复测试样例")
        self.assertIsNotNone(result)
        records = self.runtime.recorder.tool_calls
        self.assertEqual([r["name"] for r in records], ["read_file", "terminal", "write_file"])
        self.assertEqual(records[1]["output"]["returncode"], 1)
        self.assertIn("AssertionError", records[1]["output"]["stderr"])
        self.assertFalse(self.runtime.workspace.propose("calculator_tests.py", "fake", "")["ok"])
        self.assertIn("return a - b", self.runtime.workspace.permission.backend.snapshot())
        approved = self.runtime.resolve_write(self.runtime.workspace.pending.request_id, True)
        self.assertTrue(approved["executed"])
        self.assertTrue(approved["tests"]["test_passed"])
        self.assertIn("OK", approved["tests"]["stderr"])

    async def test_background_and_scheduled_survive_between_user_tasks(self):
        await self.runtime.run_task("后台汇总 1,2,3")
        first = self.runtime.workspace.tasks()["tasks"][0]
        self.assertIn(first["status"], ("pending", "running"))
        self.assertIsNotNone(await self.runtime.run_task("37 + 58"))
        await self.runtime.run_task("0.05 秒后汇总 4,5,6")
        for manager in self.runtime.workspace.managers:
            task_id = next(iter(manager.jobs))
            result = await manager.get_result(task_id, wait=True)
            self.assertTrue(result["ready"])
        tasks = self.runtime.workspace.tasks()["tasks"]
        self.assertEqual([t["result"]["sum"] for t in tasks], [6, 15])
        self.assertGreaterEqual(tasks[1]["started_at"], tasks[1]["run_at"])

    async def test_exit_cancels_jobs_and_invalid_delay_never_schedules(self):
        with self.assertRaises(ValueError):
            await self.runtime.workspace.start_task([1], float("nan"))
        self.assertEqual(self.runtime.workspace.managers, [])
        await self.runtime.workspace.start_task([1], 5)
        manager = self.runtime.workspace.managers[0]
        await self.runtime.close()
        self.assertEqual(next(iter(manager.jobs.values())).status, "failed")
        self.assertTrue(all(timer.cancelled() for timer in manager.timers.values()))

    async def test_waiting_for_input_keeps_jobs_running_and_multiline_paste_is_preserved(self):
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "r", encoding="utf-8") as stream:
            with patch.object(terminal_ui.sys, "stdin", stream), patch.object(terminal_ui, "_input_buffer", bytearray()), \
                 patch.object(terminal_ui, "_input_eof", False), contextlib.redirect_stdout(self.output):
                task = asyncio.create_task(terminal_ui.read_line("你 > "))
                await self.runtime.workspace.start_task([1, 2], 0.01)
                await asyncio.sleep(0.15)
                self.assertFalse(task.done())
                self.assertEqual(self.runtime.workspace.tasks()["tasks"][0]["status"], "completed")
                os.write(write_fd, "第一行\n第二行\n".encode())
                self.assertEqual(await task, "第一行")
                self.assertEqual(await terminal_ui.read_line("你 > "), "第二行")
        os.close(write_fd)

    async def test_goal_uses_four_actual_evidence_steps(self):
        await self.runtime.run_task("核对目标：上午 200，下午 250，目标 500")
        goal = self.runtime.workspace.goals[0]
        self.assertEqual(goal.status, "complete")
        self.assertEqual(goal.snapshot()["completed_steps"], 4)
        self.assertEqual(goal.verified_report["total"], 450)
        self.assertFalse(goal.verified_report["met"])
        self.assertTrue(all(step["evidence"] for step in goal.steps))

    async def test_nested_sdk_agent_has_isolated_input_and_real_child_evidence(self):
        with contextlib.redirect_stdout(self.output):
            result = await self.runtime.run_task("委派汇总 report.json；主任务私有标记 parent-only")
        self.assertIsNotNone(result)
        child = self.runtime.child
        self.assertNotIn("parent-only", child.delegations[0]["child_input"])
        self.assertEqual(child.child_summary["total"], 400)
        self.assertEqual(len(child.child_run.raw_responses), 3)
        self.assertEqual(len(self.runtime.recorder.responses), 5)
        self.assertEqual([(r["agent"], r["name"]) for r in self.runtime.recorder.tool_calls],
                         [("Platform Main Agent", "delegate_summary"), ("Summary Subagent", "read_file"), ("Summary Subagent", "sum_numbers")])
        self.assertEqual(self.runtime.recorder.tool_calls[0]["output"]["summary"], child.child_summary)

    async def test_invalid_secret_input_never_saved_or_requested(self):
        for text in ("", "sk-" + "dummy" * 5, "API_KEY=placeholder", "密码：placeholder"):
            self.assertIsNone(await self.runtime.run_task(text))
        self.assertEqual(await self.runtime.session.storage.get_items(), [])
        self.assertEqual(self.runtime.usage()["requests"], 0)
        self.assertNotIn("placeholder", " ".join(e.text for e in self.events))

    async def test_credentials_in_sample_or_command_are_not_sent_or_saved(self):
        secret = "sk-" + "dummy" * 5
        (self.runtime.workspace.root / "note.txt").write_text(secret)
        self.assertIsNone(await self.runtime.run_task("读取 note.txt"))
        self.assertNotIn(secret, json.dumps(await self.runtime.session.storage.get_items()))
        self.assertFalse(self.runtime.workspace.propose("note.txt", secret, secret)["ok"])
        previous = self.runtime.session_id
        await handle_line(self.runtime, "/session " + secret)
        self.assertEqual(self.runtime.session_id, previous)

    async def test_demo_never_creates_network_client_and_redacts_controls(self):
        with patch.object(module, "AsyncOpenAI") as client:
            self.assertIsNotNone(await self.runtime.run_task("查看当前项目路径"))
            client.assert_not_called()
        self.assertEqual(self.runtime.recorder.tool_calls[0]["output"]["stdout"].strip(), str(self.root.resolve()))
        self.runtime.emit("final", "hello\x1b[2J\x00sk-placeholder")
        self.assertNotIn("\x1b", self.events[-1].text)
        self.assertNotIn("placeholder", self.events[-1].text)

    async def test_unknown_and_multiple_tool_calls_stop_before_execution(self):
        class BadModel(DemoModel):
            async def get_response(self, *args, **kwargs):
                result = tool("terminal", {"command": ["pwd"]})
                result.output += tool("add", {"a": 1, "b": 2}).output
                return result
        with patch.object(module, "DemoModel", BadModel), patch("agent_platform.workspace.run_terminal") as execute:
            self.assertIsNone(await self.runtime.run_task("测试多工具"))
            execute.assert_not_called()
        self.assertEqual(self.runtime.recorder.tool_calls, [])

    async def test_live_missing_authorization_and_invalid_url_fail_before_state(self):
        with patch.object(module, "Workspace") as workspace, patch.object(module, "AsyncOpenAI") as client:
            with self.assertRaises(ValueError):
                module.PlatformRuntime(self.root, demo=False)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "placeholder", "OPENAI_BASE_URL": "https://example.invalid/v1"}):
                with self.assertRaises(ValueError):
                    module.PlatformRuntime(self.root, demo=False, live_approved=True)
            workspace.assert_not_called()
            client.assert_not_called()

    async def test_mock_live_uses_same_sdk_and_cumulative_budget(self):
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
            self.assertEqual(body["model"], "gpt-6-sol")
            self.assertFalse(body["store"])
            self.assertFalse(body["parallel_tool_calls"])
            if len(requests) % 2:
                output = [{"type": "function_call", "id": "mock-call", "call_id": "live-" + str(len(requests)),
                           "name": "add", "arguments": '{"a":37,"b":58}', "status": "completed"}]
            else:
                self.assertIn("95", [x["output"] for x in body["input"] if x.get("type") == "function_call_output"])
                output = [{"type": "message", "id": "mock-message", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": "实际结果 95", "annotations": []}]}]
            return httpx2.Response(200, json={"id": "response-" + str(len(requests)), "object": "response", "created_at": 0,
                "status": "completed", "model": "gpt-6-sol", "output": output,
                "usage": {"input_tokens": 24000, "output_tokens": 40, "total_tokens": 24040}})
        def factory(**kwargs):
            self.assertEqual(kwargs["max_retries"], 0)
            return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler), trust_env=False), **kwargs)
        await self.runtime.close()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "local-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1"}), \
             patch.object(module, "AsyncOpenAI", side_effect=factory), \
             patch.object(module, "RunConfig", wraps=module.RunConfig) as config:
            self.runtime = module.PlatformRuntime(self.root, self.events.append, demo=False, live_approved=True)
            for _ in range(3):
                self.assertIsNotNone(await self.runtime.run_task("37 + 58"))
            self.assertIsNone(await self.runtime.run_task("37 + 58"))
            self.assertEqual(len(requests), 6)
            self.assertTrue(all(x.kwargs["tracing_disabled"] for x in config.call_args_list))
            self.assertAlmostEqual(self.runtime.usage()["estimated_rmb"], 0.4356)

    async def test_symlink_state_and_linked_samples_are_rejected(self):
        target = self.root / "other"
        target.mkdir()
        (self.root / "linked").symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            module.PlatformRuntime(self.root, state_dir="linked")
        sample = self.runtime.workspace.root / "note.txt"
        sample.unlink()
        sample.symlink_to(self.root / "private.txt")
        self.assertFalse(self.runtime.workspace.read("note.txt")["ok"])


class GuiControllerTests(unittest.TestCase):
    def test_gui_shares_platform_loop_for_session_tasks_and_diff(self):
        with tempfile.TemporaryDirectory(prefix="platform-check-", dir=PROJECT) as directory:
            controller = PlatformController(SimpleNamespace(state_dir=directory, session="gui-test"))
            try:
                with patch("socket.socket.connect", side_effect=AssertionError("禁止外网")):
                    self.assertTrue(controller.submit("37 + 58")[0])
                    controller.future.result(timeout=5)
                    self.assertTrue(any("95" in e["text"] for e in controller.snapshot()["events"] if e["kind"] == "final"))
                    self.assertTrue(controller.submit("把 note.txt 的状态改为 completed")[0])
                    controller.future.result(timeout=5)
                    proposal = controller.runtime.workspace.pending
                    self.assertEqual(controller.runtime.workspace.files["note.txt"].snapshot(), INITIAL_CONTENT)
                    controller.submit("/approve " + proposal.request_id)
                    controller.future.result(timeout=5)
                    self.assertIn("completed", controller.runtime.workspace.files["note.txt"].snapshot())
            finally:
                controller.close()
            self.assertFalse(controller.worker.is_alive())


if __name__ == "__main__":
    unittest.main()
