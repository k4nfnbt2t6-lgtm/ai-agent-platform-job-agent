"""离线检查修复循环：模拟模型，真实运行专用 Python 文件，不访问 API。"""

import ast
import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from openai import AsyncOpenAI

import agent
import repair_agent
import test_agent as fixtures
from repair_tools import BROKEN_CONTENT, RepairFileAccess, prepare_repair_file
from terminal_tool import run_terminal


class RepairFlowTests(unittest.IsolatedAsyncioTestCase):
    response = fixtures.AgentConfigurationTests.response
    tool_response = fixtures.AgentConfigurationTests.tool_response
    final_response = fixtures.AgentConfigurationTests.final_response

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix=".repair-test-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        self.start_patch(patch("repair_agent.PROJECT_DIR", self.root))
        self.start_patch(patch.dict(os.environ, {
            "OPENAI_API_KEY": "local-test-placeholder",
            "OPENAI_BASE_URL": "https://api.likeai520.cc/v1",
        }, clear=True))
        for method in ("connect", "connect_ex"):
            self.start_patch(patch.object(socket.socket, method, side_effect=AssertionError("禁止实际网络连接")))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.start_patch(contextlib.redirect_stdout(self.stdout))
        self.start_patch(contextlib.redirect_stderr(self.stderr))

    def start_patch(self, patcher):
        value = patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        return value

    def mock_http(self, handler):
        def factory(**kwargs):
            transport = httpx2.MockTransport(handler)
            return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=transport, trust_env=False), **kwargs)
        return self.start_patch(patch("repair_agent.AsyncOpenAI", side_effect=factory))

    def practice_path(self):
        files = list(self.root.glob("agent_repair_*.py"))
        self.assertEqual(len(files), 1)
        return files[0]

    @staticmethod
    def actual_outputs(body):
        return {
            item["call_id"]: ast.literal_eval(item["output"])
            for item in body["input"] if item.get("type") == "function_call_output"
        }

    async def test_complete_repair_feeds_actual_python_error_and_success_into_model(self):
        requests = []
        fixed = BROKEN_CONTENT.replace("a + c", "a + b")
        original_file = prepare_repair_file(self.root)
        original_identity = (original_file.stat().st_dev, original_file.stat().st_ino)

        def handler(request):
            self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
            body = json.loads(request.content)
            requests.append(body)
            number = len(requests)
            self.assertLessEqual(number, 5)
            self.assertEqual(body["model"], "gpt-6-sol")
            self.assertEqual(body["max_output_tokens"], agent.MAX_OUTPUT_TOKENS)
            self.assertEqual(body["reasoning"]["effort"], "none")
            self.assertEqual(body.get("tool_choice", "auto"), "auto")
            self.assertFalse(body["parallel_tool_calls"])
            self.assertEqual({item["name"] for item in body["tools"]}, {"read_file", "write_file", "terminal"})
            file = self.practice_path()
            outputs = self.actual_outputs(body)
            self.assertEqual(len(outputs), number - 1)
            if number == 1:
                return self.tool_response(None, number, "read_file", {"path": file.name})
            self.assertEqual(outputs["call_local_1"]["content"], BROKEN_CONTENT)
            if number == 2:
                return self.tool_response(["python", file.name], number)
            failure = outputs["call_local_2"]
            self.assertEqual(failure["returncode"], 1)
            self.assertEqual(failure["stdout"], "")
            self.assertIn("NameError: name 'c' is not defined", failure["stderr"])
            self.assertIn(file.name, failure["stderr"])
            self.assertEqual(failure["source"], BROKEN_CONTENT)
            if number == 3:
                self.assertEqual(file.read_text(), BROKEN_CONTENT)
                return self.tool_response(None, number, "write_file", {
                    "path": file.name, "content": fixed,
                    "expected_content": outputs["call_local_1"]["content"],
                })
            self.assertTrue(outputs["call_local_3"]["ok"])
            self.assertEqual(file.read_text(), fixed)
            if number == 4:
                return self.tool_response(["python", file.name], number)
            self.assertEqual(outputs["call_local_4"]["returncode"], 0)
            self.assertEqual(outputs["call_local_4"]["stdout"], "95\n")
            self.assertEqual(outputs["call_local_4"]["stderr"], "")
            return self.final_response(number, f"{file.name} 的 NameError 已修复，真实输出是 95。")

        def metered_handler(request):
            body = handler(request).json()
            body["usage"].update(input_tokens=23_877, output_tokens=78, total_tokens=23_955)
            return httpx2.Response(200, json=body)

        factory = self.mock_http(metered_handler)
        with (
            patch("repair_tools.run_terminal", wraps=run_terminal) as execution,
            patch("repair_agent.RunConfig", wraps=repair_agent.RunConfig) as config,
            patch("repair_agent.prepare_repair_file") as create,
        ):
            self.assertEqual(await repair_agent.main(
                resume_file=original_file.name, previous_input_tokens=23_877, previous_output_tokens=78,
            ), 0)
            create.assert_not_called()
            self.assertEqual(execution.call_count, 2)
            for call, source in zip(execution.call_args_list, (BROKEN_CONTENT, fixed)):
                self.assertEqual(call.kwargs["env"], {})
                self.assertEqual(call.kwargs["input_text"], source)
                self.assertEqual(call.kwargs["cwd"], self.root)
                self.assertEqual(call.kwargs["timeout"], 3)
            config.assert_called_once_with(tracing_disabled=True)
        factory.assert_called_once_with(
            api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1", timeout=30.0, max_retries=0,
        )
        self.assertEqual(len(requests), 5)
        self.assertIn("自动修复循环验证： 通过", self.stdout.getvalue())
        self.assertIn("工具执行次数： 4", self.stdout.getvalue())
        self.assertIn("模型请求次数： 5", self.stdout.getvalue())
        self.assertIn("本模块累计模型请求次数： 6", self.stdout.getvalue())
        self.assertIn("输入 143262 token，输出 468 token", self.stdout.getvalue())
        self.assertIn("$0.04368060", self.stdout.getvalue())
        self.assertIn("token 用量提醒", self.stdout.getvalue())
        self.assertEqual((original_file.stat().st_dev, original_file.stat().st_ino), original_identity)

    async def test_extreme_input_stops_for_projected_fee_risk_not_token_limit(self):
        requests = []

        def handler(request):
            requests.append(request)
            file = self.practice_path()
            body = self.tool_response(None, 1, "read_file", {"path": file.name}).json()
            body["usage"]["input_tokens"] = 150_000
            body["usage"]["total_tokens"] = 150_001
            return httpx2.Response(200, json=body)

        self.mock_http(handler)
        with patch.object(RepairFileAccess, "read_file") as read:
            self.assertEqual(await repair_agent.main(), 1)
            read.assert_not_called()
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.practice_path().read_text(), BROKEN_CONTENT)
        self.assertIn("费用预估", self.stderr.getvalue())
        self.assertIn("需你确认", self.stderr.getvalue())
        self.assertIn("token 用量提醒", self.stdout.getvalue())
        self.assertIn("150000", self.stdout.getvalue())
        self.assertIn("工具执行次数： 0", self.stdout.getvalue())

    async def test_single_high_input_round_continues_repair_when_cost_is_affordable(self):
        requests = []

        def handler(request):
            requests.append(request)
            number = len(requests)
            file = self.practice_path()
            if number == 1:
                return self.tool_response(None, number, "read_file", {"path": file.name})
            if number == 2:
                return self.tool_response(["python", file.name], number)
            if number == 3:
                body = self.tool_response(None, number, "write_file", {
                    "path": file.name, "content": BROKEN_CONTENT.replace("a + c", "a + b"),
                    "expected_content": BROKEN_CONTENT,
                }).json()
                body["usage"]["input_tokens"] = 50_000
                body["usage"]["total_tokens"] = 50_001
                return httpx2.Response(200, json=body)
            if number == 4:
                return self.tool_response(["python", file.name], number)
            return self.final_response(number, f"{file.name} 已修复并实际输出 95。")

        self.mock_http(handler)
        self.assertEqual(await repair_agent.main(), 0)
        self.assertEqual(len(requests), 5)
        self.assertEqual(self.practice_path().read_text(), BROKEN_CONTENT.replace("a + c", "a + b"))
        self.assertIn("NameError", self.stdout.getvalue())
        self.assertIn("token 用量提醒", self.stdout.getvalue())
        self.assertIn("自动修复循环验证： 通过", self.stdout.getvalue())

    async def test_budget_reservation_blocks_request_before_sending(self):
        requests = []
        self.mock_http(lambda request: requests.append(request))
        with patch("agent.BUDGET_USD", 0.001):
            self.assertEqual(await repair_agent.main(), 1)
        self.assertEqual(requests, [])
        self.assertIn("预算", self.stderr.getvalue())

    async def test_prior_cost_is_included_before_authorizing_continuation(self):
        file = prepare_repair_file(self.root)
        requests = []
        self.mock_http(lambda request: requests.append(request))
        self.assertEqual(await repair_agent.main(
            resume_file=file.name, previous_input_tokens=300_000, previous_output_tokens=0,
        ), 1)
        self.assertEqual(requests, [])
        self.assertEqual(file.read_text(), BROKEN_CONTENT)
        self.assertIn("费用预估", self.stderr.getvalue())
        self.assertIn("本模块累计模型请求次数： 1", self.stdout.getvalue())

    async def test_resume_rejects_other_files_and_changed_practice_without_overwriting(self):
        file = prepare_repair_file(self.root)
        file.write_text(BROKEN_CONTENT.replace("a + c", "a + b"))
        with patch("repair_agent.AsyncOpenAI") as factory, patch("repair_agent.prepare_repair_file") as create:
            for requested in ("agent.py", "../" + file.name, str(file), file.name):
                self.assertEqual(await repair_agent.main(resume_file=requested), 1)
            factory.assert_not_called()
            create.assert_not_called()
        self.assertEqual(file.read_text(), BROKEN_CONTENT.replace("a + c", "a + b"))

    async def test_missing_usage_stops_without_claiming_free_run(self):
        requests = []

        def handler(request):
            requests.append(request)
            body = self.final_response().json()
            body.pop("usage")
            return httpx2.Response(200, json=body)

        self.mock_http(handler)
        self.assertEqual(await repair_agent.main(), 1)
        self.assertEqual(len(requests), 1)
        self.assertIn("未返回可核对", self.stderr.getvalue())
        self.assertNotIn("$0.00000000", self.stdout.getvalue())

    async def test_http_error_does_not_retry_or_change_endpoint(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx2.Response(400, json={"error": {"message": "模拟兼容性错误", "type": "invalid_request_error"}})

        self.mock_http(handler)
        self.assertEqual(await repair_agent.main(), 1)
        self.assertEqual(len(requests), 1)
        self.assertIn("HTTP 400", self.stderr.getvalue())

    async def test_absent_configuration_creates_no_file_or_client(self):
        os.environ.pop("OPENAI_API_KEY")
        with patch("repair_agent.AsyncOpenAI") as factory:
            self.assertEqual(await repair_agent.main(), 1)
            factory.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_blocked_command_does_not_execute_or_continue_loop(self):
        requests = []

        def handler(request):
            requests.append(request)
            return self.tool_response(["python", "agent.py"], 1)

        self.mock_http(handler)
        with patch("repair_tools.run_terminal") as execution:
            self.assertEqual(await repair_agent.main(), 1)
            execution.assert_not_called()
        self.assertEqual(len(requests), 1)

    async def test_timeout_stops_even_when_program_errors_are_allowed(self):
        requests = []

        def handler(request):
            requests.append(request)
            return self.tool_response(["python", self.practice_path().name], 1)

        self.mock_http(handler)
        with patch("repair_tools.run_terminal", return_value={
            "returncode": None, "stdout": "", "stderr": "模拟运行超时",
        }) as execution:
            self.assertEqual(await repair_agent.main(), 1)
            execution.assert_called_once()
        self.assertEqual(len(requests), 1)


class RepairCodeSafetyTests(unittest.TestCase):
    def test_unsafe_code_and_other_commands_never_reach_process_execution(self):
        with tempfile.TemporaryDirectory(prefix=".repair-safety-", dir=Path(__file__).parent) as directory:
            root = Path(directory).resolve()
            file = prepare_repair_file(root)
            access = RepairFileAccess(root, file)
            access.read_file(file.name)
            with patch("repair_tools.run_terminal") as execution:
                unsafe = (
                    "import os\nos.remove('agent.py')\n",
                    "print(open('agent.py').read())\n",
                    "while True: pass\n",
                    "a = 37\nb = 58\nprint(a.__class__)\n",
                )
                for source in unsafe:
                    self.assertFalse(access.write_file(file.name, source, BROKEN_CONTENT)["ok"])
                self.assertEqual(file.read_text(), BROKEN_CONTENT)
                for command in (["sh", "-c", "pwd"], ["python", "agent.py"], ["python", str(file)], ["python", file.name, "extra"]):
                    self.assertIsNone(access.run_python(command)["returncode"])
                file.write_text(unsafe[0])
                self.assertIsNone(access.run_python(["python", file.name])["returncode"])
                execution.assert_not_called()


if __name__ == "__main__":
    unittest.main()
