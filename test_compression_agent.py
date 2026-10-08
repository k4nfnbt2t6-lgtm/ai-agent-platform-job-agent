"""离线模型响应，真实运行 Session 和压缩流程；禁止实际网络连接。"""

import contextlib
import copy
import io
import json
import os
import socket
import unittest
from unittest.mock import patch

import httpx2
from agents import Agent, ModelSettings, OpenAIResponsesModel
from openai import APITimeoutError, AsyncOpenAI

import compression_agent as lesson
from session_agent import message_text
import test_agent as fixtures


def extract_facts(items):
    """离线模拟只依据请求收到的事实记录或摘要，不从答案变量补齐。"""
    facts = {}
    decoder = json.JSONDecoder()
    for item in items:
        text = message_text(item)
        for label in ("事实记录：", "事实更新：", "旧历史摘要："):
            if label in text:
                value, _ = decoder.raw_decode(text.split(label, 1)[1])
                facts.update(value)
                break
    return facts


class CompressionFlowTests(unittest.IsolatedAsyncioTestCase):
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
        self.example = lesson.Example(126, 296, 297, "offline000001")
        self.start_patch(patch("compression_agent.Example", return_value=self.example))
        self.requests = []

    def start_patch(self, patcher):
        value = patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        return value

    def mock_http(self, handler):
        def factory(**kwargs):
            return AsyncOpenAI(http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler), trust_env=False), **kwargs)
        return self.start_patch(patch("compression_agent.AsyncOpenAI", side_effect=factory))

    def history_handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertFalse(body["store"])
        self.assertFalse(body.get("tools"))
        self.assertEqual(body["reasoning"]["effort"], "none")
        self.assertEqual(body["max_output_tokens"], lesson.MAX_OUTPUT_TOKENS)
        self.assertNotIn("previous_response_id", body)
        self.assertNotIn("conversation", body)
        number = len(self.requests)
        self.assertLessEqual(number, 3)
        if body["instructions"] == lesson.SUMMARY_INSTRUCTIONS:
            self.assertEqual(number, 2)
            self.assertEqual(len(body["input"]), 1)
            old = json.loads(body["input"][0]["content"])
            self.assertEqual(len(old), 30)
            self.assertNotIn(lesson.FOLLOWUP_TASK, lesson.json_text(old))
            facts = extract_facts(old)
            answer = facts
        else:
            self.assertEqual(body["instructions"], lesson.AGENT_INSTRUCTIONS)
            self.assertEqual(body["input"][-1]["content"], lesson.FOLLOWUP_TASK)
            facts = extract_facts(body["input"])
            answer = {**facts, "sum": facts["a"] + facts["b"]}
            if number == 1:
                self.assertEqual(len(body["input"]), 35)
                self.assertIn("mock-check-", lesson.json_text(body["input"]))
            else:
                self.assertEqual(number, 3)
                self.assertEqual(len(body["input"]), 7)
                self.assertNotIn("mock-check-", lesson.json_text(body["input"]))
                self.assertEqual(body["input"][2:-1], lesson.simulated_history(self.example)[-4:])
        self.assertEqual(facts, self.example.facts)
        response = self.final_response(number, lesson.json_text(answer)).json()
        response["output"][0]["id"] = f"msg_compression_offline_{number}"
        inputs, outputs = (4000, 4200, 600)[number - 1], (200, 180, 200)[number - 1]
        response["usage"].update(input_tokens=inputs, output_tokens=outputs, total_tokens=inputs + outputs)
        return httpx2.Response(200, json=response)

    async def test_actual_requests_use_full_history_then_generated_summary_and_recent_turns(self):
        factory = self.mock_http(self.history_handler)
        with patch("compression_agent.RunConfig", wraps=lesson.RunConfig) as config:
            self.assertEqual(await lesson.main(prior_cost_rmb=0.001206), 0)
            config.assert_called_once_with(tracing_disabled=True)
        factory.assert_called_once_with(api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1",
                                        timeout=lesson.REQUEST_TIMEOUT_SECONDS, max_retries=0)
        self.assertEqual(len(self.requests), 3)
        output = self.stdout.getvalue()
        self.assertIn("Context Compression 验证： 通过", output)
        self.assertIn("Session 历史：34 → 6 个条目，14731 → 892 字节", output)
        self.assertIn("4000 → 600 token", output)
        self.assertIn('"sum":422', output)
        self.assertIn("使用你提供的账单金额", output)
        self.assertIn("本模块累计预算记录：约 0.036306 元", output)
        self.assertIn("累计模型请求次数（含此前一次超时）： 4", output)
        self.assertNotIn("local-test-placeholder", output + self.stderr.getvalue())

    async def test_stale_summary_is_not_adopted_and_no_third_request_occurs(self):
        def handler(request):
            response = self.history_handler(request).json()
            if len(self.requests) == 2:
                stale = {**self.example.facts, "b": self.example.old_b}
                response["output"][0]["content"][0]["text"] = lesson.json_text(stale)
            return httpx2.Response(200, json=response)
        self.mock_http(handler)
        self.assertEqual(await lesson.main(), 1)
        self.assertEqual(len(self.requests), 2)
        self.assertIn("未采用该摘要，原历史仍保留", self.stderr.getvalue())

    async def test_wrong_post_compression_answer_fails_without_retry(self):
        def handler(request):
            response = self.history_handler(request).json()
            if len(self.requests) == 3:
                wrong = {**self.example.expected_answer, "sum": 0}
                response["output"][0]["content"][0]["text"] = lesson.json_text(wrong)
            return httpx2.Response(200, json=response)
        self.mock_http(handler)
        self.assertEqual(await lesson.main(), 1)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("压缩后关键事实和和值正确：未通过", self.stdout.getvalue())

    async def test_verifier_rejects_full_raw_context_even_when_answer_is_correct(self):
        recorder = lesson.CompressionRecorder()
        async with AsyncOpenAI(api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.history_handler), trust_env=False)) as client:
            model = OpenAIResponsesModel("gpt-6-sol", openai_client=client)
            settings = ModelSettings(store=False, max_tokens=lesson.MAX_OUTPUT_TOKENS,
                                     reasoning={"effort": "none"})
            learner = Agent(name="Offline", instructions=lesson.AGENT_INSTRUCTIONS, model=model, model_settings=settings)
            summarizer = Agent(name="Offline Summarizer", instructions=lesson.SUMMARY_INSTRUCTIONS, model=model, model_settings=settings)
            data = await lesson.run_compression(learner, summarizer, recorder, self.example)
        self.assertTrue(all(lesson.verify_compression(data, recorder, self.example).values()))
        recorder.contexts[2] = recorder.contexts[0]
        checks = lesson.verify_compression(data, recorder, self.example)
        self.assertFalse(checks["压缩后请求只有摘要、近期历史和同一个新任务"])
        self.assertFalse(checks["原始模拟检查明细不再进入压缩后的请求"])

    async def test_affordable_high_usage_continues_without_claiming_token_reduction(self):
        def handler(request):
            response = self.history_handler(request).json()
            response["usage"].update(input_tokens=24_000, output_tokens=200, total_tokens=24_200)
            return httpx2.Response(200, json=response)
        self.mock_http(handler)
        self.assertEqual(await lesson.main(), 0)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("token 用量提醒", self.stdout.getvalue())
        self.assertIn("不声称实际 token 节省已验证", self.stdout.getvalue())

    async def test_projected_fee_risk_stops_before_summary_request(self):
        def handler(request):
            response = self.history_handler(request).json()
            response["usage"].update(input_tokens=150_000, total_tokens=150_200)
            return httpx2.Response(200, json=response)
        self.mock_http(handler)
        self.assertEqual(await lesson.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("超过已授权的 1 元预算", self.stderr.getvalue())

    async def test_api_error_is_redacted_and_not_retried(self):
        attempts = []
        def handler(request):
            attempts.append(request)
            return httpx2.Response(400, json={"error": {"message": "local-test-placeholder 接口不兼容", "type": "invalid_request_error"}})
        self.mock_http(handler)
        self.assertEqual(await lesson.main(), 1)
        self.assertEqual(len(attempts), 1)
        self.assertIn("HTTP 400", self.stderr.getvalue())
        self.assertNotIn("local-test-placeholder", self.stdout.getvalue() + self.stderr.getvalue())

    async def test_missing_or_unapproved_configuration_creates_no_client(self):
        for name, value in (("OPENAI_API_KEY", ""), ("OPENAI_BASE_URL", ""), ("OPENAI_BASE_URL", "https://example.invalid/v1")):
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}), \
                 patch("compression_agent.AsyncOpenAI") as factory:
                self.assertEqual(await lesson.main(), 1)
                factory.assert_not_called()

    async def test_unexpected_tool_call_stops_after_one_response(self):
        attempts = []
        def handler(request):
            attempts.append(request)
            return self.tool_response(["pwd"], 1)
        self.mock_http(handler)
        self.assertEqual(await lesson.main(), 1)
        self.assertEqual(len(attempts), 1)
        self.assertIn("没有开放工具", self.stderr.getvalue())

    async def test_timeout_makes_one_request_and_reports_unknown_new_cost(self):
        attempts = []
        def handler(request):
            attempts.append(request)
            raise APITimeoutError(request=request)
        factory = self.mock_http(handler)
        self.assertEqual(await lesson.main(prior_cost_rmb=0.02), 1)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(factory.call_args.kwargs["timeout"], 120.0)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertIn("请求等待超时", self.stderr.getvalue())
        self.assertIn("不能证明平台未处理或未计费", self.stderr.getvalue())
        self.assertIn("1 个请求未返回用量", self.stdout.getvalue())
        self.assertIn("此前超时请求预算记录：0.020000 元", self.stdout.getvalue())
        self.assertIn("累计模型请求次数（含此前一次超时）： 2", self.stdout.getvalue())

    async def test_prior_actual_cost_is_included_and_can_stop_before_any_new_request(self):
        self.mock_http(self.history_handler)
        self.assertEqual(await lesson.main(prior_cost_rmb=0.70), 1)
        self.assertEqual(self.requests, [])
        self.assertIn("超过已授权的 1 元预算", self.stderr.getvalue())
        self.assertIn("使用你提供的账单金额", self.stdout.getvalue())

    async def test_invalid_prior_cost_or_replay_tag_makes_no_client(self):
        invalid = [(float("nan"), None), (float("inf"), None), (-1, None), (0, "not-a-replay-tag")]
        for cost, tag in invalid:
            with self.subTest(cost=cost, tag=tag), patch("compression_agent.AsyncOpenAI") as factory:
                self.assertEqual(await lesson.main(prior_cost_rmb=cost, example_tag=tag), 1)
                factory.assert_not_called()


class CompressionBoundaryTests(unittest.TestCase):
    def test_replay_tag_reproduces_the_same_example_and_history(self):
        first = lesson.make_example("a1b2c3d4e5f6")
        second = lesson.make_example("a1b2c3d4e5f6")
        self.assertEqual(first, second)
        self.assertEqual(lesson.simulated_history(first), lesson.simulated_history(second))
        self.assertNotEqual(first.b, first.old_b)

    def test_split_keeps_complete_recent_turns_including_intermediate_items(self):
        history = lesson.simulated_history(lesson.Example(126, 296, 297, "offline000001"))
        extra = [{"type": "function_call", "call_id": "mock_one"},
                 {"type": "function_call_output", "call_id": "mock_one", "output": "mock data"}]
        history[-3:-3] = extra
        old, recent = lesson.split_history(history)
        self.assertEqual(len(old), 30)
        self.assertEqual(recent, history[30:])
        self.assertEqual(recent[1:3], extra)
        with self.assertRaises(ValueError):
            lesson.split_history(history, keep_recent_turns=0)
        with self.assertRaises(ValueError):
            lesson.split_history(history[-2:])

    def test_invalid_summary_does_not_modify_original_history(self):
        example = lesson.Example(126, 296, 297, "offline000001")
        history = lesson.simulated_history(example)
        old, recent = lesson.split_history(history)
        snapshot = copy.deepcopy((old, recent))
        summaries = ["not JSON", "null", lesson.json_text({k: v for k, v in example.facts.items() if k != "marker"}),
                     lesson.json_text({**example.facts, "test_policy": "可修改"}),
                     lesson.json_text({**example.facts, "b": example.old_b})]
        for summary in summaries:
            with self.subTest(summary=summary), self.assertRaises(lesson.LoopStopped):
                lesson.prepare_compressed_history(old, recent, summary, example.facts)
            self.assertEqual((old, recent), snapshot)


if __name__ == "__main__":
    unittest.main()
