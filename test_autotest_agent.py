"""离线验证本节：模拟模型响应，真实运行 Python 和 unittest，不访问收费 API。"""

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
import autotest_agent
import test_agent as fixtures
from autotest_tools import BROKEN_CODE, AutoTestAccess, prepare_autotest_files
from terminal_tool import run_terminal


class AutoTestFlowTests(unittest.IsolatedAsyncioTestCase):
    response = fixtures.AgentConfigurationTests.response
    tool_response = fixtures.AgentConfigurationTests.tool_response
    final_response = fixtures.AgentConfigurationTests.final_response

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix=".autotest-check-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        self.start_patch(patch("autotest_agent.PROJECT_DIR", self.root))
        self.start_patch(patch.dict(os.environ, {
            "OPENAI_API_KEY": "local-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1",
        }, clear=True))
        for method in ("connect", "connect_ex"):
            self.start_patch(patch.object(socket.socket, method, side_effect=AssertionError("离线检查禁止网络连接")))
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
        return self.start_patch(patch("autotest_agent.AsyncOpenAI", side_effect=factory))

    def paths(self):
        code = list(self.root.glob("agent_test_practice_*.py"))
        tests = list(self.root.glob("agent_test_cases_*.py"))
        self.assertEqual((len(code), len(tests)), (1, 1))
        return code[0], tests[0]

    @staticmethod
    def actual_outputs(body):
        return {item["call_id"]: ast.literal_eval(item["output"])
                for item in body["input"] if item.get("type") == "function_call_output"}

    async def test_model_tools_receive_real_test_failure_then_real_passing_result(self):
        requests = []
        fixed = BROKEN_CODE.replace("return a - b", "return a + b")
        original_tests = None

        def handler(request):
            nonlocal original_tests
            self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
            body = json.loads(request.content)
            requests.append(body)
            number = len(requests)
            self.assertLessEqual(number, 6)
            self.assertEqual(body["model"], "gpt-6-sol")
            self.assertEqual(body["max_output_tokens"], agent.MAX_OUTPUT_TOKENS)
            self.assertEqual(body["reasoning"]["effort"], "none")
            self.assertEqual(body.get("tool_choice", "auto"), "auto")
            self.assertFalse(body["parallel_tool_calls"])
            self.assertEqual({item["name"] for item in body["tools"]}, {"read_file", "write_file", "terminal"})
            code, tests = self.paths()
            outputs = self.actual_outputs(body)
            self.assertEqual(len(outputs), number - 1)
            test_command = ["python", "-m", "unittest", tests.stem, "-v"]
            if number == 1:
                original_tests = tests.read_text()
                return self.tool_response(None, number, "read_file", {"path": code.name})
            original = outputs["call_local_1"]
            self.assertEqual(original["content"], BROKEN_CODE)
            self.assertEqual(original["test_content"], original_tests)
            self.assertIn("self.assertEqual(add(37, 58), 95)", original_tests)
            if number == 2:
                return self.tool_response(["python", code.name], number)
            running = outputs["call_local_2"]
            self.assertEqual((running["returncode"], running["stdout"], running["stderr"]), (0, "-21\n", ""))
            if number == 3:
                return self.tool_response(test_command, number)
            failure = outputs["call_local_3"]
            self.assertEqual(failure["returncode"], 1)
            self.assertIn("AssertionError: -21 != 95", failure["stderr"])
            self.assertIn("FAILED (failures=1)", failure["stderr"])
            self.assertIn(tests.name, failure["stderr"])
            if number == 4:
                self.assertEqual(code.read_text(), BROKEN_CODE)
                return self.tool_response(None, number, "write_file", {
                    "path": code.name, "content": fixed, "expected_content": original["content"],
                })
            self.assertEqual(code.read_text(), fixed)
            self.assertEqual(tests.read_text(), original_tests)
            self.assertTrue(outputs["call_local_4"]["ok"])
            if number == 5:
                return self.tool_response(test_command, number)
            success = outputs["call_local_5"]
            self.assertEqual(success["returncode"], 0)
            self.assertIn("Ran 1 test", success["stderr"])
            self.assertIn("\nOK\n", success["stderr"])
            return self.final_response(number, f"{code.name} 已修复，测试通过，37+58=95；能运行还需验证结果是否正确。")

        def metered_handler(request):
            body = handler(request).json()
            body["usage"].update(input_tokens=24_000, output_tokens=100, total_tokens=24_100)
            return httpx2.Response(200, json=body)

        factory = self.mock_http(metered_handler)
        with patch("autotest_tools.run_terminal", wraps=run_terminal) as execution, \
             patch("autotest_agent.RunConfig", wraps=autotest_agent.RunConfig) as config:
            self.assertEqual(await autotest_agent.main(), 0)
            self.assertEqual(execution.call_count, 3)
            for call in execution.call_args_list:
                self.assertEqual(call.kwargs["env"], {})
                self.assertEqual(call.kwargs["cwd"], self.root)
                self.assertEqual(call.kwargs["timeout"], 3)
            config.assert_called_once_with(tracing_disabled=True)
        factory.assert_called_once_with(api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1",
                                        timeout=30.0, max_retries=0)
        self.assertEqual(len(requests), 6)
        self.assertIn("自动测试循环验证： 通过", self.stdout.getvalue())
        self.assertIn("固定测试内容未改变： True", self.stdout.getvalue())
        self.assertIn("工具执行次数： 5", self.stdout.getvalue())
        self.assertIn("token 用量提醒", self.stdout.getvalue())
        self.assertIn("$0.04410000", self.stdout.getvalue())
        self.assertIn("已知费用约 0.0000 元", self.stdout.getvalue())

    async def test_model_cannot_make_tests_pass_by_editing_the_test(self):
        requests = []
        original_tests = None

        def handler(request):
            nonlocal original_tests
            requests.append(request)
            code, tests = self.paths()
            if len(requests) == 1:
                original_tests = tests.read_text()
                return self.tool_response(None, 1, "read_file", {"path": code.name})
            return self.tool_response(None, 2, "write_file", {
                "path": tests.name, "content": BROKEN_CODE.replace("a - b", "a + b"),
                "expected_content": BROKEN_CODE,
            })

        self.mock_http(handler)
        self.assertEqual(await autotest_agent.main(), 1)
        self.assertEqual(len(requests), 2)
        code, tests = self.paths()
        self.assertEqual(code.read_text(), BROKEN_CODE)
        self.assertEqual(tests.read_text(), original_tests)
        self.assertIn("文件操作失败", self.stderr.getvalue())

    async def test_repeated_actions_stop_at_five_tools_and_six_requests(self):
        requests = []

        def handler(request):
            requests.append(request)
            code, _ = self.paths()
            return self.tool_response(["python", code.name], len(requests))

        self.mock_http(handler)
        with patch("autotest_tools.run_terminal", wraps=run_terminal) as execution:
            self.assertEqual(await autotest_agent.main(), 1)
            self.assertEqual(execution.call_count, 5)
        self.assertEqual(len(requests), 6)
        self.assertIn("5 次工具执行上限", self.stderr.getvalue())

    async def test_extreme_usage_stops_for_cost_risk_before_tool_execution(self):
        requests = []

        def handler(request):
            requests.append(request)
            code, _ = self.paths()
            body = self.tool_response(None, 1, "read_file", {"path": code.name}).json()
            body["usage"].update(input_tokens=150_000, output_tokens=1, total_tokens=150_001)
            return httpx2.Response(200, json=body)

        self.mock_http(handler)
        with patch.object(AutoTestAccess, "read_file") as read:
            self.assertEqual(await autotest_agent.main(), 1)
            read.assert_not_called()
        self.assertEqual(len(requests), 1)
        self.assertIn("费用预估", self.stderr.getvalue())
        self.assertIn("需你确认", self.stderr.getvalue())

    async def test_missing_configuration_creates_no_practice_or_client(self):
        os.environ.pop("OPENAI_API_KEY")
        with patch("autotest_agent.AsyncOpenAI") as factory:
            self.assertEqual(await autotest_agent.main(), 1)
            factory.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_api_error_is_not_retried(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx2.Response(400, json={"error": {"message": "模拟接口错误", "type": "invalid_request_error"}})

        self.mock_http(handler)
        self.assertEqual(await autotest_agent.main(), 1)
        self.assertEqual(len(requests), 1)
        self.assertIn("HTTP 400", self.stderr.getvalue())


class AutoTestSafetyTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix=".autotest-safety-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        code, tests = prepare_autotest_files(self.root)
        self.access = AutoTestAccess(self.root, code, tests)

    def test_changed_assertion_or_replaced_test_is_not_executed(self):
        access = self.access
        access.read_file(access.relative_path)
        access.tests.allowed_file.write_text(access.expected_tests.replace("), 95)", "), -21)"))
        with patch("autotest_tools.run_terminal") as execution:
            self.assertIsNone(access.run(access.test_command)["returncode"])
            self.assertFalse(access.read_file(access.relative_path)["ok"])
            self.assertFalse(access.write_file(access.relative_path, BROKEN_CODE.replace("a - b", "a + b"), BROKEN_CODE)["ok"])
            execution.assert_not_called()

    def test_other_files_commands_and_unsafe_python_are_rejected(self):
        access = self.access
        important = self.root / "agent.py"
        important.write_text("important sentinel")
        original_tests = access.test_snapshot()
        access.read_file(access.relative_path)
        with patch("autotest_tools.run_terminal") as execution:
            for command in (["python", "agent.py"], ["python", "-m", "unittest", "discover"],
                            ["sh", "-c", "pwd"], ["python", access.relative_path, "extra"]):
                self.assertIsNone(access.run(command)["returncode"])
            for path in ("agent.py", "../outside.py", access.tests.relative_path):
                self.assertFalse(access.write_file(path, BROKEN_CODE, BROKEN_CODE)["ok"])
            for source in ("import os\nos.remove('agent.py')\n", BROKEN_CODE.replace("a - b", "95")):
                self.assertFalse(access.write_file(access.relative_path, source, BROKEN_CODE)["ok"])
            execution.assert_not_called()
        self.assertEqual(important.read_text(), "important sentinel")
        self.assertEqual(access.snapshot(), BROKEN_CODE)
        self.assertEqual(access.test_snapshot(), original_tests)


if __name__ == "__main__":
    unittest.main()
