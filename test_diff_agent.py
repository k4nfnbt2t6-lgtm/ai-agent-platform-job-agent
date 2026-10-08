"""无收费检查：模拟模型响应，真实文件读写和 Diff，禁止网络连接。"""

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

import diff_agent
from diff_tools import INITIAL_SOURCE, EXPECTED_SOURCE, DiffFileAccess, build_diff, prepare_diff_file
import test_agent as fixtures


class DiffTextTests(unittest.TestCase):
    def test_exact_unified_diff_and_three_distinct_change_types(self):
        report = build_diff(INITIAL_SOURCE, EXPECTED_SOURCE, "sample.py")
        self.assertEqual(report["unified_diff"], (
            "--- a/sample.py\n+++ b/sample.py\n@@ -1,9 +1,9 @@\n"
            " # Diff practice\n-# obsolete note: remove this line\n \n"
            " def add(a, b):\n-    return a - b\n+    return a + b\n \n \n"
            ' if __name__ == "__main__":\n+    # Show the result.\n     print(add(37, 58))\n'
        ))
        self.assertEqual([c["kind"] for c in report["changes"]], ["deleted", "modified", "added"])
        self.assertEqual(report["summary"], {
            "added_lines": 1, "deleted_lines": 1, "modified_blocks": 1,
            "replaced_old_lines": 1, "replacement_new_lines": 1,
            "unified_added_lines": 2, "unified_deleted_lines": 2,
        })

    def test_no_change_and_empty_file_insertion_or_deletion(self):
        same = build_diff("keep\n", "keep\n", "sample.txt")
        self.assertEqual(same["unified_diff"], "")
        self.assertEqual(same["changes"], [])
        self.assertFalse(any(same["summary"].values()))
        inserted = build_diff("", "new\n", "sample.txt")
        self.assertEqual(inserted["summary"]["added_lines"], 1)
        self.assertIn("@@ -0,0 +1 @@\n+new\n", inserted["unified_diff"])
        removed = build_diff("old\n", "", "sample.txt")
        self.assertEqual(removed["summary"]["deleted_lines"], 1)
        self.assertIn("@@ -1 +0,0 @@\n-old\n", removed["unified_diff"])

    def test_no_final_newline_is_visible_instead_of_silently_changed(self):
        report = build_diff("old", "new\n", "sample.txt")
        self.assertEqual(report["unified_diff"], (
            "--- a/sample.txt\n+++ b/sample.txt\n@@ -1 +1 @@\n"
            "-old\n\\ No newline at end of file\n+new\n"))
        self.assertEqual(report["changes"][0]["old_lines"], ["old"])

    def test_unequal_replacement_is_a_block_not_fake_one_to_one_lines(self):
        report = build_diff("keep\nold1\nold2\n", "keep\nnew\n", "sample.txt")
        self.assertEqual(report["summary"]["modified_blocks"], 1)
        self.assertEqual(report["summary"]["replaced_old_lines"], 2)
        self.assertEqual(report["summary"]["replacement_new_lines"], 1)
        self.assertEqual(report["summary"]["added_lines"], 0)
        self.assertEqual(report["summary"]["deleted_lines"], 0)


class DiffFlowTests(unittest.IsolatedAsyncioTestCase):
    response = fixtures.AgentConfigurationTests.response
    tool_response = fixtures.AgentConfigurationTests.tool_response
    final_response = fixtures.AgentConfigurationTests.final_response

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix=".diff-check-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
        self.start_patch(patch("diff_agent.PROJECT_DIR", self.root))
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
        return self.start_patch(patch("diff_agent.AsyncOpenAI", side_effect=factory))

    def flow_handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        number = len(self.requests)
        self.assertEqual(str(request.url), "https://api.likeai520.cc/v1/responses")
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertEqual(body["max_output_tokens"], 1024)
        self.assertFalse(body["parallel_tool_calls"])
        self.assertFalse(body["store"])
        self.assertEqual(body.get("tool_choice", "auto"), "auto")
        self.assertEqual(body["reasoning"]["effort"], "none")
        self.assertEqual({t["name"] for t in body["tools"]}, {"read_file", "write_file"})
        self.assertLessEqual(number, 3)
        path = next(self.root.glob("agent_diff_*.py"))
        actual = {item["call_id"]: ast.literal_eval(item["output"])
                  for item in body["input"] if item.get("type") == "function_call_output"}
        if number == 1:
            self.assertEqual(actual, {})
            self.assertNotIn(INITIAL_SOURCE, json.dumps(body, ensure_ascii=False))
            result = self.tool_response(None, number, "read_file", {"path": path.name})
        elif number == 2:
            original = actual["call_local_1"]["content"]
            self.assertEqual(original, path.read_text())
            new = original.replace("# obsolete note: remove this line\n", "").replace(
                "return a - b", "return a + b").replace(
                "    print(add(37, 58))", "    # Show the result.\n    print(add(37, 58))")
            result = self.tool_response(None, number, "write_file", {
                "path": path.name, "expected_content": original, "content": new})
        else:
            written = actual["call_local_2"]
            self.assertEqual(written["content"], path.read_text())
            self.assertEqual(written["before"], INITIAL_SOURCE)
            self.assertIn("-    return a - b\n+    return a + b\n", written["diff"]["unified_diff"])
            self.assertIn("+    # Show the result.\n", written["diff"]["unified_diff"])
            self.assertIn("-# obsolete note: remove this line\n", written["diff"]["unified_diff"])
            summary = written["diff"]["summary"]
            answer = {"path": path.name, **{key: summary[key] for key in (
                "added_lines", "deleted_lines", "modified_blocks")}}
            result = self.final_response(number, json.dumps(answer, ensure_ascii=False))
        payload = result.json()
        payload["usage"].update(input_tokens=24_000, output_tokens=100, total_tokens=24_100)
        return httpx2.Response(200, json=payload)

    async def test_real_writes_and_diff_are_fed_back_into_mocked_model(self):
        factory = self.mock_http(self.flow_handler)
        with patch("diff_agent.RunConfig", wraps=diff_agent.RunConfig) as config:
            self.assertEqual(await diff_agent.main(), 0)
        factory.assert_called_once_with(api_key="local-test-placeholder", base_url="https://api.likeai520.cc/v1",
            timeout=120.0, max_retries=0)
        config.assert_called_once_with(tracing_disabled=True)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(next(self.root.glob("agent_diff_*.py")).read_text(), EXPECTED_SOURCE)
        log = self.stdout.getvalue() + self.stderr.getvalue()
        self.assertIn("Diff 修改与展示验证： 通过", log)
        self.assertIn("统一 Diff 的 + / - 合计：+2 / -2", log)
        self.assertIn("$0.02205000", log)
        self.assertIn("token 用量提醒 3 次", log)
        self.assertNotIn("local-test-placeholder", log)

    async def test_verifier_rejects_tampered_diff_and_missing_feedback(self):
        self.mock_http(self.flow_handler)
        captured = {}
        original_verify = diff_agent.verify_diff
        def capture(*args):
            captured["args"] = args
            return original_verify(*args)
        with patch("diff_agent.verify_diff", side_effect=capture):
            self.assertEqual(await diff_agent.main(), 0)
        result, recorder, access, before = captured["args"]
        receipt = copy.deepcopy(recorder.tool_calls[1]["output"])
        recorder.tool_calls[1]["output"]["diff"]["unified_diff"] = "模型声称：改好了"
        self.assertFalse(original_verify(result, recorder, access, before)[
            "Diff 来自实际原文和实际新文本，没有由模型编造"])
        recorder.tool_calls[1]["output"] = receipt
        recorder.model_inputs[2] = {}
        self.assertFalse(original_verify(result, recorder, access, before)[
            "实际读取结果和含 Diff 的写入结果均交回下一轮模型"])

    async def test_other_files_and_stale_content_are_rejected_without_diff(self):
        important = self.root / "agent.py"
        important.write_text("# important course file\n")
        path = prepare_diff_file(self.root)
        access = DiffFileAccess(self.root, path)
        original = access.read_file(path.name)
        for target in ("agent.py", "../agent.py", str(path)):
            result = access.write_file(target, EXPECTED_SOURCE, original["content"])
            self.assertFalse(result["ok"])
            self.assertNotIn("diff", result)
        path.write_text(INITIAL_SOURCE + "# external change\n")
        result = access.write_file(path.name, EXPECTED_SOURCE, original["content"])
        self.assertFalse(result["ok"])
        self.assertNotIn("diff", result)
        self.assertTrue(path.read_text().endswith("# external change\n"))
        self.assertEqual(important.read_text(), "# important course file\n")

    async def test_wrong_report_fails_without_more_calls(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            if len(self.requests) == 3:
                payload["output"][0]["content"][0]["text"] = '{"added_lines":99}'
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await diff_agent.main(), 1)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("模型最终报告与真实 Diff 相符：未通过", self.stdout.getvalue())

    async def test_repeated_reads_stop_without_infinite_loop(self):
        def handler(request):
            self.requests.append(json.loads(request.content))
            path = next(self.root.glob("agent_diff_*.py"))
            return self.tool_response(None, len(self.requests), "read_file", {"path": path.name})
        self.mock_http(handler)
        self.assertEqual(await diff_agent.main(), 1)
        self.assertEqual(len(self.requests), 3)
        self.assertIn("2 次工具执行上限", self.stderr.getvalue())
        self.assertEqual(next(self.root.glob("agent_diff_*.py")).read_text(), INITIAL_SOURCE)

    async def test_high_token_cost_risk_stops_before_tool_execution(self):
        def handler(request):
            payload = self.flow_handler(request).json()
            payload["usage"].update(input_tokens=150_000, output_tokens=100, total_tokens=150_100)
            return httpx2.Response(200, json=payload)
        self.mock_http(handler)
        self.assertEqual(await diff_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("超过已授权的 1 元预算", self.stderr.getvalue())
        self.assertIn("工具执行次数： 0", self.stdout.getvalue())

    async def test_missing_or_other_base_config_creates_no_file_or_client(self):
        with patch("diff_agent.AsyncOpenAI") as factory:
            for name, value in (("OPENAI_API_KEY", ""), ("OPENAI_BASE_URL", ""),
                                ("OPENAI_BASE_URL", "https://example.invalid/v1")):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    self.assertEqual(await diff_agent.main(), 1)
            factory.assert_not_called()
        self.assertEqual(list(self.root.glob("agent_diff_*.py")), [])

    async def test_http_error_and_timeout_each_send_one_request_and_redact(self):
        def handler(request):
            self.requests.append(request)
            return httpx2.Response(400, json={"error": {
                "message": "Bad request local-test-placeholder", "type": "invalid_request_error"}})
        self.mock_http(handler)
        self.assertEqual(await diff_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("local-test-placeholder", self.stdout.getvalue() + self.stderr.getvalue())
        self.requests.clear()
        def timed_out(request):
            self.requests.append(request)
            raise APITimeoutError(request=request)
        self.mock_http(timed_out)
        self.assertEqual(await diff_agent.main(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("请核对平台账单", self.stderr.getvalue())

    async def test_local_demo_needs_no_credentials_and_calls_no_model(self):
        with patch.dict(os.environ, {}, clear=True), patch("diff_agent.AsyncOpenAI") as factory:
            self.assertEqual(await diff_agent.main(demo=True), 0)
            factory.assert_not_called()
        self.assertIn("没有模型或 API", self.stdout.getvalue())
        self.assertEqual(next(self.root.glob("agent_diff_*.py")).read_text(), EXPECTED_SOURCE)


if __name__ == "__main__":
    unittest.main()
