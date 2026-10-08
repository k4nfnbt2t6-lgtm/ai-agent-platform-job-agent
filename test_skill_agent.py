"""离线验证 Skill：模拟模型响应，真实加载说明、读取文件并执行受限程序。"""

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
from agents import Agent, ModelSettings, OpenAIResponsesModel, RunConfig, Runner
from openai import APITimeoutError, AsyncOpenAI

import skill_agent
from skill_tools import (PRACTICE_SOURCE, SkillCatalog, create_skill_tools,
                         practice_access, prepare_skill_file, read_metadata)
from tools import add
import test_agent as fixtures


class SkillTests(unittest.IsolatedAsyncioTestCase):
    response = fixtures.AgentConfigurationTests.response
    final_response = fixtures.AgentConfigurationTests.final_response
    tool_response = fixtures.AgentConfigurationTests.tool_response

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix=".skill-tests-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        folder = self.root / "lab_skills" / skill_agent.SKILL_NAME
        folder.mkdir(parents=True)
        self.skill_path = folder / "SKILL.md"
        self.skill_path.write_text((Path(__file__).parent / "lab_skills" / skill_agent.SKILL_NAME / "SKILL.md").read_text())
        self.start_patch(patch("skill_agent.PROJECT_DIR", self.root))
        self.start_patch(patch.dict(os.environ, {
            "OPENAI_API_KEY": "local-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1",
        }, clear=True))
        for method in ("connect", "connect_ex"):
            self.start_patch(patch.object(socket.socket, method,
                side_effect=AssertionError("离线验证禁止实际网络连接")))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.start_patch(contextlib.redirect_stdout(self.stdout))
        self.start_patch(contextlib.redirect_stderr(self.stderr))
        self.example = skill_agent.Example(137, 258)
        self.start_patch(patch("skill_agent.Example", return_value=self.example))
        self.requests = []

    def start_patch(self, patcher):
        value = patcher.__enter__()
        self.addCleanup(patcher.__exit__, None, None, None)
        return value

    def mock_http(self, handler):
        def factory(**kwargs):
            return AsyncOpenAI(http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler), trust_env=False), **kwargs)
        return self.start_patch(patch("skill_agent.AsyncOpenAI", side_effect=factory))

    def flow_handler(self, request):
        """模拟决策；报告数据只来自当前任务和本次实际工具输出。"""
        self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
        body = json.loads(request.content)
        self.requests.append(body)
        number = len(self.requests)
        self.assertLessEqual(number, 6)
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertFalse(body["store"])
        self.assertFalse(body["parallel_tool_calls"])
        self.assertNotIn("previous_response_id", body)
        self.assertNotIn("conversation", body)
        user = next(item["content"] for item in body["input"] if item.get("role") == "user")
        calls = [item for item in body["input"] if item.get("type") == "function_call"]
        outputs = {item["call_id"]: item["output"] for item in body["input"]
                   if item.get("type") == "function_call_output"}
        if "任务数据：" in user:
            task = json.loads(user.split("任务数据：", 1)[1])
            if not calls:
                metadata = json.loads(body["instructions"].split("可用 Skill 目录（这里只有元信息）：\n", 1)[1])
                result = self.tool_response(None, number, name="load_skill", arguments={"name": metadata[0]["name"]})
            elif len(calls) == 1:
                loaded = ast.literal_eval(outputs[calls[0]["call_id"]])
                self.assertTrue(loaded["ok"])
                result = self.tool_response(None, number, name="read_file", arguments={"path": task["path"]})
            elif len(calls) == 2:
                read = ast.literal_eval(outputs[calls[1]["call_id"]])
                self.assertTrue(read["ok"])
                result = self.tool_response(None, number, name="terminal", arguments={"command": ["python", task["path"]]})
            else:
                loaded = ast.literal_eval(outputs[calls[0]["call_id"]])
                ran = ast.literal_eval(outputs[calls[-1]["call_id"]])
                passed = ran["returncode"] == 0 and ran["stdout"] == task["expected_stdout"] and ran["stderr"] == ""
                result = self.final_response(number, json.dumps({"skill": loaded["name"], "path": task["path"],
                    "returncode": ran["returncode"], "expected_stdout": task["expected_stdout"],
                    "actual_stdout": ran["stdout"], "verdict": "通过" if passed else "未通过"}, ensure_ascii=False))
        else:
            if not calls:
                match = re.search(r"计算 (\d+) \+ (\d+)", user)
                a, b = map(int, match.groups())
                result = self.tool_response(None, number, name="add", arguments={"a": a, "b": b})
            else:
                value = ast.literal_eval(outputs[calls[-1]["call_id"]])
                result = self.final_response(number, json.dumps({"sum": value}))
        payload = result.json()
        payload["usage"].update(input_tokens=24_000, output_tokens=100, total_tokens=24_100)
        return httpx2.Response(200, json=payload)

    async def test_actual_skill_loading_execution_and_unrelated_tool_only_task(self):
        factory = self.mock_http(self.flow_handler)
        with patch("skill_agent.RunConfig", wraps=skill_agent.RunConfig) as config:
            self.assertEqual(await skill_agent.main(), 0)
        factory.assert_called_once_with(api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1",
            timeout=120.0, max_retries=0)
        config.assert_called_once_with(tracing_disabled=True)
        self.assertEqual(len(self.requests), 6)
        body = self.skill_path.read_text()
        for request in self.requests:
            self.assertNotIn(body, request["instructions"])
        self.assertNotIn("actual_stdout", json.dumps(self.requests[0], ensure_ascii=False))
        loaded = next(item["output"] for item in self.requests[1]["input"] if item.get("type") == "function_call_output")
        self.assertEqual(ast.literal_eval(loaded)["instructions"], body)
        self.assertEqual(self.requests[4]["input"], [{"role": "user", "content": self.example.calculation_task}])
        self.assertNotIn("actual_stdout", json.dumps(self.requests[4:]))
        source = next(self.root.glob("agent_skill_*.py")).read_text()
        self.assertEqual(source, PRACTICE_SOURCE)
        log = self.stdout.getvalue() + self.stderr.getvalue()
        self.assertIn("Skills 选择与使用验证： 通过", log)
        self.assertIn("$0.04410000", log)
        self.assertIn("token 用量提醒 6 次", log)
        self.assertNotIn("local-test-placeholder", log)

    async def test_verifier_rejects_eager_full_skill_context_and_previous_task_leak(self):
        self.mock_http(self.flow_handler)
        captured = {}
        original = skill_agent.verify_skill
        def capture(*args):
            captured["args"] = args
            return original(*args)
        with patch("skill_agent.verify_skill", side_effect=capture):
            self.assertEqual(await skill_agent.main(), 0)
        recorder = captured["args"][1]
        instructions = copy.deepcopy(recorder.instructions)
        recorder.instructions[0] += self.skill_path.read_text()
        self.assertFalse(original(*captured["args"])["初始请求只有 Skill 元信息，没有正文或预设完整流程"])
        recorder.instructions = instructions
        recorder.contexts[4] = recorder.contexts[3] + recorder.contexts[4]
        self.assertFalse(original(*captured["args"])["无关任务没有继承已加载 Skill 或上一任务历史"])

    async def test_same_skill_handles_another_program_and_distinguishes_running_from_correct(self):
        catalog = SkillCatalog(self.root)
        async with AsyncOpenAI(api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self.flow_handler), trust_env=False)) as client:
            for source, expected, verdict in ((PRACTICE_SOURCE, "95\n", "通过"),
                    (PRACTICE_SOURCE.replace("a + b", "a + a"), "95\n", "未通过"),
                    (PRACTICE_SOURCE.replace("a + b", "a + a"), "74\n", "通过")):
                with self.subTest(source=source, expected=expected):
                    self.requests.clear()
                    path = prepare_skill_file(self.root)
                    path.write_text(source)
                    access = practice_access(self.root, path)
                    recorder = skill_agent.SkillRecorder()
                    recorder.phase = "检查程序"
                    learning_agent = Agent(name="Offline Skill", instructions=skill_agent.make_instructions(catalog),
                        model=OpenAIResponsesModel("gpt-6-sol", openai_client=client),
                        model_settings=ModelSettings(store=False, parallel_tool_calls=False),
                        tools=[*create_skill_tools(catalog, access), add])
                    result = await Runner.run(learning_agent, skill_agent.inspection_task(access.relative_path, expected),
                        max_turns=4, hooks=recorder, run_config=RunConfig(tracing_disabled=True))
                    report = json.loads(result.final_output)
                    self.assertEqual(report["verdict"], verdict)
                    self.assertEqual(report["returncode"], 0)
                    self.assertEqual(report["actual_stdout"], "74\n" if "a + a" in source else "95\n")
                    self.assertEqual(path.read_text(), source)
                    self.assertEqual([r["name"] for r in recorder.tool_calls], ["load_skill", "read_file", "terminal"])

    async def test_catalog_rejects_paths_unknown_names_and_changed_metadata(self):
        catalog = SkillCatalog(self.root)
        for name in ("../agent.py", str(self.skill_path), "unknown-skill"):
            self.assertFalse(catalog.load(name)["ok"])
        self.skill_path.write_text(self.skill_path.read_text().replace("description:", "unexpected:"))
        self.assertFalse(catalog.load(skill_agent.SKILL_NAME)["ok"])

    async def test_invalid_metadata_and_linked_folders_are_rejected(self):
        for text in ("no header", "---\nname: missing-description\n---\n",
                "---\nname: check-python-output\nname: duplicate\ndescription: test\n---\n",
                "---\nname: mismatched-folder\ndescription: test\n---\n"):
            with self.subTest(text=text):
                self.skill_path.write_text(text)
                with self.assertRaises(ValueError):
                    SkillCatalog(self.root)
        self.skill_path.write_text((Path(__file__).parent / "lab_skills" / skill_agent.SKILL_NAME / "SKILL.md").read_text())
        linked = self.root / "lab_skills" / "linked-skill"
        linked.symlink_to(self.skill_path.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            SkillCatalog(self.root)

    async def test_discovery_stops_at_metadata_and_load_checks_body_size(self):
        stream = io.StringIO("---\nname: example-skill\ndescription: simple case\n---\nbody remains unread")
        self.assertEqual(read_metadata(stream), {"name": "example-skill", "description": "simple case"})
        self.assertEqual(stream.read(), "body remains unread")
        self.skill_path.write_text(self.skill_path.read_text() + "x" * 3_000)
        catalog = SkillCatalog(self.root)
        self.assertEqual(len(catalog.metadata()), 1)
        self.assertFalse(catalog.load(skill_agent.SKILL_NAME)["ok"])

    async def test_other_paths_commands_and_unsafe_python_never_reach_subprocess(self):
        path = prepare_skill_file(self.root)
        access = practice_access(self.root, path)
        self.assertFalse(access.read_file("agent.py")["ok"])
        self.assertFalse(access.read_file("../README.md")["ok"])
        with patch("repair_tools.run_terminal") as execute:
            for command in (["pwd"], ["python", "agent.py"], ["sh", "-c", "echo test"]):
                self.assertIsNone(access.run_python(command)["returncode"])
            path.write_text("import os\nprint(os.environ)\n")
            self.assertIsNone(access.run_python(["python", path.name])["returncode"])
            execute.assert_not_called()

    async def test_wrong_final_report_fails_without_repair_or_extra_requests(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            if len(self.requests) == 4:
                payload["output"][0]["content"][0]["text"] = '{"verdict":"通过"}'
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await skill_agent.main(), 1)
        self.assertEqual(len(self.requests), 6)
        self.assertIn("报告采用 Skill 中的规则与格式，字段来自真实证据：未通过", self.stdout.getvalue())
        self.assertEqual(next(self.root.glob("agent_skill_*.py")).read_text(), PRACTICE_SOURCE)

    async def test_repeated_loading_stops_at_phase_limit_without_retries(self):
        def handler(request):
            self.requests.append(json.loads(request.content))
            payload = self.tool_response(None, len(self.requests), name="load_skill",
                arguments={"name": skill_agent.SKILL_NAME}).json()
            payload["usage"].update(input_tokens=200, output_tokens=20, total_tokens=220)
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await skill_agent.main(), 1)
        self.assertEqual(len(self.requests), 4)
        self.assertIn("本节已停止", self.stderr.getvalue())

    async def test_invalid_skill_name_stops_before_next_request(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            payload["output"][0]["arguments"] = json.dumps({"name": "../../agent.py"})
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await skill_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("Skill 加载被拒绝", self.stderr.getvalue())

    async def test_wrong_program_output_is_not_claimed_success(self):
        prepare = skill_agent.prepare_skill_file
        def incorrect_source(root):
            path = prepare(root)
            path.write_text(PRACTICE_SOURCE.replace("a + b", "a + a"))
            return path
        self.mock_http(self.flow_handler)
        with patch("skill_agent.prepare_skill_file", side_effect=incorrect_source):
            self.assertEqual(await skill_agent.main(), 1)
        self.assertEqual(len(self.requests), 6)
        self.assertIn('"actual_stdout": "74\\n", "verdict": "未通过"', self.stdout.getvalue())

    async def test_fee_risk_stops_before_skill_loading_or_followup_calls(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            payload["usage"].update(input_tokens=150_000, total_tokens=150_100)
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await skill_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("模型选择并加载 Skill：", self.stdout.getvalue())
        self.assertIn("超过已授权的 1 元预算", self.stderr.getvalue())

    async def test_missing_or_unapproved_config_creates_no_practice_or_client(self):
        for name, value in (("OPENAI_API_KEY", ""), ("OPENAI_BASE_URL", ""),
                            ("OPENAI_BASE_URL", "https://example.invalid/v1")):
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}), \
                    patch("skill_agent.AsyncOpenAI") as client, patch("skill_agent.prepare_skill_file") as prepare:
                self.assertEqual(await skill_agent.main(), 1)
                client.assert_not_called()
                prepare.assert_not_called()

    async def test_http_error_redacted_without_retry(self):
        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx2.Response(400, json={"error": {"message": "local-test-placeholder 参数不兼容", "type": "invalid_request_error"}})
        self.mock_http(handler)
        self.assertEqual(await skill_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("HTTP 400", self.stderr.getvalue())
        self.assertNotIn("local-test-placeholder", self.stdout.getvalue() + self.stderr.getvalue())

    async def test_timeout_report_keeps_unknown_fee_and_does_not_retry(self):
        def handler(request):
            self.requests.append(json.loads(request.content))
            raise APITimeoutError(request=request)
        self.mock_http(handler)
        self.assertEqual(await skill_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("不代表平台未处理或未扣费", self.stderr.getvalue())
        self.assertIn("费用请查看平台账单", self.stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
