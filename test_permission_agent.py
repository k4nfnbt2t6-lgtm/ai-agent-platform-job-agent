"""本课无收费检查：危险请求只走拒绝分支，命令执行用真实安全样例。"""

import ast
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from agents.exceptions import ModelBehaviorError
from agents.tool_context import ToolContext
from openai import APITimeoutError, AsyncOpenAI

import permission_agent
from permission_tools import INITIAL_CODE, EXPECTED_CODE, PermissionAccess, create_permission_tools, prepare_permission_files
import test_agent as fixtures
from terminal_tool import run_terminal


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    response = fixtures.AgentConfigurationTests.response
    tool_response = fixtures.AgentConfigurationTests.tool_response
    final_response = fixtures.AgentConfigurationTests.final_response

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix=".permission-check-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        self.start_patch(patch("permission_agent.PROJECT_DIR", self.root))
        self.start_patch(patch.dict(os.environ, {
            "OPENAI_API_KEY": "local-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1",
        }, clear=True))
        for method in ("connect", "connect_ex"):
            self.start_patch(patch.object(socket.socket, method, side_effect=AssertionError("离线检查禁止网络连接")))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.start_patch(contextlib.redirect_stdout(self.stdout))
        self.start_patch(contextlib.redirect_stderr(self.stderr))
        self.requests = []

    def start_patch(self, patcher):
        value = patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        return value

    def access(self):
        code, tests = prepare_permission_files(self.root)
        return PermissionAccess(self.root, code, tests)

    def mock_http(self, handler):
        def factory(**kwargs):
            return AsyncOpenAI(http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler), trust_env=False), **kwargs)
        return self.start_patch(patch("permission_agent.AsyncOpenAI", side_effect=factory))

    def flow_handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        number = len(self.requests)
        self.assertLessEqual(number, 6)
        self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertEqual(body["max_output_tokens"], 1024)
        self.assertFalse(body["parallel_tool_calls"])
        self.assertFalse(body["store"])
        self.assertEqual(body["reasoning"]["effort"], "none")
        self.assertEqual({t["name"] for t in body["tools"]}, {
            "read_file", "terminal", "write_file", "delete_file"})
        data = json.loads(body["input"][0]["content"].split("任务数据：", 1)[1])
        code = self.root / data["path"]
        self.assertEqual(code.read_text(), INITIAL_CODE, "批准前每一轮磁盘都应保持原文")
        actual = {item["call_id"]: ast.literal_eval(item["output"])
                  for item in body["input"] if item.get("type") == "function_call_output"}
        if number == 1:
            self.assertEqual(actual, {})
            result = self.tool_response(data["dangerous_probe"], number)
        elif number == 2:
            denied = actual["call_local_1"]
            self.assertEqual(denied["permission"]["decision"], "deny")
            self.assertFalse(denied["executed"])
            result = self.tool_response(None, number, "read_file", {"path": data["path"]})
        elif number == 3:
            self.assertEqual(actual["call_local_2"]["content"], INITIAL_CODE)
            result = self.tool_response(data["safe_command"], number)
        elif number == 4:
            pwd = actual["call_local_3"]
            self.assertEqual(pwd["returncode"], 0)
            self.assertEqual(pwd["stdout"].strip(), str(self.root))
            result = self.tool_response(data["test_command"], number)
        elif number == 5:
            tested = actual["call_local_4"]
            self.assertEqual(tested["returncode"], 0)
            self.assertIn("\nOK\n", tested["stderr"])
            old = actual["call_local_2"]["content"]
            result = self.tool_response(None, number, "write_file", {
                "path": data["path"], "content": old.replace("status: pending", "status: approved"),
                "expected_content": old})
        else:
            pending = actual["call_local_5"]
            self.assertEqual(pending["permission"]["decision"], "ask")
            self.assertFalse(pending["executed"])
            self.assertIn("-# permission status: pending\n+# permission status: approved\n",
                          pending["diff"]["unified_diff"])
            result = self.final_response(number, json.dumps({
                "dangerous_command": "denied", "safe_command": "executed",
                "tests": "passed", "write": "pending_confirmation"}))
        payload = result.json()
        payload["usage"].update(input_tokens=24_000, output_tokens=100, total_tokens=24_100)
        return httpx2.Response(200, json=payload)

    async def test_allow_deny_ask_and_human_approval_with_real_safe_execution(self):
        factory = self.mock_http(self.flow_handler)
        with patch("builtins.input", return_value="approve") as human, \
             patch("permission_agent.RunConfig", wraps=permission_agent.RunConfig) as config, \
             patch("permission_tools.run_terminal", wraps=run_terminal) as pwd, \
             patch("autotest_tools.run_terminal", wraps=run_terminal) as test:
            self.assertEqual(await permission_agent.main(), 0)
        self.assertEqual(len(self.requests), 6)
        human.assert_called_once()
        config.assert_called_once_with(tracing_disabled=True)
        factory.assert_called_once_with(api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1",
            timeout=120.0, max_retries=0)
        pwd.assert_called_once()
        self.assertEqual(pwd.call_args.args[0], ["pwd"])
        test.assert_called_once()
        self.assertEqual(test.call_args.args[0][:4], [sys.executable, "-I", "-B", "-c"])
        for execute in (pwd, test):
            self.assertEqual(execute.call_args.kwargs["env"], {})
            self.assertEqual(execute.call_args.kwargs["cwd"], self.root)
            self.assertEqual(execute.call_args.kwargs["timeout"], 3)
        self.assertEqual(next(self.root.glob("agent_permission_practice_*.py")).read_text(), EXPECTED_CODE)
        log = self.stdout.getvalue() + self.stderr.getvalue()
        self.assertIn("Permissions 权限管理验证： 通过", log)
        self.assertIn("$0.04410000", log)
        self.assertIn("token 用量提醒 6 次", log)
        self.assertNotIn("local-test-placeholder", log)

    async def test_human_rejection_keeps_file_and_is_a_valid_permission_result(self):
        self.mock_http(self.flow_handler)
        with patch("builtins.input", return_value=""):
            self.assertEqual(await permission_agent.main(), 0)
        self.assertEqual(len(self.requests), 6)
        self.assertEqual(next(self.root.glob("agent_permission_practice_*.py")).read_text(), INITIAL_CODE)
        self.assertIn("审批结果： rejected", self.stdout.getvalue())

    async def test_dangerous_commands_do_not_reach_any_executor(self):
        access = self.access()
        with patch("permission_tools.run_terminal") as first, patch("autotest_tools.run_terminal") as second:
            for command in (["rm", "-rf", access.backend.relative_path], ["sh", "-c", "pwd"],
                    ["pwd", ";", "rm"], ["python", "-c", "print(95)"], ["sudo", "anything"],
                    ["python", "-m", "unittest", "discover"], []):
                with self.subTest(command=command):
                    result = access.terminal(command)
                    self.assertEqual(result["permission"]["decision"], "deny")
                    self.assertFalse(result["executed"])
            first.assert_not_called()
            second.assert_not_called()
        self.assertEqual(access.command_attempts, [])

    async def test_outside_and_important_paths_denied_before_file_access(self):
        access = self.access()
        with patch.object(access.backend, "read_file") as read, \
             patch.object(access.backend, "snapshot") as snapshot, \
             patch.object(access.backend, "write_file") as write:
            for path in ("../permission_fake_private.txt", "/permission_fake_private.txt", "agent.py", ""):
                self.assertEqual(access.read_file(path)["permission"]["decision"], "deny")
                self.assertEqual(access.write_file(path, "simulated", "")["permission"]["decision"], "deny")
                self.assertEqual(access.delete_file(path)["permission"]["decision"], "deny")
            read.assert_not_called()
            snapshot.assert_not_called()
            write.assert_not_called()
        self.assertEqual(access.policy.check("unknown", path=access.backend.relative_path).decision, "deny")

    async def test_approval_is_specific_one_time_and_stale_content_not_overwritten(self):
        access = self.access()
        path = access.backend.relative_path
        access.read_file(path)
        proposal = access.write_file(path, EXPECTED_CODE, INITIAL_CODE)
        self.assertEqual(proposal["permission"]["decision"], "ask")
        self.assertEqual(access.backend.snapshot(), INITIAL_CODE)
        self.assertFalse(access.resolve_write("wrong-request-id", True)["ok"])
        self.assertIsNotNone(access.pending)
        access.backend.allowed_file.write_text(INITIAL_CODE + "# external harmless edit\n")
        result = access.resolve_write(proposal["request_id"], True)
        self.assertEqual(result["status"], "approved_but_not_written")
        self.assertFalse(result["executed"])
        self.assertTrue(access.backend.snapshot().endswith("# external harmless edit\n"))
        self.assertFalse(access.resolve_write(proposal["request_id"], True)["ok"])

    async def test_model_cannot_approve_itself_with_tool_argument(self):
        access = self.access()
        access.read_file(access.backend.relative_path)
        tools = create_permission_tools(access)
        self.assertNotIn("resolve_write", [tool.name for tool in tools])
        write = next(tool for tool in tools if tool.name == "write_file")
        self.assertNotIn("approved", write.params_json_schema["properties"])
        args = {"path": access.backend.relative_path, "content": EXPECTED_CODE,
                "expected_content": INITIAL_CODE, "approved": True}
        try:
            arguments = json.dumps(args)
            await write.on_invoke_tool(ToolContext(context=None, tool_name="write_file",
                tool_call_id="call_fake_approval", tool_arguments=arguments,
                run_config=permission_agent.RunConfig(tracing_disabled=True)), arguments)
        except ModelBehaviorError:
            pass
        self.assertEqual(access.backend.snapshot(), INITIAL_CODE)
        self.assertEqual(access.approvals, [])

    async def test_write_without_read_and_repeat_proposals_are_denied(self):
        access = self.access()
        name = access.backend.relative_path
        self.assertEqual(access.write_file(name, EXPECTED_CODE, INITIAL_CODE)["permission"]["decision"], "deny")
        self.assertEqual(access.audit[-1]["decision"], "deny")
        access.read_file(name)
        self.assertEqual(access.write_file(name, EXPECTED_CODE, INITIAL_CODE)["permission"]["decision"], "ask")
        self.assertEqual(access.write_file(name, EXPECTED_CODE, INITIAL_CODE)["permission"]["decision"], "deny")
        self.assertEqual(access.backend.snapshot(), INITIAL_CODE)

    async def test_modified_tests_and_unsafe_code_never_execute(self):
        access = self.access()
        with patch("autotest_tools.run_terminal") as execute:
            access.backend.tests.allowed_file.write_text("# changed fixed tests\n")
            self.assertFalse(access.terminal(access.backend.test_command)["executed"])
            access.backend.tests.allowed_file.write_text(access.backend.expected_tests)
            access.backend.allowed_file.write_text("import os\nprint(os.environ)\n")
            self.assertFalse(access.terminal(access.backend.test_command)["executed"])
            execute.assert_not_called()

    async def test_model_cannot_keep_using_tools_after_ask(self):
        def handler(request):
            response = self.flow_handler(request)
            if len(self.requests) == 6:
                return self.tool_response(["pwd"], 6)
            return response
        self.mock_http(handler)
        with patch("builtins.input") as human:
            self.assertEqual(await permission_agent.main(), 1)
            human.assert_not_called()
        self.assertEqual(len(self.requests), 6)
        self.assertIn("模型不能继续调用工具或自行批准", self.stderr.getvalue())
        self.assertEqual(next(self.root.glob("agent_permission_practice_*.py")).read_text(), INITIAL_CODE)

    async def test_false_success_report_does_not_open_approval_or_add_requests(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            if len(self.requests) == 6:
                payload["output"][0]["content"][0]["text"] = '{"write":"already_written"}'
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        with patch("builtins.input") as human:
            self.assertEqual(await permission_agent.main(), 1)
            human.assert_not_called()
        self.assertEqual(len(self.requests), 6)
        self.assertEqual(next(self.root.glob("agent_permission_practice_*.py")).read_text(), INITIAL_CODE)

    async def test_verifier_rejects_missing_feedback(self):
        captured = {}
        original = permission_agent.verify_model_run
        def capture(*args):
            captured["args"] = args
            return original(*args)
        self.mock_http(self.flow_handler)
        with patch("permission_agent.verify_model_run", side_effect=capture), patch("builtins.input", return_value=""):
            self.assertEqual(await permission_agent.main(), 0)
        result, recorder, access = captured["args"]
        recorder.model_inputs[5] = {}
        self.assertFalse(original(result, recorder, access)["包括拒绝和待确认在内的实际工具结果均交回模型"])

    async def test_cost_risk_stops_before_any_model_tool_executes(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            payload["usage"].update(input_tokens=150_000, output_tokens=100, total_tokens=150_100)
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        with patch("builtins.input") as human:
            self.assertEqual(await permission_agent.main(), 1)
            human.assert_not_called()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("超过已授权的 1 元预算", self.stderr.getvalue())
        self.assertIn("工具执行次数： 0", self.stdout.getvalue())

    async def test_missing_or_unauthorized_configuration_makes_no_client_or_file(self):
        with patch("permission_agent.AsyncOpenAI") as factory:
            for name, value in (("OPENAI_API_KEY", ""), ("OPENAI_BASE_URL", ""),
                    ("OPENAI_BASE_URL", "https://example.invalid/v1")):
                with patch.dict(os.environ, {name: value}):
                    self.assertEqual(await permission_agent.main(), 1)
            factory.assert_not_called()
        self.assertFalse(list(self.root.glob("agent_permission_*.py")))

    async def test_http_error_and_timeout_are_not_retried_or_logged_with_credentials(self):
        def error_handler(request):
            self.requests.append(request)
            return httpx2.Response(400, json={"error": {
                "message": "invalid local-test-placeholder", "type": "invalid_request_error"}})
        self.mock_http(error_handler)
        self.assertEqual(await permission_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("local-test-placeholder", self.stdout.getvalue() + self.stderr.getvalue())
        self.requests.clear()
        def timeout_handler(request):
            self.requests.append(request)
            raise APITimeoutError(request=request)
        self.mock_http(timeout_handler)
        self.assertEqual(await permission_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("请核对平台账单", self.stderr.getvalue())

    async def test_demo_has_no_model_and_labels_simulated_approval(self):
        with patch.dict(os.environ, {}, clear=True), patch("permission_agent.AsyncOpenAI") as factory, patch("builtins.input") as human:
            self.assertEqual(await permission_agent.main(demo=True), 0)
            factory.assert_not_called()
            human.assert_not_called()
        self.assertIn("模型请求和用户批准均模拟", self.stdout.getvalue())
        self.assertIn("本地权限边界验证： 通过", self.stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
