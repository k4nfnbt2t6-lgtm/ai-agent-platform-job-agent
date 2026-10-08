"""离线测试：模拟模型响应，真实操作专用临时文件，不访问任何 API 服务。"""

import contextlib
import ast
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from agents.tool_context import ToolContext
from openai import AsyncOpenAI

import agent
import tools
from file_tools import EXPECTED_CONTENT, INITIAL_CONTENT, FileAccess
from terminal_tool import run_terminal


class AgentConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 练习文件放在项目内的测试目录，结束后仅清理这些测试文件。
        workspace = tempfile.TemporaryDirectory(prefix=".file-tools-test-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.start_patch(patch("agent.PROJECT_DIR", Path(workspace.name).resolve()))
        # 此值不是有效密钥；真实环境变量不会用于测试。
        self.start_patch(patch.dict(os.environ, {
            "OPENAI_API_KEY": "local-test-placeholder",
            "OPENAI_BASE_URL": "https://api.likeai520.cc/v1",
        }, clear=True))
        for method in ("connect", "connect_ex"):
            self.start_patch(patch.object(
                socket.socket, method,
                side_effect=AssertionError("离线测试禁止建立网络连接"),
            ))
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.start_patch(contextlib.redirect_stdout(self.stdout))
        self.start_patch(contextlib.redirect_stderr(self.stderr))

    def start_patch(self, patcher):
        value = patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        return value

    def mock_http(self, handler):
        def create_client(**kwargs):
            http_client = httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler), trust_env=False,
            )
            return AsyncOpenAI(http_client=http_client, **kwargs)

        return self.start_patch(patch("agent.AsyncOpenAI", side_effect=create_client))

    def response(self, output, number=1):
        return httpx2.Response(200, json={
            "id": f"resp_local_{number}",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": "gpt-6-sol",
            "output": output,
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        })

    def final_response(self, number=1, text="尚未查看目录。"):
        return self.response([{
            "id": "msg_local",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{
                "type": "output_text", "text": text, "annotations": [],
            }],
        }], number)

    def tool_response(self, command, number, name="terminal", arguments=None):
        return self.response([{
            "id": f"fc_local_{number}",
            "type": "function_call",
            "call_id": f"call_local_{number}",
            "name": name,
            "arguments": json.dumps({"command": command} if arguments is None else arguments),
            "status": "completed",
        }], number)

    async def check_missing(self, name, value=None):
        if value is None:
            os.environ.pop(name)
        else:
            os.environ[name] = value
        with patch("agent.AsyncOpenAI") as factory:
            self.assertEqual(await agent.main(), 1)
            factory.assert_not_called()
        self.assertIn(name, self.stderr.getvalue())
        self.assertNotIn("local-test-placeholder", self.stderr.getvalue())

    async def test_missing_key_stops_before_client_creation(self):
        await self.check_missing("OPENAI_API_KEY")

    async def test_blank_key_stops_before_client_creation(self):
        await self.check_missing("OPENAI_API_KEY", "   ")

    async def test_missing_base_url_does_not_fall_back_to_openai(self):
        await self.check_missing("OPENAI_BASE_URL")

    async def test_blank_base_url_stops_before_client_creation(self):
        await self.check_missing("OPENAI_BASE_URL", "   ")

    async def test_loop_feeds_actual_tool_outputs_into_next_requests(self):
        requests = []
        practice_path = None

        def handler(request):
            nonlocal practice_path
            self.assertEqual(request.method, "POST")
            self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
            self.assertEqual(request.headers["authorization"], "Bearer local-test-placeholder")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "gpt-6-sol")
            self.assertEqual(body["max_output_tokens"], agent.MAX_OUTPUT_TOKENS)
            self.assertEqual(body["reasoning"]["effort"], "none")
            # SDK 后续轮次可省略此字段；API 的默认值仍是 auto。
            self.assertEqual(body.get("tool_choice", "auto"), "auto")
            self.assertFalse(body["parallel_tool_calls"])
            self.assertEqual({tool["name"] for tool in body["tools"]}, {"add", "terminal", "read_file", "write_file"})
            requests.append(body)
            number = len(requests)
            self.assertLessEqual(number, 3, "Loop must not issue a fourth request")
            outputs = {
                item["call_id"]: ast.literal_eval(item["output"])
                for item in body["input"] if item.get("type") == "function_call_output"
            }
            if number == 1:
                self.assertEqual(outputs, {})
                files = list(agent.PROJECT_DIR.glob("agent_practice_*.txt"))
                self.assertEqual(len(files), 1)
                practice_path = files[0]
                return self.tool_response(None, number, "read_file", {"path": practice_path.name})
            self.assertTrue(outputs["call_local_1"]["ok"])
            original = outputs["call_local_1"]["content"]
            self.assertEqual(original, INITIAL_CONTENT)
            if number == 2:
                self.assertEqual(original, practice_path.read_text())
                self.assertEqual(set(outputs), {"call_local_1"})
                return self.tool_response(None, number, "write_file", {
                    "path": practice_path.name,
                    "content": original.replace("status: pending", "status: completed", 1),
                    "expected_content": original,
                })
            self.assertEqual(set(outputs), {"call_local_1", "call_local_2"})
            self.assertTrue(outputs["call_local_2"]["ok"])
            self.assertEqual(outputs["call_local_2"]["content"], EXPECTED_CONTENT)
            self.assertEqual(practice_path.read_text(), EXPECTED_CONTENT)
            return self.final_response(number, f"{practice_path.name} 的 status 已修改为 completed。")

        factory = self.mock_http(handler)
        with (
            patch("agent.RunConfig", wraps=agent.RunConfig) as config,
            patch("tools.run_terminal") as execution,
        ):
            self.assertEqual(await agent.main(), 0)
            config.assert_called_once_with(tracing_disabled=True)
            execution.assert_not_called()
        factory.assert_called_once_with(
            api_key="local-test-placeholder",
            base_url="https://api.likeai520.cc/v1",
            timeout=30.0,
            max_retries=0,
        )
        self.assertEqual(len(requests), 3)
        self.assertIn("文件读写工具验证： 通过", self.stdout.getvalue())
        self.assertIn("第 2 轮模型请求：带入 1 条工具结果", self.stdout.getvalue())
        self.assertIn("第 3 轮模型请求：带入 2 条工具结果", self.stdout.getvalue())
        self.assertIn("模型请求次数： 3", self.stdout.getvalue())
        self.assertIn("工具执行次数： 2", self.stdout.getvalue())
        self.assertIn("status: completed", self.stdout.getvalue())

    async def test_no_tool_call_is_reported_without_retry(self):
        requests = []

        def handler(request):
            requests.append(request)
            return self.final_response()

        self.mock_http(handler)
        with patch("tools.run_terminal") as execution:
            self.assertEqual(await agent.main(), 1)
            execution.assert_not_called()
        self.assertEqual(len(requests), 1)
        self.assertIn("文件读写工具验证： 未通过", self.stdout.getvalue())

    async def test_disallowed_command_does_not_reach_subprocess(self):
        requests = []

        def handler(request):
            requests.append(request)
            return self.response([{
                "id": "fc_blocked",
                "type": "function_call",
                "call_id": "call_blocked",
                "name": "terminal",
                "arguments": '{"command":["sh","-c","pwd"]}',
                "status": "completed",
            }])

        self.mock_http(handler)
        with patch("tools.run_terminal") as execution:
            self.assertEqual(await agent.main(), 1)
            execution.assert_not_called()
        self.assertEqual(len(requests), 1)
        self.assertIn("命令未执行", self.stdout.getvalue())
        self.assertIn("文件读写工具验证： 未通过", self.stdout.getvalue())
        self.assertIn("工具失败或命令被拒绝", self.stderr.getvalue())

    async def test_extra_tool_is_stopped_after_two_executions(self):
        requests = []

        def handler(request):
            requests.append(request)
            self.assertLessEqual(len(requests), 3)
            return self.tool_response(["pwd"], len(requests))

        self.mock_http(handler)
        with patch("tools.run_terminal", wraps=run_terminal) as execution:
            self.assertEqual(await agent.main(), 1)
            self.assertEqual(execution.call_count, 2)
        self.assertEqual(len(requests), 3)
        self.assertIn("2 次工具执行上限", self.stderr.getvalue())

    async def test_input_over_old_byte_limit_is_allowed_with_affordable_cost(self):
        requests = []

        def handler(request):
            requests.append(request)
            return self.final_response()

        self.mock_http(handler)
        teaching_context = agent.TASK + "必要的教学上下文。" * 600
        with patch("agent.TASK", teaching_context), patch("tools.run_terminal") as execution:
            self.assertEqual(await agent.main(), 1)
            execution.assert_not_called()
        self.assertEqual(len(requests), 1)
        self.assertGreater(len(requests[0].content), 10_000)
        self.assertNotIn("字节上限", self.stderr.getvalue())

    async def test_failed_tool_stops_before_another_model_request(self):
        requests = []

        def handler(request):
            requests.append(request)
            return self.tool_response(["pwd"], len(requests))

        self.mock_http(handler)
        with patch("tools.run_terminal", return_value={
            "returncode": 1, "stdout": "", "stderr": "模拟执行失败",
        }) as execution:
            self.assertEqual(await agent.main(), 1)
            execution.assert_called_once()
        self.assertEqual(len(requests), 1)
        self.assertIn("工具失败", self.stderr.getvalue())

    async def test_unauthorized_base_url_is_rejected(self):
        os.environ["OPENAI_BASE_URL"] = "https://configured.invalid/custom/v1"
        with patch("agent.AsyncOpenAI") as factory:
            self.assertEqual(await agent.main(), 1)
            factory.assert_not_called()

    async def test_simulated_unsupported_endpoint_is_not_retried_or_changed(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx2.Response(404, json={
                "error": {"message": "Simulated unsupported Responses endpoint", "type": "not_found"},
            })

        self.mock_http(handler)
        self.assertEqual(await agent.main(), 1)
        self.assertIn("HTTP 404", self.stderr.getvalue())
        self.assertEqual(len(requests), 1)


class TerminalToolSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_paths_shell_and_write_commands_are_blocked(self):
        commands = [
            ["ls", ".."], ["ls", "/"], ["ls", "-la"],
            ["env"], ["touch", "learning-test-marker"], ["ls; pwd"], [],
        ]
        with patch("tools.run_terminal") as execution:
            for command in commands:
                with self.subTest(command=command):
                    arguments = json.dumps({"command": command})
                    result = await tools.terminal.on_invoke_tool(
                        ToolContext(
                            context=None,
                            tool_name="terminal",
                            tool_call_id="call_local_safety",
                            tool_arguments=arguments,
                            run_config=agent.RunConfig(tracing_disabled=True),
                        ),
                        arguments,
                    )
                    self.assertIsNone(result["returncode"])
                    self.assertEqual(result["stdout"], "")
                    self.assertIn("命令未执行", result["stderr"])
            execution.assert_not_called()

    async def test_cwd_is_passed_to_the_real_command(self):
        target = Path(__file__).resolve().parent.parent
        result = run_terminal(["pwd"], cwd=target)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"].strip(), str(target))


class ImportSafetyTests(unittest.TestCase):
    def test_import_does_not_start_agent_or_require_credentials(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c", (
                "import socket; from unittest.mock import patch; "
                "guard = patch.object(socket.socket, 'connect', "
                "side_effect=AssertionError('Network forbidden')); guard.start(); "
                "import agent; print('import-ok')"
            )],
            cwd=Path(__file__).parent,
            env={},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "import-ok")


if __name__ == "__main__":
    unittest.main()
