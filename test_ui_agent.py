"""模拟模型/HTTP，实际 SDK、工具、界面控制器；不调用收费 API。"""

import asyncio
import contextlib
import curses
import io
import json
import os
import socket
import re
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import httpx2
from openai import AsyncOpenAI

import gui_agent
import tui_agent
import ui_runtime
from tools import PROJECT_DIR


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.runtime = ui_runtime.AgentRuntime(self.events.append)
        for name in ("connect", "connect_ex"):
            p = patch.object(socket.socket, name, side_effect=AssertionError("不允许外网连接"))
            p.start()
            self.addCleanup(p.stop)

    async def test_real_sdk_demo_calls_add_and_feeds_output_back(self):
        result = await self.runtime.run_task(ui_runtime.DEMO_TASK)
        self.assertTrue(ui_runtime.verify_arithmetic(self.runtime, result))
        kinds = [e.kind for e in self.events]
        self.assertLess(kinds.index("tool_result"), kinds.index("final"))
        self.assertIn("执行工具：add", [e.text for e in self.events])
        self.assertEqual(self.runtime.usage()["estimated_rmb"], 0)

    async def test_directory_uses_existing_terminal_in_project(self):
        result = await self.runtime.run_task("请查看当前项目路径 pwd")
        self.assertIsNotNone(result)
        record = self.runtime.recorder.tool_calls[0]
        self.assertEqual(record["arguments"], {"command": ["pwd"]})
        self.assertEqual(record["output"]["stdout"].strip(), str(PROJECT_DIR))
        self.assertEqual(record["output"]["returncode"], 0)

    async def test_empty_and_secret_input_never_reach_model(self):
        self.assertIsNone(await self.runtime.run_task(" "))
        self.assertIsNone(await self.runtime.run_task("sk-" + "dummy" * 5))
        self.assertFalse(self.runtime.recorder.model_inputs)
        self.assertNotIn("dummy", " ".join(e.text for e in self.events))

    async def test_same_recorder_keeps_limits_between_tasks(self):
        for _ in range(3):
            self.assertIsNotNone(await self.runtime.run_task("37 + 58"))
        self.assertIsNone(await self.runtime.run_task("37 + 58"))
        self.assertEqual(len(self.runtime.recorder.responses), 6)
        self.assertEqual(len(self.runtime.recorder.tool_calls), 3)

    async def test_controls_and_key_redacted_before_display(self):
        self.runtime.emit("log", "hello\x1b[2J\x00sk-" + "placeholder")
        self.assertNotIn("\x1b", self.events[-1].text)
        self.assertNotIn("placeholder", self.events[-1].text)

    async def test_cancellation_is_observable_and_stops(self):
        task = asyncio.create_task(self.runtime.run_task("37 + 58"))
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(any(e.text.startswith("已取消") for e in self.events))
        self.assertFalse(self.runtime.recorder.tool_calls)

    async def test_live_uses_same_core_with_mock_http_and_two_real_tool_rounds(self):
        bodies = []
        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            self.assertEqual(body["model"], "gpt-6-sol")
            self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
            self.assertEqual({x["name"] for x in body["tools"]}, {"add", "terminal"})
            self.assertEqual(body["instructions"], ui_runtime.INSTRUCTIONS)
            self.assertFalse(body["parallel_tool_calls"])
            self.assertFalse(body["store"])
            if len(bodies) == 1:
                output = [{"type": "function_call", "id": "fc_demo", "call_id": "call_demo", "name": "add",
                           "arguments": '{"a":37,"b":58}', "status": "completed"}]
            else:
                self.assertEqual([x["output"] for x in body["input"] if x.get("type") == "function_call_output"], ["95"])
                output = [{"type": "message", "role": "assistant", "id": "msg_demo", "status": "completed",
                           "content": [{"type": "output_text", "text": "实际工具结果为 95。", "annotations": []}]}]
            return httpx2.Response(200, json={"id": f"response_{len(bodies)}", "object": "response",
                "created_at": 0, "status": "completed", "model": "gpt-6-sol", "output": output,
                "usage": {"input_tokens": 24000, "output_tokens": 40, "total_tokens": 24040}})
        def client_factory(**kwargs):
            self.assertEqual(kwargs["max_retries"], 0)
            return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler), trust_env=False), **kwargs)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "local-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1"}, clear=True), \
             patch("ui_runtime.AsyncOpenAI", side_effect=client_factory), \
             patch("ui_runtime.RunConfig", wraps=ui_runtime.RunConfig) as config:
            runtime = ui_runtime.AgentRuntime(self.events.append, demo=False)
            result = await runtime.run_task(ui_runtime.DEMO_TASK)
        self.assertTrue(ui_runtime.verify_arithmetic(runtime, result))
        self.assertEqual(runtime.usage()["warnings"], 2)
        self.assertAlmostEqual(runtime.usage()["estimated_rmb"], 0.1452)
        config.assert_called_once_with(tracing_disabled=True)
        self.assertEqual(len(bodies), 2)

    async def test_missing_config_and_wrong_url_stop_before_client(self):
        for env in ({}, {"OPENAI_API_KEY": "placeholder", "OPENAI_BASE_URL": "https://example.invalid/v1"}):
            with patch.dict(os.environ, env, clear=True), patch("ui_runtime.AsyncOpenAI") as client:
                with self.assertRaises(ValueError):
                    ui_runtime.AgentRuntime(self.events.append, demo=False)
                client.assert_not_called()

    async def test_unsafe_terminal_request_never_executes_process(self):
        class UnsafeModel(ui_runtime.DemoModel):
            async def get_response(self, *args, **kwargs):
                return ui_runtime.ModelResponse(output=[ui_runtime.ResponseFunctionToolCall(
                    type="function_call", name="terminal", call_id="unsafe-demo",
                    arguments=json.dumps({"command": ["rm", "-rf", "/"]}),
                )], usage=ui_runtime.Usage(input_tokens=1, output_tokens=1), response_id=None)
        with patch("ui_runtime.DemoModel", UnsafeModel), patch("tools.run_terminal") as execution:
            self.assertIsNone(await self.runtime.run_task("模拟危险请求，只验证拒绝"))
        execution.assert_not_called()
        self.assertEqual(len(self.runtime.recorder.responses), 1)
        self.assertTrue(any(e.kind == "error" for e in self.events))

    async def test_expensive_usage_stops_before_tool_execution(self):
        class CostlyModel(ui_runtime.DemoModel):
            async def get_response(self, *args, **kwargs):
                response = await super().get_response(*args, **kwargs)
                response.usage = ui_runtime.Usage(input_tokens=400000, output_tokens=10)
                return response
        with patch("ui_runtime.DemoModel", CostlyModel):
            self.assertIsNone(await self.runtime.run_task("37 + 58"))
        self.assertFalse(self.runtime.recorder.tool_calls)
        self.assertEqual(len(self.runtime.recorder.responses), 1)

    async def test_connection_failure_does_not_retry_or_expose_secret(self):
        class FailingModel(ui_runtime.DemoModel):
            async def get_response(self, *args, **kwargs):
                raise ui_runtime.APIConnectionError(request=httpx2.Request("POST", "https://api.likeai520.cc/v1/responses"))
        with patch("ui_runtime.DemoModel", FailingModel):
            self.assertIsNone(await self.runtime.run_task("37 + 58"))
        self.assertEqual(len(self.runtime.recorder.model_inputs), 1)
        self.assertFalse(self.runtime.recorder.responses)
        self.assertTrue(any("不重试" in e.text for e in self.events))


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.controller = ui_runtime.InterfaceController(demo=True)
        self.addCleanup(self.controller.close)

    def wait_finished(self):
        deadline = time.monotonic() + 4
        while self.controller.snapshot()["busy"] and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.controller.snapshot()["busy"])

    def test_controller_nonblocking_and_duplicate_submit_rejected(self):
        start = time.monotonic()
        self.assertTrue(self.controller.submit("37 + 58")[0])
        self.assertLess(time.monotonic() - start, 0.1)
        self.assertFalse(self.controller.submit("37 + 58")[0])
        self.wait_finished()
        state = self.controller.snapshot()
        self.assertEqual(self.controller.runtime.usage()["requests"], 2)
        self.assertIn("95", next(e["text"] for e in state["events"] if e["kind"] == "final"))

    def test_terminal_panel_accepts_keyboard_and_shows_core_events(self):
        class Window:
            def __init__(self):
                self.keys = iter(list("37 + 58") + ["\n"])
                self.shown = []
            def timeout(self, _): pass
            def getmaxyx(self): return 30, 100
            def erase(self): pass
            def refresh(self): pass
            def addnstr(self, y, x, text, width): self.shown.append(text)
            def get_wch(self):
                try: return next(self.keys)
                except StopIteration:
                    time.sleep(0.02)
                    if not self_outer.controller.snapshot()["busy"]: return "\x1b"
                    raise curses.error()
        self_outer = self
        window = Window()
        with patch("tui_agent.curses.curs_set"):
            tui_agent.panel(window, self.controller)
        record = self.controller.runtime.recorder.tool_calls[0]
        self.assertEqual(record["name"], "add")
        self.assertEqual(record["output"], 95)
        self.assertEqual(self.controller.runtime.recorder.model_inputs[1][record["call_id"]], "95")
        self.assertIn("输入任务：37 + 58", window.shown)
        self.assertTrue(any("思考中" in text for text in window.shown))
        self.assertTrue(any("95" in text for text in window.shown))

    def test_gui_loopback_submit_and_result_use_same_runtime(self):
        server, url = gui_agent.create_server(self.controller)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base, token = url.split("#")
        headers = {"X-Lab-Token": token, "Content-Type": "application/json"}
        with urlopen(base, timeout=2) as response:
            html = response.read().decode()
            self.assertIn("最终结果", html)
            self.assertIn("textContent", html)
            self.assertNotIn("innerHTML", html)
        with self.assertRaises(HTTPError) as denied:
            urlopen(base + "events", timeout=2)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        wrong_origin = headers | {"Origin": "https://example.invalid"}
        with self.assertRaises(HTTPError) as cross_origin:
            urlopen(Request(base + "task", data=b'{"task":"37 + 58"}', headers=wrong_origin), timeout=2)
        cross_origin.exception.close()
        request = Request(base + "task", data=json.dumps({"task": "37 + 58"}).encode(), headers=headers)
        with urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 202)
        self.wait_finished()
        with urlopen(Request(base + "events", headers=headers), timeout=2) as response:
            state = json.load(response)
        self.assertEqual(self.controller.runtime.usage()["requests"], 2)
        self.assertTrue(any(e["kind"] == "tool_result" and "95" in e["text"] for e in state["events"]))
        self.assertTrue(any(e["kind"] == "final" and "95" in e["text"] for e in state["events"]))

    def test_close_cancels_worker_and_refuses_new_work(self):
        self.controller.submit("37 + 58")
        self.controller.close()
        self.assertFalse(self.controller.worker.is_alive())
        self.assertFalse(self.controller.submit("37 + 58")[0])

    def test_gui_javascript_parses_as_served(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("未安装 Node；需用浏览器实测脚本。")
        script = re.search(r"<script>(.*?)</script>", gui_agent.PAGE, flags=re.S).group(1)
        result = subprocess.run([node, "--check"], input=script, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
