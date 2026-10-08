"""模拟收费接口，真实运行 SDK Session；阻止实际网络连接。"""

import contextlib
import copy
import io
import json
import os
import re
import socket
import unittest
from unittest.mock import patch

import httpx2
from agents import Agent, ModelSettings, OpenAIResponsesModel, SQLiteSession
from openai import AsyncOpenAI

import session_agent
import test_agent as fixtures


class SessionFlowTests(unittest.IsolatedAsyncioTestCase):
    response = fixtures.AgentConfigurationTests.response
    final_response = fixtures.AgentConfigurationTests.final_response
    tool_response = fixtures.AgentConfigurationTests.tool_response

    def setUp(self):
        self.start_patch(patch.dict(os.environ, {
            "OPENAI_API_KEY": "local-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1",
        }, clear=True))
        for method in ("connect", "connect_ex"):
            self.start_patch(patch.object(socket.socket, method, side_effect=AssertionError("离线检查禁止网络连接")))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.start_patch(contextlib.redirect_stdout(self.stdout))
        self.start_patch(contextlib.redirect_stderr(self.stderr))
        self.example = session_agent.Example(137, 258, "lesson-offline123")
        self.start_patch(patch("session_agent.Example", return_value=self.example))
        self.requests = []

    def start_patch(self, patcher):
        value = patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        return value

    def mock_http(self, handler):
        def factory(**kwargs):
            return AsyncOpenAI(http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler), trust_env=False), **kwargs)
        return self.start_patch(patch("session_agent.AsyncOpenAI", side_effect=factory))

    def history_handler(self, request):
        """模拟回答只从这次实际 HTTP 请求的用户消息中查找数据。"""
        self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
        body = json.loads(request.content)
        self.requests.append(body)
        self.assertLessEqual(len(self.requests), 4)
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertFalse(body["store"])
        self.assertFalse(body.get("tools"))
        self.assertNotIn("previous_response_id", body)
        self.assertNotIn("conversation", body)
        self.assertEqual(body["instructions"], session_agent.INSTRUCTIONS)
        messages = [session_agent.message_text(item) for item in body["input"] if item.get("role") == "user"]
        if messages[-1] == self.example.first_task:
            answer = "已记录"
        else:
            answer = "信息不足"
            for message in messages[:-1]:
                match = re.fullmatch(r"本次练习的两个整数是 (\d+) 和 (\d+)，练习标记是 (\S+)。请先确认收到。", message)
                if match:
                    a, b, marker = match.groups()
                    answer = json.dumps({"sum": int(a) + int(b), "marker": marker})
        response = self.final_response(len(self.requests), answer).json()
        response["output"][0]["id"] = f"msg_offline_{len(self.requests)}"
        response["usage"].update(input_tokens=24_000, output_tokens=100, total_tokens=24_100)
        return httpx2.Response(200, json=response)

    async def test_four_http_requests_show_missing_context_then_full_session_history(self):
        factory = self.mock_http(self.history_handler)
        with patch("session_agent.RunConfig", wraps=session_agent.RunConfig) as config:
            self.assertEqual(await session_agent.main(), 0)
            config.assert_called_once_with(tracing_disabled=True)
        factory.assert_called_once_with(api_key="local-test-placeholder",
            base_url="https://api.likeai520.cc/v1", timeout=30.0, max_retries=0)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.requests[1]["input"], [{"role": "user", "content": session_agent.SECOND_TASK}])
        self.assertEqual([item.get("role") for item in self.requests[3]["input"]], ["user", "assistant", "user"])
        self.assertEqual(self.requests[3]["input"][0]["content"], self.example.first_task)
        self.assertIn('"sum": 395', self.stdout.getvalue())
        self.assertIn("Session / Context 对比验证： 通过", self.stdout.getvalue())
        self.assertIn("Session 已保存条目数： 4", self.stdout.getvalue())
        self.assertIn("token 用量提醒", self.stdout.getvalue())
        self.assertIn("$0.02940000", self.stdout.getvalue())
        self.assertNotIn("local-test-placeholder", self.stdout.getvalue() + self.stderr.getvalue())

    async def test_own_verification_rejects_answer_claim_if_actual_context_is_wrong(self):
        recorder = session_agent.SessionRecorder()
        async with AsyncOpenAI(api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.history_handler), trust_env=False)) as client:
            learning_agent = Agent(name="Offline", instructions=session_agent.INSTRUCTIONS,
                model=OpenAIResponsesModel("gpt-6-sol", openai_client=client), model_settings=ModelSettings(store=False))
            results, histories = await session_agent.run_comparison(learning_agent, recorder, self.example)
        self.assertTrue(all(session_agent.verify_comparison(results, histories, recorder, self.example).values()))
        original = copy.deepcopy(recorder.contexts)
        recorder.contexts[3] = recorder.contexts[1]
        checks = session_agent.verify_comparison(results, histories, recorder, self.example)
        self.assertFalse(checks["有 Session：第二轮确实带入完整上一轮历史"])
        recorder.contexts = original
        recorder.contexts[1] = histories[0] + recorder.contexts[1]
        self.assertFalse(session_agent.verify_comparison(results, histories, recorder, self.example)[
            "没有 Session：第二轮只收到新任务"])

    async def test_same_id_without_same_in_memory_storage_does_not_share_history(self):
        one = SQLiteSession("same_id", db_path=":memory:")
        two = SQLiteSession("same_id", db_path=":memory:")
        try:
            items = [{"role": "user", "content": self.example.first_task}]
            await one.add_items(items)
            self.assertEqual(await one.get_items(), items)
            self.assertEqual(await two.get_items(), [])
        finally:
            one.close()
            two.close()

    async def test_wrong_model_answer_fails_without_additional_requests(self):
        def handler(request):
            response = self.history_handler(request)
            if len(self.requests) == 4:
                body = response.json()
                body["output"][0]["content"][0]["text"] = '{"sum": 0, "marker": "invented"}'
                return httpx2.Response(200, json=body)
            return response
        self.mock_http(handler)
        self.assertEqual(await session_agent.main(), 1)
        self.assertEqual(len(self.requests), 4)
        self.assertIn("有 Session：模型正确返回和值及随机标记：未通过", self.stdout.getvalue())

    async def test_fee_risk_stops_before_second_http_request(self):
        def handler(request):
            body = self.history_handler(request).json()
            body["usage"].update(input_tokens=150_000, total_tokens=150_100)
            return httpx2.Response(200, json=body)
        self.mock_http(handler)
        self.assertEqual(await session_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("token 用量提醒", self.stdout.getvalue())
        self.assertIn("超过已授权的 1 元预算", self.stderr.getvalue())

    async def test_api_error_is_redacted_and_not_retried(self):
        attempts = []
        def handler(request):
            attempts.append(request)
            return httpx2.Response(400, json={"error": {"message": "local-test-placeholder 参数不兼容", "type": "invalid_request_error"}})
        self.mock_http(handler)
        self.assertEqual(await session_agent.main(), 1)
        self.assertEqual(len(attempts), 1)
        self.assertIn("HTTP 400", self.stderr.getvalue())
        self.assertNotIn("local-test-placeholder", self.stdout.getvalue() + self.stderr.getvalue())

    async def test_missing_or_unapproved_configuration_makes_no_client(self):
        cases = (("OPENAI_API_KEY", ""), ("OPENAI_BASE_URL", ""), ("OPENAI_BASE_URL", "https://example.invalid/v1"))
        for name, value in cases:
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}), \
                 patch("session_agent.AsyncOpenAI") as factory:
                self.assertEqual(await session_agent.main(), 1)
                factory.assert_not_called()

    async def test_unexpected_tool_call_stops_after_one_response(self):
        attempts = []
        def handler(request):
            attempts.append(request)
            return self.tool_response(["pwd"], 1)
        self.mock_http(handler)
        self.assertEqual(await session_agent.main(), 1)
        self.assertEqual(len(attempts), 1)
        self.assertIn("本节没有开放工具", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
