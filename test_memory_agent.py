"""离线验证 Memory；模拟模型响应，真实读写 JSON 和运行 SDK Session。"""

import ast
import contextlib
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
from openai import APITimeoutError, AsyncOpenAI

import memory_agent
from memory_tools import (EMPTY_MEMORY, MEMORY_KEY, MemoryGrant, MemoryStore,
                          json_text, prepare_memory_file)
import test_agent as fixtures


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    response = fixtures.AgentConfigurationTests.response
    final_response = fixtures.AgentConfigurationTests.final_response
    tool_response = fixtures.AgentConfigurationTests.tool_response

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix=".memory-tests-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        self.start_patch(patch("memory_agent.PROJECT_DIR", self.root))
        self.start_patch(patch.dict(os.environ, {
            "OPENAI_API_KEY": "local-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1",
        }, clear=True))
        for method in ("connect", "connect_ex"):
            self.start_patch(patch.object(socket.socket, method,
                side_effect=AssertionError("离线验证禁止实际网络连接")))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.start_patch(contextlib.redirect_stdout(self.stdout))
        self.start_patch(contextlib.redirect_stderr(self.stderr))
        self.example = memory_agent.Example("简体中文；先例子后代码；pref-offline-129", "session-only-offline-728")
        self.start_patch(patch("memory_agent.Example", return_value=self.example))
        self.requests = []

    def start_patch(self, patcher):
        value = patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        return value

    def mock_http(self, handler):
        def factory(**kwargs):
            return AsyncOpenAI(http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler), trust_env=False), **kwargs)
        return self.start_patch(patch("memory_agent.AsyncOpenAI", side_effect=factory))

    def flow_handler(self, request):
        """回答和工具参数仅取自这次实际请求的资料，不代替程序写记忆。"""
        self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
        body = json.loads(request.content)
        self.requests.append(body)
        number = len(self.requests)
        self.assertLessEqual(number, 5)
        self.assertEqual(body["instructions"], memory_agent.INSTRUCTIONS)
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertFalse(body["store"])
        self.assertFalse(body.get("parallel_tool_calls", False))
        self.assertNotIn("previous_response_id", body)
        self.assertNotIn("conversation", body)
        tools = [tool["name"] for tool in body.get("tools", [])]
        outputs = [item["output"] for item in body["input"] if item.get("type") == "function_call_output"]
        if tools == ["write_memory"]:
            if outputs:
                result = self.final_response(number, "已保存。")
            else:
                user = next(item["content"] for item in body["input"] if item.get("role") == "user")
                sample = json.loads(user.split("\n")[1])
                result = self.tool_response(None, number, name="write_memory",
                    arguments={"key": MEMORY_KEY, "value": sample["learning_preference"]})
        elif tools == ["read_memory"]:
            if not outputs:
                result = self.tool_response(None, number, name="read_memory", arguments={"key": MEMORY_KEY})
            else:
                # 本项目 SDK 将 dict 工具结果转成 Python 字典文本。
                memory = ast.literal_eval(outputs[-1])["memory"]
                result = self.final_response(number, json_text({
                    "preference": memory["value"] if memory else None, "temporary_marker": None}))
        else:
            self.assertEqual(tools, [])
            result = self.final_response(number, json_text({"preference": None, "temporary_marker": None}))
        payload = result.json()
        payload["usage"].update(input_tokens=24_000, output_tokens=100, total_tokens=24_100)
        return httpx2.Response(200, json=payload)

    def memory_file(self):
        return next(self.root.glob("agent_memory_*.json"))

    async def test_five_requests_write_and_read_in_fresh_sessions_without_old_history(self):
        factory = self.mock_http(self.flow_handler)
        with patch("memory_agent.RunConfig", wraps=memory_agent.RunConfig) as config:
            self.assertEqual(await memory_agent.main(), 0)
        factory.assert_called_once_with(api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1",
            timeout=120.0, max_retries=0)
        config.assert_called_once_with(tracing_disabled=True)
        self.assertEqual(len(self.requests), 5)
        only_query = [{"role": "user", "content": memory_agent.RECALL_TASK}]
        self.assertEqual(self.requests[2]["input"], only_query)
        self.assertEqual(self.requests[3]["input"], only_query)
        self.assertNotIn(self.example.preference, json_text(self.requests[2:4]))
        self.assertNotIn(self.example.temporary_marker, json_text(self.requests[2:]))
        actual = MemoryStore(self.root, self.memory_file()).snapshot()
        self.assertEqual(actual["memories"][0]["value"], self.example.preference)
        self.assertNotIn(self.example.temporary_marker, self.memory_file().read_text())
        output = self.stdout.getvalue() + self.stderr.getvalue()
        self.assertIn("Memory 跨 Session 验证： 通过", output)
        self.assertIn("$0.03675000", output)
        self.assertIn("token 用量提醒 5 次", output)
        self.assertNotIn("local-test-placeholder", output)

    async def test_verification_rejects_leaked_old_session_context_even_with_correct_answers(self):
        self.mock_http(self.flow_handler)
        captured = {}
        original = memory_agent.verify_memory
        def capture(data, recorder, example):
            captured.update(data=data, recorder=recorder, example=example)
            return original(data, recorder, example)
        with patch("memory_agent.verify_memory", side_effect=capture):
            self.assertEqual(await memory_agent.main(), 0)
        recorder = captured["recorder"]
        recorder.contexts[3].insert(0, {"role": "user", "content": self.example.first_task})
        checks = original(captured["data"], recorder, captured["example"])
        self.assertFalse(checks["新 Session 初始为空，首次请求只有当前任务"])
        self.assertFalse(checks["旧 Session 原文和临时口令没有进入新会话 Context"])

    async def test_separate_python_process_reads_memory_without_session_or_api(self):
        path = prepare_memory_file(self.root)
        store = MemoryStore(self.root, path, MemoryGrant(self.example.preference))
        self.assertTrue(store.write(MEMORY_KEY, self.example.preference)["ok"])
        script = ("import json, sys; from memory_tools import MemoryStore; "
                  "print(json.dumps(MemoryStore(sys.argv[1],sys.argv[2]).snapshot(),ensure_ascii=False))")
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.root), str(path)],
            cwd=Path(__file__).parent, env={}, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), store.snapshot())

    async def test_only_explicit_grant_is_saved_not_temporary_chat_or_guessed_preference(self):
        path = prepare_memory_file(self.root)
        writer = MemoryStore(self.root, path, MemoryGrant(self.example.preference))
        for key, value in (("temporary_marker", self.example.temporary_marker),
                (MEMORY_KEY, self.example.temporary_marker), (MEMORY_KEY, "模型猜测的另一项偏好")):
            self.assertFalse(writer.write(key, value)["ok"])
            self.assertEqual(writer.snapshot(), EMPTY_MEMORY)
        self.assertFalse(MemoryStore(self.root, path).write(MEMORY_KEY, self.example.preference)["ok"])
        self.assertTrue(writer.write(MEMORY_KEY, self.example.preference)["ok"])
        saved = writer.snapshot()
        self.assertFalse(writer.write(MEMORY_KEY, self.example.preference)["ok"])
        self.assertEqual(writer.snapshot(), saved)

    async def test_empty_and_invalid_memory_are_not_invented_or_repaired(self):
        path = Path(prepare_memory_file(self.root))
        store = MemoryStore(self.root, path)
        self.assertEqual(store.read(MEMORY_KEY), {"ok": True, "memory": None})
        self.assertFalse(store.read("unknown_key")["ok"])
        for text in ('not json', '{"version":1,"memories":[],"session_history":[]}',
                '{"version":true,"memories":[]}', '{"version":1,"memories":[{}]}'):
            path.write_text(text)
            self.assertFalse(store.read(MEMORY_KEY)["ok"])
            self.assertEqual(path.read_text(), text)

    async def test_store_refuses_outside_project_file_and_replaced_file(self):
        with self.assertRaises(ValueError):
            MemoryStore(self.root, Path(__file__))
        path = Path(prepare_memory_file(self.root))
        store = MemoryStore(self.root, path)
        replacement = Path(prepare_memory_file(self.root))
        replacement.replace(path)
        self.assertFalse(store.read(MEMORY_KEY)["ok"])

    async def test_wrong_recall_answer_fails_without_extra_requests_or_local_patch(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            if len(self.requests) == 5:
                payload["output"][0]["content"][0]["text"] = json_text({
                    "preference": "invented", "temporary_marker": self.example.temporary_marker})
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await memory_agent.main(), 1)
        self.assertEqual(len(self.requests), 5)
        self.assertIn("读取 Memory：正确返回完整偏好，临时口令仍未知：未通过", self.stdout.getvalue())
        self.assertEqual(MemoryStore(self.root, self.memory_file()).snapshot()["memories"][0]["value"],
                         self.example.preference)

    async def test_model_claim_without_memory_write_stops_before_new_sessions(self):
        def handler(request):
            self.requests.append(json.loads(request.content))
            return self.final_response(1, "已保存。")
        self.mock_http(handler)
        self.assertEqual(await memory_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(MemoryStore(self.root, self.memory_file()).snapshot(), EMPTY_MEMORY)
        self.assertIn("没有正确写入获准记忆", self.stderr.getvalue())

    async def test_model_unauthorized_memory_write_is_rejected_before_next_request(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            payload["output"][0]["arguments"] = json_text({"key": MEMORY_KEY, "value": self.example.temporary_marker})
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await memory_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(MemoryStore(self.root, self.memory_file()).snapshot(), EMPTY_MEMORY)
        self.assertIn("Memory 工具操作失败或被拒绝", self.stderr.getvalue())

    async def test_unexpected_tool_in_baseline_stops_without_overwriting_memory(self):
        def handler(request):
            response = self.flow_handler(request)
            if len(self.requests) == 3:
                payload = self.tool_response(None, 3, name="write_memory",
                    arguments={"key": MEMORY_KEY, "value": "changed"}).json()
                payload["usage"].update(input_tokens=100, output_tokens=30, total_tokens=130)
                return httpx2.Response(200, json=payload)
            return response
        self.mock_http(handler)
        self.assertEqual(await memory_agent.main(), 1)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("本阶段未开放的工具", self.stderr.getvalue())
        self.assertEqual(MemoryStore(self.root, self.memory_file()).snapshot()["memories"][0]["value"],
                         self.example.preference)

    async def test_fee_risk_stops_before_tools_and_followup_requests(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            payload["usage"].update(input_tokens=150_000, total_tokens=150_100)
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await memory_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(MemoryStore(self.root, self.memory_file()).snapshot(), EMPTY_MEMORY)
        self.assertIn("超过已授权的 1 元预算", self.stderr.getvalue())

    async def test_missing_or_unapproved_config_does_not_create_memory_or_client(self):
        for name, value in (("OPENAI_API_KEY", ""), ("OPENAI_BASE_URL", ""),
                            ("OPENAI_BASE_URL", "https://example.invalid/v1")):
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}), \
                    patch("memory_agent.AsyncOpenAI") as client, patch("memory_agent.prepare_memory_file") as prepare:
                self.assertEqual(await memory_agent.main(), 1)
                client.assert_not_called()
                prepare.assert_not_called()

    async def test_http_error_is_redacted_and_not_retried(self):
        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx2.Response(400, json={"error": {"message": "local-test-placeholder 参数不兼容", "type": "invalid_request_error"}})
        self.mock_http(handler)
        self.assertEqual(await memory_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("HTTP 400", self.stderr.getvalue())
        self.assertNotIn("local-test-placeholder", self.stdout.getvalue() + self.stderr.getvalue())

    async def test_timeout_once_reports_unknown_fee_and_preserves_file(self):
        def handler(request):
            self.requests.append(json.loads(request.content))
            raise APITimeoutError(request=request)
        self.mock_http(handler)
        self.assertEqual(await memory_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("不代表平台未处理或未扣费", self.stderr.getvalue())
        self.assertIn("费用请查看平台账单", self.stdout.getvalue())
        self.assertEqual(MemoryStore(self.root, self.memory_file()).snapshot(), EMPTY_MEMORY)


if __name__ == "__main__":
    unittest.main()
