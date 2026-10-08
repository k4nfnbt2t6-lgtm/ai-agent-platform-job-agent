"""模拟模型响应，后台 worker、状态、计算和时间关系均真实执行；禁止网络。"""

import ast
import asyncio
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
from openai import APITimeoutError, AsyncOpenAI

import background_agent
from background_tools import BackgroundTasks, create_background_tools
import test_agent as fixtures


class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    response = fixtures.AgentConfigurationTests.response
    tool_response = fixtures.AgentConfigurationTests.tool_response
    final_response = fixtures.AgentConfigurationTests.final_response

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix=".background-check-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        self.start_patch(patch("background_agent.PROJECT_DIR", self.root))
        self.start_patch(patch.dict(os.environ, {
            "OPENAI_API_KEY": "local-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1",
        }, clear=True))
        for method in ("connect", "connect_ex"):
            self.start_patch(patch.object(socket.socket, method, side_effect=AssertionError("离线检查禁止网络连接")))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.start_patch(contextlib.redirect_stdout(self.stdout))
        self.start_patch(contextlib.redirect_stderr(self.stderr))
        self.requests = []
        self.managers = []
        original = BackgroundTasks

        def factory(*args, **kwargs):
            # 仅缩短离线样例 worker 延迟；真实命令仍为四秒。
            kwargs.setdefault("delay", 0.15)
            manager = original(*args, **kwargs)
            self.managers.append(manager)
            return manager
        self.start_patch(patch("background_agent.BackgroundTasks", side_effect=factory))

    def start_patch(self, patcher):
        value = patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        return value

    def mock_http(self, handler):
        def factory(**kwargs):
            return AsyncOpenAI(http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler), trust_env=False), **kwargs)
        return self.start_patch(patch("background_agent.AsyncOpenAI", side_effect=factory))

    def flow_response(self, body, number):
        actual = {item["call_id"]: ast.literal_eval(item["output"])
                  for item in body["input"] if item.get("type") == "function_call_output"}
        if number == 1:
            self.assertEqual(actual, {})
            return self.tool_response(None, number, "start_background", {"numbers": self.managers[0].numbers})
        first = actual["call_local_1"]["task"]
        self.assertEqual(first["status"], "pending")
        self.assertIsNone(first["result"])
        task_id = first["task_id"]
        if number == 2:
            return self.tool_response(None, number, "add", {"a": 37, "b": 58})
        self.assertEqual(actual["call_local_2"]["result"], 95)
        if number == 3:
            return self.tool_response(None, number, "get_task_status", {"task_id": task_id})
        if number == 4:
            self.assertEqual(actual["call_local_3"]["task"]["task_id"], task_id)
            return self.tool_response(None, number, "get_task_result", {"task_id": task_id})
        task = actual["call_local_4"]["task"]
        self.assertTrue(actual["call_local_4"]["ready"])
        self.assertEqual(task["status"], "completed")
        report = {"task_id": task_id, "status": task["status"], "background_sum": task["result"]["sum"],
                  "count": task["result"]["count"], "receipt": task["result"]["receipt"],
                  "foreground_sum": actual["call_local_2"]["result"]}
        return self.final_response(number, json.dumps(report))

    def flow_handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        number = len(self.requests)
        self.assertLessEqual(number, 5)
        self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertFalse(body["store"])
        self.assertFalse(body["parallel_tool_calls"])
        self.assertEqual(body["reasoning"]["effort"], "none")
        self.assertEqual(body["max_output_tokens"], 1024)
        self.assertEqual({tool["name"] for tool in body["tools"]},
                         {"start_background", "add", "get_task_status", "get_task_result"})
        response = self.flow_response(body, number)
        payload = response.json()
        payload["usage"].update(input_tokens=24_000, output_tokens=100, total_tokens=24_100)
        return httpx2.Response(200, json=payload)

    def saved(self):
        paths = list(self.root.glob("agent_background_state_*.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    async def test_model_tools_real_worker_concurrency_feedback_and_receipt(self):
        factory = self.mock_http(self.flow_handler)
        with patch("background_agent.RunConfig", wraps=background_agent.RunConfig) as config:
            self.assertEqual(await background_agent.main(), 0)
        factory.assert_called_once_with(api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1",
            timeout=120.0, max_retries=0)
        config.assert_called_once_with(tracing_disabled=True)
        saved = self.saved()
        self.assertTrue(saved["verified"])
        self.assertTrue(saved["closed"])
        job = saved["tasks"][0]
        self.assertEqual([x["status"] for x in job["history"]], ["pending", "running", "completed"])
        self.assertEqual(saved["model_decisions"][1]["tasks"][0]["status"], "running")
        self.assertLess(saved["model_decisions"][1]["at"], job["finished_at"])
        self.assertEqual(saved["foreground_events"][0]["background_status"], "running")
        self.assertEqual(job["result"]["sum"], sum(job["numbers"]))
        self.assertEqual(saved["usage"]["requests"], 5)
        self.assertEqual(saved["usage"]["tool_calls"], 4)
        self.assertEqual(saved["usage"]["input_tokens"], 120000)
        self.assertIn("Background Tasks 后台任务验证： 通过", self.stdout.getvalue())
        self.assertNotIn("local-test-placeholder", self.stdout.getvalue())
        self.assertTrue(all(task.done() for task in self.managers[0].workers.values()))

    async def test_slow_model_may_finish_add_after_worker_without_false_claims(self):
        async def handler(request):
            body = json.loads(request.content)
            self.requests.append(body)
            if len(self.requests) == 2:
                await asyncio.sleep(0.2)
            return self.flow_response(body, len(self.requests))
        self.mock_http(handler)
        self.assertEqual(await background_agent.main(), 0)
        saved = self.saved()
        self.assertEqual(saved["model_decisions"][1]["tasks"][0]["status"], "running")
        self.assertEqual(saved["foreground_events"][0]["background_status"], "completed")
        self.assertIn("未伪造重叠", self.stdout.getvalue())

    async def test_pending_running_result_not_ready_completed_and_snapshot_isolation(self):
        manager = BackgroundTasks([12, 8], 1, 2, delay=0.03)
        try:
            started = manager.start([12, 8])
            task_id = started["task"]["task_id"]
            self.assertEqual(manager.get_status(task_id)["task"]["status"], "pending")
            started["task"]["numbers"][0] = 999
            self.assertEqual(manager.jobs[task_id].numbers, [12, 8])
            self.assertFalse((await manager.get_result(task_id))["ready"])
            await asyncio.sleep(0)
            self.assertEqual(manager.get_status(task_id)["task"]["status"], "running")
            self.assertEqual(manager.add(1, 2)["background_status"], "running")
            result = await manager.get_result(task_id, wait=True)
            self.assertTrue(result["ready"])
            self.assertEqual(result["task"]["status"], "completed")
            self.assertEqual(result["task"]["result"]["sum"], 20)
        finally:
            await manager.close()

    async def test_failed_worker_is_queryable_and_exception_text_not_saved(self):
        def fail(numbers):
            raise ValueError("local-test-placeholder 不应进入日志")
        manager = BackgroundTasks([1], 1, 2, delay=0, compute=fail)
        try:
            task_id = manager.start([1])["task"]["task_id"]
            result = await manager.get_result(task_id, wait=True)
            self.assertTrue(result["ready"])
            self.assertFalse(result["ok"])
            self.assertEqual(manager.get_status(task_id)["task"]["status"], "failed")
            self.assertEqual(result["task"]["error"], "ValueError")
            self.assertIsNone(result["task"]["result"])
            self.assertNotIn("local-test-placeholder", json.dumps(manager.snapshot()))
        finally:
            await manager.close()

    async def test_cancel_before_or_after_start_cleans_worker_and_records_failure(self):
        for advance in (False, True):
            manager = BackgroundTasks([1], 1, 2, delay=5)
            task_id = manager.start([1])["task"]["task_id"]
            if advance:
                await asyncio.sleep(0)
            await manager.close()
            await manager.close()
            self.assertTrue(manager.workers[task_id].done())
            self.assertEqual(manager.get_status(task_id)["task"]["status"], "failed")
            self.assertIsNotNone(manager.jobs[task_id].finished_at)
            with self.assertRaises(ValueError):
                manager.start([1])

    async def test_invalid_data_unknown_id_duplicate_start_and_wrong_add_are_rejected(self):
        for invalid in ([], [True], [10001], list(range(9)), "rm -rf /", ["../../private"]):
            with self.assertRaises(ValueError):
                BackgroundTasks(invalid, 1, 2)
        manager = BackgroundTasks([1, 2], 37, 58, delay=0)
        try:
            self.assertFalse(manager.get_status("arbitrary-id")["ok"])
            self.assertFalse((await manager.get_result("arbitrary-id", wait=True))["ok"])
            self.assertFalse(manager.add(37, 58)["ok"])
            with self.assertRaises(ValueError):
                manager.start([2, 3])
            manager.start([1, 2])
            with self.assertRaises(ValueError):
                manager.start([1, 2])
            self.assertFalse(manager.add(True, 58)["ok"])
            self.assertFalse(manager.add(2, 3)["ok"])
            self.assertTrue(manager.add(58, 37)["ok"])
            self.assertFalse(manager.add(37, 58)["ok"])
            self.assertEqual({t.name for t in create_background_tools(manager)},
                             {"start_background", "add", "get_task_status", "get_task_result"})
        finally:
            await manager.close()

    async def test_result_wait_is_bounded_and_timeout_does_not_cancel_worker(self):
        manager = BackgroundTasks([1], 1, 2, delay=0.05)
        task_id = manager.start([1])["task"]["task_id"]
        real_wait = asyncio.wait_for
        async def short_wait(awaitable, timeout):
            self.assertEqual(timeout, 6.0)
            return await real_wait(awaitable, 0.001)
        try:
            with patch("background_tools.asyncio.wait_for", side_effect=short_wait):
                result = await manager.get_result(task_id, wait=True)
            self.assertFalse(result["ok"])
            self.assertFalse(manager.workers[task_id].done())
            final = await manager.get_result(task_id, wait=True)
            self.assertTrue(final["ready"])
        finally:
            await manager.close()

    async def test_false_final_report_not_accepted(self):
        def handler(request):
            response = self.flow_handler(request)
            if len(self.requests) == 5:
                payload = response.json()
                report = json.loads(payload["output"][0]["content"][0]["text"])
                report["receipt"] = "invented-receipt"
                return self.final_response(5, json.dumps(report))
            return response
        self.mock_http(handler)
        self.assertEqual(await background_agent.main(), 1)
        self.assertFalse(self.saved()["verified"])
        self.assertEqual(len(self.requests), 5)

    async def test_duplicate_model_start_is_stopped_and_worker_cleaned(self):
        def handler(request):
            self.requests.append(json.loads(request.content))
            return self.tool_response(None, len(self.requests), "start_background", {"numbers": self.managers[0].numbers})
        self.mock_http(handler)
        self.assertEqual(await background_agent.main(), 1)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(t.done() for t in self.managers[0].workers.values()))
        self.assertFalse(self.saved()["verified"])

    async def test_missing_or_unauthorized_configuration_creates_no_client_or_record(self):
        for variable, value in (("OPENAI_API_KEY", ""), ("OPENAI_BASE_URL", ""), ("OPENAI_BASE_URL", "https://example.invalid/v1")):
            with patch.dict(os.environ, {variable: value}), patch("background_agent.AsyncOpenAI") as factory:
                self.assertEqual(await background_agent.main(), 1)
                factory.assert_not_called()
            self.assertEqual(list(self.root.iterdir()), [])

    async def test_budget_risk_stops_before_starting_worker(self):
        def handler(request):
            response = self.flow_handler(request)
            payload = response.json()
            payload["usage"].update(input_tokens=200000, output_tokens=100, total_tokens=200100)
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await background_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.managers[0].jobs, {})
        self.assertEqual(self.saved()["usage"]["tool_calls"], 0)

    async def test_http_error_redacts_key_and_does_not_retry(self):
        def handler(request):
            self.requests.append(request)
            return httpx2.Response(400, json={"error":{"message":"local-test-placeholder sk-fake-long-placeholder-value", "type":"invalid_request_error"}})
        self.mock_http(handler)
        self.assertEqual(await background_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("local-test-placeholder", self.stderr.getvalue())
        self.assertNotIn("sk-fake-long-placeholder-value", self.stderr.getvalue())
        self.assertEqual(self.saved()["tasks"], [])

    async def test_timeout_after_start_cancels_worker_without_retries(self):
        def handler(request):
            if self.requests:
                self.requests.append(json.loads(request.content))
                raise APITimeoutError(request=request)
            return self.flow_handler(request)
        self.mock_http(handler)
        self.assertEqual(await background_agent.main(), 1)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(t.done() for t in self.managers[0].workers.values()))
        self.assertEqual(self.saved()["tasks"][0]["status"], "failed")
        self.assertIn("请核对平台账单", self.stderr.getvalue())

    async def test_demo_without_credentials_runs_real_concurrent_work(self):
        with patch.dict(os.environ, {}, clear=True), patch("background_agent.AsyncOpenAI") as client:
            self.assertEqual(await background_agent.main(demo=True), 0)
        client.assert_not_called()
        saved = self.saved()
        self.assertTrue(saved["verified"])
        self.assertEqual(saved["tasks"][0]["result"]["sum"], 400)
        self.assertEqual(saved["foreground_events"][0]["background_status"], "running")
        self.assertIn("没有模型或 API", self.stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
