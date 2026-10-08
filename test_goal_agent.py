"""无收费检查：模拟模型规划和报告，真实读取来源、计算、更新并保存 Goal。"""

import ast
import contextlib
import copy
import io
import json
import os
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from openai import APITimeoutError, AsyncOpenAI

import goal_agent
from goal_tools import Example, GoalAccess, PlannedStep, Report, create_goal_tools, prepare_goal_files
import test_agent as fixtures


class GoalTests(unittest.IsolatedAsyncioTestCase):
    response = fixtures.AgentConfigurationTests.response
    tool_response = fixtures.AgentConfigurationTests.tool_response
    final_response = fixtures.AgentConfigurationTests.final_response

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix=".goal-check-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        self.example = Example(137, 245, 400, "only-in-artificial-inputs-74a2")
        self.start_patch(patch("goal_agent.PROJECT_DIR", self.root))
        self.start_patch(patch("goal_agent.Example.random", return_value=self.example))
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

    def mock_http(self, handler):
        def factory(**kwargs):
            return AsyncOpenAI(http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler), trust_env=False), **kwargs)
        return self.start_patch(patch("goal_agent.AsyncOpenAI", side_effect=factory))

    def access(self):
        return GoalAccess(self.root, prepare_goal_files(self.root, self.example))

    @staticmethod
    def plan(access, reversed_reads=False):
        names = list(access.files)
        if reversed_reads:
            names.reverse()
        return [PlannedStep(action="read_file", target=name, description="模型计划：读取来源 " + name)
                for name in names] + [
            PlannedStep(action="add", target="", description="模型计划：汇总实际数量"),
            PlannedStep(action="verify_report", target="", description="模型计划：核对报告和达标情况"),
        ]

    def flow_handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        number = len(self.requests)
        self.assertLessEqual(number, 6)
        state = json.loads(body["instructions"].split(goal_agent.GOAL_PREFIX, 1)[1])
        self.assertEqual(state["completed_steps"], [0, 0, 1, 2, 3, 4][number - 1])
        self.assertEqual(state["pending_steps"], [0, 4, 3, 2, 1, 0][number - 1])
        first = json.loads(self.requests[0]["instructions"].split(goal_agent.GOAL_PREFIX, 1)[1])
        self.assertEqual(state["goal_id"], first["goal_id"])
        self.assertEqual(state["objective"], first["objective"])
        self.assertEqual(state["criteria"], first["criteria"])
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertEqual(body["max_output_tokens"], 1024)
        self.assertFalse(body["parallel_tool_calls"])
        self.assertFalse(body["store"])
        self.assertEqual(body["reasoning"]["effort"], "none")
        self.assertEqual({t["name"] for t in body["tools"]}, {"set_plan", "read_file", "add", "verify_report"})
        names = re.findall(r'agent_goal_(?:morning|afternoon)_[A-Za-z0-9_]+\.json', state["objective"])
        self.assertEqual(len(names), 2)
        actual = {item["call_id"]: ast.literal_eval(item["output"])
                  for item in body["input"] if item.get("type") == "function_call_output"}
        if number == 1:
            self.assertEqual(state["steps"], [])
            self.assertEqual(actual, {})
            self.assertNotIn(self.example.batch, json.dumps(body))
            steps = [{"action": "read_file", "target": name, "description": "模型计划：读取来源 " + name}
                     for name in names] + [
                {"action": "add", "target": "", "description": "模型计划：汇总实际数量"},
                {"action": "verify_report", "target": "", "description": "模型计划：核对报告和达标情况"},
            ]
            result = self.tool_response(None, number, "set_plan", {"steps": steps})
        elif number in (2, 3):
            next_step = next(s for s in state["steps"] if s["status"] == "pending")
            self.assertEqual(next_step["action"], "read_file")
            result = self.tool_response(None, number, "read_file", {"path": next_step["target"]})
        elif number == 4:
            self.assertEqual(state["steps"][2]["status"], "pending")
            counts = [json.loads(actual[f"call_local_{n}"]["content"])["count"] for n in (2, 3)]
            result = self.tool_response(None, number, "add", {"a": counts[0], "b": counts[1]})
        elif number == 5:
            sources = [actual[f"call_local_{n}"]["path"] for n in (2, 3)]
            data = [json.loads(actual[f"call_local_{n}"]["content"]) for n in (2, 3)]
            afternoon = next(d for d in data if d["period"] == "afternoon")
            total = actual["call_local_4"]["result"]
            report = {"total": total, "target": afternoon["target"], "met": total >= afternoon["target"],
                      "batch": afternoon["batch"], "sources": sources}
            result = self.tool_response(None, number, "verify_report", {"report": report})
        else:
            self.assertEqual(state["status"], "ready_for_report")
            self.assertTrue(all(s["status"] == "completed" and s["evidence"] for s in state["steps"]))
            result = self.final_response(number, json.dumps(actual["call_local_5"]["report"]))
        payload = result.json()
        payload["usage"].update(input_tokens=24_000, output_tokens=100, total_tokens=24_100)
        return httpx2.Response(200, json=payload)

    def recorded_goal(self):
        path = sorted(self.root.glob("agent_goal_state_*.json"), key=lambda p: p.stat().st_mtime_ns)[-1]
        return json.loads(path.read_text())

    async def test_model_plan_progress_goal_context_and_completion_are_real(self):
        factory = self.mock_http(self.flow_handler)
        with patch("goal_agent.RunConfig", wraps=goal_agent.RunConfig) as config:
            self.assertEqual(await goal_agent.main(), 0)
        factory.assert_called_once_with(api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1",
            timeout=120.0, max_retries=0)
        config.assert_called_once_with(tracing_disabled=True)
        self.assertEqual(len(self.requests), 6)
        saved = self.recorded_goal()
        self.assertEqual(saved["goal"]["status"], "complete")
        self.assertEqual(saved["goal"]["completed_steps"], 4)
        self.assertEqual(saved["goal"]["pending_steps"], 0)
        self.assertEqual(saved["goal"]["verified_report"]["total"], 382)
        self.assertFalse(saved["goal"]["verified_report"]["met"])
        self.assertEqual(saved["usage"]["input_tokens"], 144000)
        self.assertEqual(saved["usage"]["tool_calls"], 5)
        self.assertEqual(saved["history"][0]["steps"], [])
        self.assertEqual(saved["history"][-1]["status"], "complete")
        log = self.stdout.getvalue() + self.stderr.getvalue()
        self.assertIn("Goals 目标与进度验证： 通过", log)
        self.assertIn("$0.04410000", log)
        self.assertNotIn("local-test-placeholder", log)

    async def test_goal_can_complete_with_either_met_or_unmet_business_target(self):
        self.mock_http(self.flow_handler)
        with patch("goal_agent.Example.random", return_value=Example(137, 245, 300, self.example.batch)):
            self.assertEqual(await goal_agent.main(), 0)
        saved = self.recorded_goal()
        self.assertEqual(saved["goal"]["status"], "complete")
        self.assertTrue(saved["goal"]["verified_report"]["met"])

    async def test_reverse_read_order_and_sources_are_valid_if_model_plan_matches(self):
        access = self.access()
        plan = self.plan(access, reversed_reads=True)
        self.assertTrue(access.set_plan(plan)["ok"])
        for step in plan[:2]:
            self.assertTrue(access.read_file(step.target)["ok"])
        self.assertTrue(access.add(self.example.afternoon, self.example.morning)["ok"])
        report = access.expected_report()
        report["sources"].reverse()
        self.assertTrue(access.verify_report(Report(**report))["ok"])
        access.goal.complete(report)
        self.assertEqual(access.goal.status, "complete")

    async def test_cannot_skip_sources_calculate_arbitrary_values_or_self_tick_complete(self):
        access = self.access()
        self.assertFalse(access.add(137, 245)["ok"])
        with self.assertRaises(ValueError):
            access.goal.complete({"total": 382})
        access.set_plan(self.plan(access))
        self.assertFalse(access.read_file(list(access.files)[1])["ok"])
        self.assertEqual(access.goal.snapshot()["completed_steps"], 0)
        for path in access.files:
            self.assertTrue(access.read_file(path)["ok"])
        self.assertFalse(access.add(1, 2)["ok"])
        self.assertFalse(access.add(True, 245)["ok"])
        self.assertEqual(access.goal.snapshot()["completed_steps"], 2)

    async def test_changed_input_cannot_be_used_as_completion_evidence(self):
        access = self.access()
        access.set_plan(self.plan(access))
        name = next(iter(access.files))
        access.files[name].allowed_file.write_text('{"period":"morning","count":999,"batch":"different"}\n')
        self.assertFalse(access.read_file(name)["ok"])
        self.assertEqual(access.goal.snapshot()["completed_steps"], 0)
        self.assertEqual(access.data, {})

    async def test_unknown_paths_denied_without_reading_files(self):
        access = self.access()
        access.set_plan(self.plan(access))
        for backend in access.files.values():
            with patch.object(backend, "read_file") as read:
                for path in ("../goal_fake_private.json", "/goal_fake_private.json", "agent.py"):
                    self.assertFalse(access.read_file(path)["ok"])
                read.assert_not_called()

    async def test_plan_replacement_and_invalid_plan_cannot_erase_progress(self):
        access = self.access()
        steps = self.plan(access)
        self.assertFalse(access.set_plan(steps[:1])["ok"])
        invalid = copy.deepcopy(steps)
        invalid[0].target = "agent.py"
        self.assertFalse(access.set_plan(invalid)["ok"])
        self.assertTrue(access.set_plan(steps)["ok"])
        access.read_file(steps[0].target)
        before = access.goal.snapshot()
        self.assertFalse(access.set_plan(steps)["ok"])
        self.assertEqual(access.goal.snapshot(), before)

    async def test_wrong_report_is_not_verified_and_premature_completion_fails(self):
        access = self.access()
        access.set_plan(self.plan(access))
        for path in access.files:
            access.read_file(path)
        access.add(137, 245)
        actual = access.expected_report()
        for bad in ({**actual, "total": 999}, {**actual, "met": True},
                    {**actual, "batch": "made-up"}, {**actual, "sources": ["agent.py", "agent.py"]}):
            self.assertFalse(access.verify_report(Report(**bad))["ok"])
        self.assertEqual(access.goal.snapshot()["completed_steps"], 3)
        self.assertIsNone(access.goal.verified_report)
        with self.assertRaises(ValueError):
            access.goal.complete(actual)

    async def test_false_final_answer_is_stopped_even_after_four_steps_done(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            if len(self.requests) == 6:
                payload["output"][0]["content"][0]["text"] = '{"total":999}'
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await goal_agent.main(), 1)
        self.assertEqual(len(self.requests), 6)
        saved = self.recorded_goal()
        self.assertEqual(saved["goal"]["completed_steps"], 4)
        self.assertEqual(saved["goal"]["status"], "stopped")
        self.assertTrue(all(s["status"] != "complete" for s in saved["history"]))

    async def test_repeated_planning_stops_with_existing_plan_preserved(self):
        def handler(request):
            response = self.flow_handler(request)
            if len(self.requests) == 2:
                body = self.requests[0]
                names = re.findall(r'agent_goal_(?:morning|afternoon)_[A-Za-z0-9_]+\.json', body["instructions"])
                steps = [{"action": "read_file", "target": name, "description": "another plan"} for name in names]
                steps += [{"action": "add", "target": "", "description": "sum"},
                          {"action": "verify_report", "target": "", "description": "verify"}]
                return self.tool_response(None, 2, "set_plan", {"steps": steps})
            return response
        self.mock_http(handler)
        self.assertEqual(await goal_agent.main(), 1)
        self.assertEqual(len(self.requests), 2)
        saved = self.recorded_goal()
        self.assertEqual(len(saved["goal"]["steps"]), 4)
        self.assertTrue(saved["goal"]["steps"][0]["description"].startswith("模型计划："))

    async def test_verifier_rejects_stale_goal_context_and_missing_tool_feedback(self):
        captured = {}
        original = goal_agent.verify_run
        def capture(*args):
            captured["args"] = args
            return original(*args)
        self.mock_http(self.flow_handler)
        with patch("goal_agent.verify_run", side_effect=capture):
            self.assertEqual(await goal_agent.main(), 0)
        result, recorder, access, example = captured["args"]
        recorder.goal_contexts[2]["completed_steps"] = 0
        self.assertFalse(original(result, recorder, access, example)["每轮 Goal 进度真实更新，完成与待办同时可见"])
        recorder.model_inputs[5] = {}
        self.assertFalse(original(result, recorder, access, example)["含最新 Goal 的实际工具结果均进入后续模型请求"])

    async def test_budget_risk_stops_before_plan_tool_and_records_incomplete_goal(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            payload["usage"].update(input_tokens=150_000, output_tokens=100, total_tokens=150_100)
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await goal_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("超过已授权的 1 元预算", self.stderr.getvalue())
        self.assertEqual(self.recorded_goal()["goal"]["status"], "stopped")
        self.assertEqual(self.recorded_goal()["goal"]["steps"], [])

    async def test_missing_or_unauthorized_configuration_creates_no_client_or_files(self):
        with patch("goal_agent.AsyncOpenAI") as factory:
            for name, value in (("OPENAI_API_KEY", ""), ("OPENAI_BASE_URL", ""),
                    ("OPENAI_BASE_URL", "https://example.invalid/v1")):
                with patch.dict(os.environ, {name: value}):
                    self.assertEqual(await goal_agent.main(), 1)
            factory.assert_not_called()
        self.assertFalse(list(self.root.glob("agent_goal_*.json")))

    async def test_http_error_and_timeout_do_not_retry_or_log_credentials(self):
        def error_handler(request):
            self.requests.append(request)
            return httpx2.Response(400, json={"error": {
                "message": "invalid local-test-placeholder", "type": "invalid_request_error"}})
        self.mock_http(error_handler)
        self.assertEqual(await goal_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("local-test-placeholder", self.stdout.getvalue() + self.stderr.getvalue())
        self.requests.clear()
        def timeout_handler(request):
            self.requests.append(request)
            raise APITimeoutError(request=request)
        self.mock_http(timeout_handler)
        self.assertEqual(await goal_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("请核对平台账单", self.stderr.getvalue())

    async def test_local_demo_has_no_api_and_record_is_not_memory_or_background(self):
        with patch.dict(os.environ, {}, clear=True), patch("goal_agent.AsyncOpenAI") as factory:
            self.assertEqual(await goal_agent.main(demo=True), 0)
            factory.assert_not_called()
        saved = self.recorded_goal()
        self.assertIsNone(saved["usage"])
        self.assertEqual(saved["goal"]["status"], "complete")
        self.assertIn("没有模型或 API", self.stdout.getvalue())
        self.assertEqual({tool.name for tool in create_goal_tools(self.access())}, {
            "set_plan", "read_file", "add", "verify_report"})


if __name__ == "__main__":
    unittest.main()
