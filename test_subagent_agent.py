"""两层模型响应模拟，SDK 委派、上下文隔离、文件读取和计算真实执行。"""

import ast
import contextlib
import copy
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

import subagent_agent
from subagent_tools import Example, SubagentAccess, Summary, prepare_subagent_file
import test_agent as fixtures


class SubagentTests(unittest.IsolatedAsyncioTestCase):
    response=fixtures.AgentConfigurationTests.response
    tool_response=fixtures.AgentConfigurationTests.tool_response
    final_response=fixtures.AgentConfigurationTests.final_response

    def setUp(self):
        workspace=tempfile.TemporaryDirectory(prefix=".subagent-check-",dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.root=Path(workspace.name).resolve()
        self.example=Example([137,245,18],450,"child-data-test-only","parent-marker-must-stay-local-to-main")
        self.start_patch(patch("subagent_agent.PROJECT_DIR",self.root))
        self.start_patch(patch("subagent_agent.Example.random",return_value=self.example))
        self.start_patch(patch.dict(os.environ,{
            "OPENAI_API_KEY":"local-test-placeholder","OPENAI_BASE_URL":"https://api.likeai520.cc/v1",
        },clear=True))
        for method in ("connect","connect_ex"):
            self.start_patch(patch.object(socket.socket,method,side_effect=AssertionError("离线检查禁止网络连接")))
        self.stdout,self.stderr=io.StringIO(),io.StringIO()
        self.start_patch(contextlib.redirect_stdout(self.stdout))
        self.start_patch(contextlib.redirect_stderr(self.stderr))
        self.requests=[]
        self.same_ids=False
        self.recorder=None
        original=subagent_agent.TeamRecorder
        def recorder_factory():
            self.recorder=original()
            return self.recorder
        self.start_patch(patch("subagent_agent.TeamRecorder",side_effect=recorder_factory))

    def start_patch(self,patcher):
        value=patcher.__enter__()
        self.addCleanup(patcher.__exit__,None,None,None)
        return value

    def mock_http(self,handler):
        def factory(**kwargs):
            return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler),trust_env=False),**kwargs)
        return self.start_patch(patch("subagent_agent.AsyncOpenAI",side_effect=factory))

    def call_id(self,n):
        return "shared-id-across-agents" if self.same_ids and n in (1,2) else f"call_local_{n}"

    def flow_handler(self,request):
        body=json.loads(request.content)
        self.requests.append(body)
        number=len(self.requests)
        self.assertLessEqual(number,6)
        self.assertEqual(str(request.url),"https://api.likeai520.cc/v1/responses")
        self.assertEqual(body["model"],"gpt-6-sol")
        self.assertEqual(body["max_output_tokens"],1024)
        self.assertFalse(body["store"])
        self.assertFalse(body["parallel_tool_calls"])
        self.assertEqual(body["reasoning"]["effort"],"none")
        parent=number in (1,5,6)
        self.assertEqual({t["name"] for t in body["tools"]},
            {"delegate_summary","judge_report"} if parent else {"read_file","sum_numbers"})
        if not parent:
            self.assertNotIn(self.example.parent_marker,json.dumps(body))
            self.assertNotIn("450",body["instructions"])
        path=next(self.root.glob("agent_subagent_data_*.json"))
        def decode(text):
            try:
                return json.loads(text)
            except ValueError:
                return ast.literal_eval(text)
        actual={item["call_id"]:decode(item["output"]) for item in body["input"] if item.get("type")=="function_call_output"}
        if number==1:
            self.assertEqual(actual,{})
            self.assertNotIn(self.example.dataset,json.dumps(body))
            response=self.tool_response(None,number,"delegate_summary",{"path":path.name})
        elif number==2:
            self.assertEqual(actual,{})
            self.assertEqual(len(body["input"]),1)
            self.assertIn(path.name,body["input"][0]["content"])
            response=self.tool_response(None,number,"read_file",{"path":path.name})
        elif number==3:
            read=actual[self.call_id(2)]
            self.assertTrue(read["ok"])
            data=json.loads(read["content"])
            response=self.tool_response(None,number,"sum_numbers",{"numbers":data["numbers"]})
        elif number==4:
            summed=actual[self.call_id(3)]
            self.assertTrue(summed["ok"])
            report={"path":path.name,**{k:summed[k] for k in ("total","count","dataset","receipt")}}
            response=self.final_response(number,json.dumps(report))
        elif number==5:
            delegated=actual[self.call_id(1)]
            self.assertTrue(delegated["ok"])
            self.assertNotIn(self.call_id(3),actual)
            response=self.tool_response(None,number,"judge_report",{"summary":delegated["summary"],"target":self.example.target})
        else:
            judged=actual[self.call_id(5)]
            self.assertTrue(judged["ok"])
            response=self.final_response(number,json.dumps(judged["report"]))
        payload=response.json()
        if self.same_ids and number in (1,2):
            payload["output"][0]["call_id"]=self.call_id(number)
        payload["usage"].update(input_tokens=24000,output_tokens=100,total_tokens=24100)
        return httpx2.Response(200,json=payload)

    def saved(self):
        paths=list(self.root.glob("agent_subagent_state_*.json"))
        self.assertEqual(len(paths),1)
        return json.loads(paths[0].read_text())

    async def test_actual_sdk_delegation_isolated_child_loop_and_parent_resumption(self):
        factory=self.mock_http(self.flow_handler)
        with patch("subagent_agent.RunConfig",wraps=subagent_agent.RunConfig) as config:
            self.assertEqual(await subagent_agent.main(),0)
        factory.assert_called_once_with(api_key="local-test-placeholder",base_url="https://api.likeai520.cc/v1",timeout=120.0,max_retries=0)
        self.assertTrue(all(c.kwargs=={"tracing_disabled":True} for c in config.call_args_list))
        r=self.saved()
        self.assertTrue(r["verified"])
        self.assertEqual([x["agent"] for x in r["requests"]], [subagent_agent.PARENT,subagent_agent.CHILD,subagent_agent.CHILD,subagent_agent.CHILD,subagent_agent.PARENT,subagent_agent.PARENT])
        self.assertEqual(r["judgement"]["total"],400)
        self.assertEqual(r["judgement"]["target"],450)
        self.assertEqual(r["judgement"]["margin"],-50)
        self.assertFalse(r["judgement"]["met"])
        self.assertEqual(r["judgement"]["parent_marker"],self.example.parent_marker)
        self.assertEqual(r["usage"]["requests"],6)
        self.assertEqual(r["usage"]["tool_calls"],4)
        self.assertEqual(r["usage"]["input_tokens"],144000)
        self.assertEqual(r["usage"]["output_tokens"],600)
        self.assertAlmostEqual(self.recorder.spent_usd(),0.0441)
        self.assertIn("Subagent 主子协作验证： 通过",self.stdout.getvalue())
        self.assertNotIn("local-test-placeholder",self.stdout.getvalue())
        self.assertEqual(json.loads(next(self.root.glob("agent_subagent_data_*.json")).read_text()),{"numbers":self.example.numbers,"dataset":self.example.dataset})

    async def test_parent_and_child_can_have_same_call_id_without_crossing_records(self):
        self.same_ids=True
        self.mock_http(self.flow_handler)
        self.assertEqual(await subagent_agent.main(),0)
        r=self.saved()
        self.assertEqual(r["tool_calls"][0]["call_id"],r["tool_calls"][1]["call_id"])
        self.assertIn("summary",r["tool_calls"][0]["output"])
        self.assertIn("content",r["tool_calls"][1]["output"])

    async def test_met_business_target_also_supported(self):
        self.example=Example(self.example.numbers,350,self.example.dataset,self.example.parent_marker)
        with patch("subagent_agent.Example.random",return_value=self.example):
            self.mock_http(self.flow_handler)
            self.assertEqual(await subagent_agent.main(),0)
        self.assertTrue(self.saved()["judgement"]["met"])
        self.assertEqual(self.saved()["judgement"]["margin"],50)

    def access(self):
        return SubagentAccess(self.root,prepare_subagent_file(self.root,self.example),self.example)

    async def test_bad_delegation_and_paths_do_not_read_private_files(self):
        access=self.access()
        for bad in ('../private.json','/private/example.json','agent.py'):
            with self.assertRaises(subagent_agent.LoopStopped):
                access.build_input({"params":{"path":bad}})
        access.build_input({"params":{"path":access.path}})
        with patch.object(access.file,"read_file") as reader:
            for bad in ('../private.json','/private/example.json','agent.py'):
                self.assertFalse(access.read_file(bad)["ok"])
            reader.assert_not_called()
        with self.assertRaises(subagent_agent.LoopStopped):
            access.build_input({"params":{"path":access.path}})
        self.assertEqual(len(access.delegations),1)

    async def test_child_order_real_parameters_change_and_false_report_are_rejected(self):
        access=self.access()
        self.assertFalse(access.read_file(access.path)["ok"])
        self.assertFalse(access.sum_numbers(self.example.numbers)["ok"])
        access.build_input({"params":{"path":access.path}})
        source=self.root/access.path
        source.write_text('{"numbers":[1],"dataset":"changed"}')
        self.assertFalse(access.read_file(access.path)["ok"])
        source.write_text(access.initial_text)
        self.assertTrue(access.read_file(access.path)["ok"])
        self.assertFalse(access.sum_numbers([1,2,3])["ok"])
        self.assertTrue(access.sum_numbers(self.example.numbers)["ok"])
        self.assertFalse(access.sum_numbers(self.example.numbers)["ok"])
        report=access.expected_summary()
        fake=report|{"receipt":"fake"}
        with self.assertRaises(subagent_agent.LoopStopped):
            access.accept_child(json.dumps(fake))
        with self.assertRaises(subagent_agent.LoopStopped):
            access.accept_child(json.dumps(report|{"unexpected":True}))
        self.assertIsNone(access.child_summary)
        access.accept_child(json.dumps(report))
        self.assertFalse(access.judge_report(Summary(**fake),450)["ok"])
        self.assertFalse(access.judge_report(Summary(**report),449)["ok"])
        self.assertTrue(access.judge_report(Summary(**report),450)["ok"])
        self.assertFalse(access.judge_report(Summary(**report),450)["ok"])

    async def test_invalid_child_answer_stops_before_parent_continuation(self):
        def handler(request):
            response=self.flow_handler(request)
            if len(self.requests)==4:
                report=json.loads(response.json()["output"][0]["content"][0]["text"])
                report["total"]=999
                return self.final_response(4,json.dumps(report))
            return response
        self.mock_http(handler)
        self.assertEqual(await subagent_agent.main(),1)
        self.assertEqual(len(self.requests),4)
        self.assertFalse(self.saved()["verified"])
        self.assertIsNone(self.saved()["child_summary"])
        self.assertIsNone(self.saved()["judgement"])

    async def test_no_child_tools_cannot_be_presented_as_successful_delegation(self):
        def handler(request):
            if self.requests:
                self.requests.append(json.loads(request.content))
                return self.final_response(2,'{"total":400}')
            return self.flow_handler(request)
        self.mock_http(handler)
        self.assertEqual(await subagent_agent.main(),1)
        self.assertEqual(len(self.requests),2)
        self.assertIsNone(self.saved()["child_summary"])

    async def test_false_parent_report_is_rejected_without_extra_requests(self):
        def handler(request):
            response=self.flow_handler(request)
            if len(self.requests)==6:
                report=json.loads(response.json()["output"][0]["content"][0]["text"])
                report["margin"]=999
                return self.final_response(6,json.dumps(report))
            return response
        self.mock_http(handler)
        self.assertEqual(await subagent_agent.main(),1)
        self.assertFalse(self.saved()["verified"])
        self.assertEqual(len(self.requests),6)

    async def test_repeated_parent_delegation_stops_before_second_child_run(self):
        def handler(request):
            response=self.flow_handler(request)
            if len(self.requests)==5:
                path=next(self.root.glob("agent_subagent_data_*.json")).name
                return self.tool_response(None,5,"delegate_summary",{"path":path})
            return response
        self.mock_http(handler)
        self.assertEqual(await subagent_agent.main(),1)
        self.assertEqual(len(self.requests),5)
        self.assertEqual(len(self.saved()["delegations"]),1)

    async def test_nested_budget_is_counted_and_stops_before_child_tool(self):
        def handler(request):
            response=self.flow_handler(request)
            if len(self.requests)==2:
                payload=response.json()
                payload["usage"].update(input_tokens=200000,output_tokens=100,total_tokens=200100)
                return httpx2.Response(200,json=payload)
            return response
        self.mock_http(handler)
        self.assertEqual(await subagent_agent.main(),1)
        r=self.saved()
        self.assertEqual(len(self.requests),2)
        self.assertEqual(r["usage"]["input_tokens"],224000)
        self.assertEqual(r["usage"]["tool_calls"],1)
        self.assertIsNone(r["read_result"])
        self.assertIn("超过已授权",self.stderr.getvalue())

    async def test_nested_timeout_does_not_retry_and_preserves_partial_evidence(self):
        def handler(request):
            if self.requests:
                self.requests.append(json.loads(request.content))
                raise APITimeoutError(request=request)
            return self.flow_handler(request)
        self.mock_http(handler)
        self.assertEqual(await subagent_agent.main(),1)
        r=self.saved()
        self.assertEqual(len(self.requests),2)
        self.assertEqual(r["usage"]["requests"],2)
        self.assertEqual(r["usage"]["input_tokens"],24000)
        self.assertFalse(r["verified"])
        self.assertIn("请核对平台账单",self.stderr.getvalue())

    async def test_nested_http_failure_is_redacted_and_not_retried(self):
        def handler(request):
            if self.requests:
                self.requests.append(json.loads(request.content))
                return httpx2.Response(400,json={"error":{"message":"local-test-placeholder","type":"invalid_request_error"}})
            return self.flow_handler(request)
        self.mock_http(handler)
        self.assertEqual(await subagent_agent.main(),1)
        self.assertEqual(len(self.requests),2)
        self.assertNotIn("local-test-placeholder",self.stderr.getvalue())
        self.assertNotIn("local-test-placeholder",self.stdout.getvalue())

    async def test_missing_or_unauthorized_config_does_not_create_files_or_client(self):
        for variable,value in (("OPENAI_API_KEY",""),("OPENAI_BASE_URL",""),("OPENAI_BASE_URL","https://example.invalid/v1")):
            with patch.dict(os.environ,{variable:value}),patch("subagent_agent.AsyncOpenAI") as factory:
                self.assertEqual(await subagent_agent.main(),1)
                factory.assert_not_called()
            self.assertEqual(list(self.root.iterdir()),[])

    async def test_local_demo_has_no_models_and_is_not_claimed_as_real_delegation(self):
        with patch.dict(os.environ,{},clear=True),patch("subagent_agent.AsyncOpenAI") as factory:
            self.assertEqual(await subagent_agent.main(demo=True),0)
        factory.assert_not_called()
        r=self.saved()
        self.assertEqual(r["kind"],"local_demo")
        self.assertTrue(r["verified"])
        self.assertEqual(r["judgement"]["total"],400)
        self.assertIn("模型决策模拟",self.stdout.getvalue())


if __name__=='__main__':
    unittest.main()
