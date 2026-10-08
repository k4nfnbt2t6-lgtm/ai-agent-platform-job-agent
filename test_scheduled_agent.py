"""模型响应模拟；定时器、实际执行、时间边界和清理真实验证。"""

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

import scheduled_agent
from scheduled_tools import ScheduledTasks, create_scheduled_tools
import test_agent as fixtures


class ScheduledTests(unittest.IsolatedAsyncioTestCase):
    response = fixtures.AgentConfigurationTests.response
    tool_response = fixtures.AgentConfigurationTests.tool_response
    final_response = fixtures.AgentConfigurationTests.final_response

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix=".scheduled-check-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        self.start_patch(patch("scheduled_agent.PROJECT_DIR", self.root))
        self.start_patch(patch.dict(os.environ, {
            "OPENAI_API_KEY":"local-test-placeholder", "OPENAI_BASE_URL":"https://api.likeai520.cc/v1",
        }, clear=True))
        for method in ("connect", "connect_ex"):
            self.start_patch(patch.object(socket.socket, method, side_effect=AssertionError("离线检查禁止网络连接")))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.start_patch(contextlib.redirect_stdout(self.stdout))
        self.start_patch(contextlib.redirect_stderr(self.stderr))
        self.requests, self.managers = [], []
        def factory(*args, **kwargs):
            # 仅缩短模型模拟流程的等待；显式本地演示延迟保持原值。
            kwargs.setdefault("delay_seconds", 0.08)
            manager = ScheduledTasks(*args, **kwargs)
            self.managers.append(manager)
            return manager
        self.start_patch(patch("scheduled_agent.ScheduledTasks", side_effect=factory))

    def start_patch(self, patcher):
        value = patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        return value

    def mock_http(self, handler):
        def factory(**kwargs):
            return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler), trust_env=False), **kwargs)
        return self.start_patch(patch("scheduled_agent.AsyncOpenAI", side_effect=factory))

    def flow_response(self, body, number):
        actual = {item["call_id"]:ast.literal_eval(item["output"])
                  for item in body["input"] if item.get("type") == "function_call_output"}
        if number == 1:
            self.assertEqual(actual, {})
            return self.tool_response(None, number, "schedule_task", {
                "numbers":self.managers[0].numbers, "delay_seconds":self.managers[0].delay_seconds})
        submitted = actual["call_local_1"]["task"]
        self.assertEqual(submitted["status"], "scheduled")
        self.assertIsNone(submitted["started_at"])
        self.assertIsNone(submitted["result"])
        task_id = submitted["task_id"]
        if number == 2:
            return self.tool_response(None, number, "get_task_status", {"task_id":task_id})
        if number == 3:
            self.assertEqual(actual["call_local_2"]["task"]["task_id"], task_id)
            return self.tool_response(None, number, "get_task_result", {"task_id":task_id})
        finished = actual["call_local_3"]["task"]
        self.assertTrue(actual["call_local_3"]["ready"])
        self.assertEqual(finished["status"], "completed")
        report = {"task_id":task_id, "status":finished["status"], **finished["result"],
                  "not_early":finished["started_at"] >= finished["run_at"]}
        return self.final_response(number, json.dumps(report))

    def flow_handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        self.assertLessEqual(len(self.requests), 4)
        self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertFalse(body["parallel_tool_calls"])
        self.assertFalse(body["store"])
        self.assertEqual(body["reasoning"]["effort"], "none")
        self.assertEqual(body["max_output_tokens"], 1024)
        self.assertEqual({t["name"] for t in body["tools"]}, {"schedule_task", "get_task_status", "get_task_result"})
        payload = self.flow_response(body, len(self.requests)).json()
        payload["usage"].update(input_tokens=24000,output_tokens=100,total_tokens=24100)
        return httpx2.Response(200,json=payload)

    def saved(self):
        paths = list(self.root.glob("agent_scheduled_state_*.json"))
        self.assertEqual(len(paths),1)
        return json.loads(paths[0].read_text())

    async def test_model_schedules_actual_timer_and_reports_true_time_and_receipt(self):
        factory = self.mock_http(self.flow_handler)
        with patch("scheduled_agent.RunConfig",wraps=scheduled_agent.RunConfig) as config:
            self.assertEqual(await scheduled_agent.main(),0)
        config.assert_called_once_with(tracing_disabled=True)
        factory.assert_called_once_with(api_key="local-test-placeholder",base_url="https://api.likeai520.cc/v1",timeout=120.0,max_retries=0)
        r = self.saved()
        self.assertTrue(r["verified"] and r["closed"])
        job = r["tasks"][0]
        self.assertEqual([x["status"] for x in job["history"]],["scheduled","running","completed"])
        self.assertGreaterEqual(job["started_at"],job["run_at"])
        self.assertEqual(job["result"]["sum"],sum(job["numbers"]))
        self.assertEqual(r["usage"]["requests"],4)
        self.assertEqual(r["usage"]["tool_calls"],3)
        self.assertEqual(r["usage"]["input_tokens"],96000)
        self.assertIn("Scheduled Tasks 定时任务验证： 通过",self.stdout.getvalue())
        self.assertNotIn("local-test-placeholder",self.stdout.getvalue())
        self.assertTrue(all(t.cancelled() for t in self.managers[0].timers.values()))
        self.assertTrue(all(w.done() for w in self.managers[0].workers.values()))

    async def test_slow_model_can_query_completed_state_without_forcing_scheduled(self):
        async def handler(request):
            body = json.loads(request.content)
            self.requests.append(body)
            if len(self.requests)==2:
                await asyncio.sleep(0.22)
            return self.flow_response(body,len(self.requests))
        self.mock_http(handler)
        self.assertEqual(await scheduled_agent.main(),0)
        last = self.saved()["observations"][-1]
        self.assertEqual(last["task"]["status"],"completed")

    async def test_before_deadline_no_work_and_automatic_execution_without_result_query(self):
        manager = ScheduledTasks([12,8],delay_seconds=0.12)
        try:
            task_id = manager.schedule([12,8],0.12)["task"]["task_id"]
            await asyncio.sleep(0.03)
            early = manager.get_status(task_id)
            self.assertLess(early["observed_at"],early["task"]["run_at"])
            self.assertEqual(early["task"]["status"],"scheduled")
            self.assertIsNone(early["task"]["started_at"])
            self.assertFalse((await manager.get_result(task_id))["ready"])
            self.assertEqual(manager.workers,{})
            await asyncio.sleep(0.24)
            completed = manager.get_status(task_id)["task"]
            self.assertEqual(completed["status"],"completed")
            self.assertGreaterEqual(completed["started_at"],completed["run_at"])
            self.assertEqual(completed["result"]["sum"],20)
        finally:
            await manager.close()

    async def test_early_timer_wakeup_is_rescheduled_instead_of_executed(self):
        manager = ScheduledTasks([1],delay_seconds=0.02)
        try:
            task_id = manager.schedule([1],0.02)["task"]["task_id"]
            manager.timers[task_id].cancel() # 代表一次已触发的计时器。
            job = manager.jobs[task_id]
            loop = asyncio.get_running_loop()
            with patch.object(loop,"time",return_value=job.run_at-0.000001):
                manager._launch(job)
            self.assertEqual(job.status,"scheduled")
            self.assertEqual(manager.workers,{})
            self.assertIsNone(job.started_at)
            finished = await manager.get_result(task_id,wait=True)
            self.assertTrue(finished["ready"])
            self.assertGreaterEqual(job.started_at,job.run_at)
        finally:
            await manager.close()

    async def test_running_status_and_failed_result_are_observable(self):
        def fail(numbers):
            raise ValueError("local-test-placeholder 不应进入错误记录")
        manager = ScheduledTasks([1],delay_seconds=0.02,compute=fail)
        try:
            task_id = manager.schedule([1],0.02)["task"]["task_id"]
            await asyncio.sleep(0.04)
            self.assertEqual(manager.get_status(task_id)["task"]["status"],"running")
            result = await manager.get_result(task_id,wait=True)
            self.assertTrue(result["ready"])
            self.assertFalse(result["ok"])
            self.assertEqual(result["task"]["status"],"failed")
            self.assertEqual(result["task"]["error"],"ValueError")
            self.assertIsNone(result["task"]["result"])
            self.assertNotIn("local-test-placeholder",json.dumps(manager.snapshot()))
        finally:
            await manager.close()

    async def test_close_scheduled_or_running_cancels_every_timer_and_worker(self):
        for advance in (False,True):
            manager = ScheduledTasks([1],delay_seconds=0.02)
            task_id = manager.schedule([1],0.02)["task"]["task_id"]
            if advance:
                await asyncio.sleep(0.04)
            await manager.close()
            await manager.close()
            self.assertEqual(manager.jobs[task_id].status,"failed")
            self.assertTrue(manager.finished[task_id].done())
            self.assertTrue(all(t.cancelled() for t in manager.timers.values()))
            self.assertTrue(all(w.done() for w in manager.workers.values()))
            await asyncio.sleep(0.04)
            self.assertEqual(manager.jobs[task_id].status,"failed")
            with self.assertRaises(ValueError):
                manager.schedule([1],0.02)

    async def test_invalid_parameters_unknown_ids_duplicate_schedule_and_snapshot_isolation(self):
        for delay in (0,-1,6,float('inf'),float('nan'),True,'3'):
            with self.assertRaises(ValueError):
                ScheduledTasks([1],delay_seconds=delay)
        manager = ScheduledTasks([12,8],delay_seconds=0.02)
        try:
            self.assertFalse(manager.get_status('unknown')["ok"])
            self.assertFalse((await manager.get_result('unknown',wait=True))["ok"])
            for numbers,delay in (([2],0.02),([12,8],0.03),([True],0.02),(['../../private'],0.02)):
                with self.assertRaises(ValueError):
                    manager.schedule(numbers,delay)
            submitted = manager.schedule([12,8],0.02)
            submitted["task"]["numbers"][0]=999
            self.assertEqual(next(iter(manager.jobs.values())).numbers,[12,8])
            with self.assertRaises(ValueError):
                manager.schedule([12,8],0.02)
            self.assertEqual({t.name for t in create_scheduled_tools(manager)}, {"schedule_task","get_task_status","get_task_result"})
        finally:
            await manager.close()

    async def test_finite_repeat_only_two_jobs_are_possible(self):
        manager = ScheduledTasks([5],delay_seconds=0.02,max_jobs=2)
        try:
            first = manager.schedule([5],0.02)["task"]["task_id"]
            second = manager.schedule([5],0.04)["task"]["task_id"]
            with self.assertRaises(ValueError):
                manager.schedule([5],0.02)
            for task_id in (first,second):
                result=await manager.get_result(task_id,wait=True)
                self.assertTrue(result["ready"])
                self.assertGreaterEqual(result["task"]["started_at"],result["task"]["run_at"])
            self.assertEqual(len(manager.jobs),2)
        finally:
            await manager.close()

    async def test_false_final_time_or_receipt_is_rejected(self):
        def handler(request):
            response=self.flow_handler(request)
            if len(self.requests)==4:
                report=json.loads(response.json()["output"][0]["content"][0]["text"])
                report["not_early"]=False
                return self.final_response(4,json.dumps(report))
            return response
        self.mock_http(handler)
        self.assertEqual(await scheduled_agent.main(),1)
        self.assertFalse(self.saved()["verified"])
        self.assertEqual(len(self.requests),4)

    async def test_duplicate_model_schedule_stops_without_leaving_timer(self):
        def handler(request):
            self.requests.append(json.loads(request.content))
            return self.tool_response(None,len(self.requests),"schedule_task",{
                "numbers":self.managers[0].numbers,"delay_seconds":self.managers[0].delay_seconds})
        self.mock_http(handler)
        self.assertEqual(await scheduled_agent.main(),1)
        self.assertEqual(len(self.requests),2)
        self.assertFalse(self.saved()["verified"])
        self.assertTrue(all(t.cancelled() for t in self.managers[0].timers.values()))

    async def test_budget_risk_stops_before_scheduling(self):
        def handler(request):
            payload=self.flow_handler(request).json()
            payload["usage"].update(input_tokens=200000,output_tokens=100,total_tokens=200100)
            return httpx2.Response(200,json=payload)
        self.mock_http(handler)
        self.assertEqual(await scheduled_agent.main(),1)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.managers[0].jobs,{})
        self.assertEqual(self.saved()["usage"]["tool_calls"],0)

    async def test_missing_or_unauthorized_configuration_does_not_start(self):
        for variable,value in (("OPENAI_API_KEY",""),("OPENAI_BASE_URL",""),("OPENAI_BASE_URL","https://example.invalid/v1")):
            with patch.dict(os.environ,{variable:value}),patch("scheduled_agent.AsyncOpenAI") as factory:
                self.assertEqual(await scheduled_agent.main(),1)
                factory.assert_not_called()
            self.assertEqual(list(self.root.iterdir()),[])

    async def test_timeout_after_schedule_is_not_retried_and_timer_cleaned(self):
        def handler(request):
            if self.requests:
                self.requests.append(json.loads(request.content))
                raise APITimeoutError(request=request)
            return self.flow_handler(request)
        self.mock_http(handler)
        self.assertEqual(await scheduled_agent.main(),1)
        self.assertEqual(len(self.requests),2)
        self.assertTrue(self.saved()["closed"])
        self.assertTrue(all(t.cancelled() for t in self.managers[0].timers.values()))
        self.assertIn("请核对平台账单",self.stderr.getvalue())

    async def test_http_error_redacts_credentials_without_retry(self):
        def handler(request):
            self.requests.append(request)
            return httpx2.Response(400,json={"error":{"message":"local-test-placeholder","type":"invalid_request_error"}})
        self.mock_http(handler)
        self.assertEqual(await scheduled_agent.main(),1)
        self.assertEqual(len(self.requests),1)
        self.assertNotIn("local-test-placeholder",self.stderr.getvalue())
        self.assertEqual(self.saved()["tasks"],[])

    async def test_demo_is_local_and_real_without_credentials(self):
        with patch.dict(os.environ,{},clear=True),patch("scheduled_agent.AsyncOpenAI") as client:
            self.assertEqual(await scheduled_agent.main(demo=True),0)
        client.assert_not_called()
        r=self.saved()
        self.assertTrue(r["verified"] and r["closed"])
        self.assertEqual(r["tasks"][0]["result"]["sum"],400)
        self.assertGreaterEqual(r["tasks"][0]["started_at"],r["tasks"][0]["run_at"])
        self.assertIn("到期前检查： scheduled",self.stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
