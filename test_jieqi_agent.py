"""只检查实战入口的权限与流程，使用隔离临时文件，不修改揭棋项目。"""

import contextlib
import ast
import asyncio
import io
import json
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import httpx2
from openai import AsyncOpenAI

import jieqi_agent as app


class ProjectBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix=".jieqi-access-test-", dir=app.LAB)
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        for name in app.READ_RANGES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("original\n", encoding="utf-8")
        self.access = app.ProjectAccess(self.root)
        self.access.read_files(list(app.READ_RANGES))
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def append(self):
        self.access.write_file(app.TEST, "", 'test("存储写入失败：新增断言", () => {});\n',
                               app.digest(self.access.current[app.TEST]))

    def red(self):
        self.append()
        result = subprocess.CompletedProcess(app.TARGET_COMMAND, 1,
            "not ok 1 - 存储写入失败：新增断言\n  code: 'ERR_ASSERTION'\n# tests 1\n# fail 1\n", "")
        with patch("jieqi_agent.subprocess.run", return_value=result):
            self.access.terminal(app.TARGET_COMMAND)

    def test_unknown_and_outside_path_cannot_be_read(self):
        for name in ("../private.txt", "/etc/passwd", "server.js", "tests/../room.js"):
            with self.assertRaises(ValueError):
                self.access.read_files([name])

    def test_helper_and_original_tests_cannot_be_modified(self):
        with self.assertRaises(ValueError):
            self.access.write_file("client-session.js", "original", "replacement", app.digest("original\n"))
        with self.assertRaises(ValueError):
            self.access.write_file(app.TEST, "original", "replacement", app.digest("original\n"))
        self.assertEqual((self.root / app.TEST).read_text(), "original\n")

    def test_source_edit_requires_actual_assertion_failure(self):
        with self.assertRaises(ValueError):
            self.access.write_file(app.CODE, "original", "updated", app.digest("original\n"))
        self.red()
        self.access.write_file(app.CODE, "original", "updated", app.digest("original\n"))
        self.assertEqual((self.root / app.CODE).read_text(), "updated\n")

    def test_tests_freeze_after_red_and_all_requires_green(self):
        self.red()
        with self.assertRaises(ValueError):
            self.append()
        with patch("jieqi_agent.subprocess.run") as execution:
            with self.assertRaises(ValueError):
                self.access.terminal(app.ALL_COMMAND)
            execution.assert_not_called()

    def test_syntax_error_does_not_count_as_bug_reproduction(self):
        self.append()
        result = subprocess.CompletedProcess(app.TARGET_COMMAND, 1, "not ok 1 - tests/room-client.test.cjs\nSyntaxError\n", "")
        with patch("jieqi_agent.subprocess.run", return_value=result):
            self.access.terminal(app.TARGET_COMMAND)
        self.assertIsNone(self.access.red_evidence)

    def test_denied_command_never_starts_process(self):
        with patch("jieqi_agent.subprocess.run") as execution:
            for command in (["rm", "-rf", "/"], ["npm", "install"], ["sh", "-c", "pwd"], ["node", "server.js"]):
                with self.assertRaises(ValueError):
                    self.access.terminal(command)
            execution.assert_not_called()

    def test_test_process_does_not_inherit_api_key(self):
        result = subprocess.CompletedProcess(app.TARGET_COMMAND, 0, "# tests 1\n# pass 1\n# fail 0\n", "")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "local-placeholder"}), \
             patch("jieqi_agent.subprocess.run", return_value=result) as execution:
            self.access.terminal(app.TARGET_COMMAND)
        self.assertNotIn("OPENAI_API_KEY", execution.call_args.kwargs["env"])
        self.assertFalse(execution.call_args.kwargs["shell"])

    def test_modified_or_replaced_file_is_not_overwritten(self):
        self.red()
        (self.root / app.CODE).write_text("user change\n")
        with self.assertRaises(ValueError):
            self.access.write_file(app.CODE, "original", "updated", app.digest("original\n"))
        self.assertEqual((self.root / app.CODE).read_text(), "user change\n")

    def test_pipeline_accepts_red_then_source_change_and_two_green_runs(self):
        self.red()
        self.access.write_file(app.CODE, "original", "updated", app.digest("original\n"))
        for command in (app.TARGET_COMMAND, app.ALL_COMMAND):
            result = subprocess.CompletedProcess(command, 0, "# tests 1\n# pass 1\n# fail 0\n", "")
            with patch("jieqi_agent.subprocess.run", return_value=result):
                self.access.terminal(command)
        self.assertTrue(self.access.verified())
        self.assertTrue(self.access.current[app.TEST].startswith("original\n"))

    def test_read_suspected_key_fails_without_exposing_it(self):
        text = "sk-" + "placeholder" * 3
        (self.root / app.CODE).write_text(text)
        self.access.current[app.CODE] = text
        with self.assertRaises(ValueError) as error:
            self.access.read_files([app.CODE])
        self.assertNotIn(text, str(error.exception))

    def make_written_checkpoint(self):
        read = self.access.read_files(list(app.READ_RANGES))
        args = {"path": app.TEST, "old_text": "",
                "new_text": 'test("存储写入失败：新增断言", () => { button.click(); assert.equal(1, 1); });\n',
                "expected_sha256": app.digest(self.access.current[app.TEST])}
        result = self.access.write_file(**args)
        prior = {"project": str(self.root), "verified": False, "red_evidence": None, "commands": [],
                 "diffs": {app.CODE: "", app.TEST: result["diff"]},
                 "usage": {"requests": 2, "tools": 2, "input_tokens": 50866, "output_tokens": 2341,
                           "known_estimated_rmb": .187713, "unreported_requests": 0,
                           "warnings": [{"turn": 1, "local_bytes": 2774, "input_tokens": 23975},
                                        {"turn": 1, "local_bytes": 13431, "input_tokens": 26891}]},
                 "tools": [{"call_id": "saved_read", "name": "read_files", "arguments": {"paths": list(app.READ_RANGES)}, "output": read},
                           {"call_id": "saved_write", "name": "write_file", "arguments": args, "output": result}]}
        path = self.root / "agent_jieqi_state_written.json"
        path.write_text(json.dumps(prior))
        return path

    def test_written_checkpoint_preserves_baseline_and_rejects_unrecorded_changes(self):
        path = self.make_written_checkpoint()
        with patch("jieqi_agent.PROJECT", self.root), patch("jieqi_agent.LAB", self.root):
            restored = app.ProjectAccess(self.root)
            prior, history = app.restore_read_checkpoint(path, restored)
            self.assertEqual(restored.initial[app.TEST], "original\n")
            self.assertEqual(restored.current[app.TEST], self.access.current[app.TEST])
            self.assertEqual(sum(item.get("type") == "function_call_output" for item in history), 2)
            (self.root / app.TEST).write_text(self.access.current[app.TEST] + "external change\n")
            with self.assertRaises(ValueError):
                app.restore_read_checkpoint(path, app.ProjectAccess(self.root))

    def test_new_test_can_be_repaired_but_original_assertions_and_frozen_tests_are_protected(self):
        self.make_written_checkpoint()
        before = self.access.current[app.TEST]
        self.access.write_file(app.TEST, "button.click()", "button.listeners.click()", app.digest(before))
        self.assertTrue(self.access.current[app.TEST].startswith("original\n"))
        with self.assertRaises(ValueError):
            self.access.write_file(app.TEST, "assert.equal(1, 1);", "/* removed */", app.digest(self.access.current[app.TEST]))
        self.access.red_evidence = {"tests_sha256": app.digest(self.access.current[app.TEST])}
        with self.assertRaises(ValueError):
            self.access.write_file(app.TEST, "button.listeners.click()", "button.click()", app.digest(self.access.current[app.TEST]))

    def test_written_checkpoint_sdk_resume_keeps_previous_work_and_two_yuan_budget(self):
        checkpoint = self.make_written_checkpoint()
        bodies = []
        def transport(request):
            body = json.loads(request.content)
            bodies.append(body)
            index = len(bodies)
            if index == 1:
                self.assertEqual(sum(x.get("type") == "function_call_output" for x in body["input"]), 2)
                name, args = "write_file", {"path": app.TEST, "old_text": "button.click()", "new_text": "button.listeners.click()",
                    "expected_sha256": app.digest(self.access.current[app.TEST])}
            elif index == 2:
                name, args = "terminal", {"command": app.TARGET_COMMAND}
            elif index == 3:
                name, args = "write_file", {"path": app.CODE, "old_text": "original", "new_text": "updated",
                    "expected_sha256": app.digest(self.access.current[app.CODE])}
            elif index in (4, 5):
                name, args = "terminal", {"command": app.TARGET_COMMAND if index == 4 else app.ALL_COMMAND}
            else:
                self.assertEqual(index, 6)
                output = [{"type": "message", "role": "assistant", "id": "finished", "status": "completed",
                           "content": [{"type": "output_text", "text": "完成", "annotations": []}]}]
            if index != 6:
                output = [{"type": "function_call", "id": f"function_{index}", "call_id": f"next_{index}",
                           "name": name, "arguments": json.dumps(args), "status": "completed"}]
            return httpx2.Response(200, json={"id": f"response_{index}", "object": "response", "created_at": 0,
                "status": "completed", "model": "gpt-6-sol", "output": output,
                "usage": {"input_tokens": 24000, "output_tokens": 100, "total_tokens": 24100}})
        def client(**kwargs):
            self.assertEqual(kwargs["max_retries"], 0)
            return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport), trust_env=False), **kwargs)
        def process(command, **kwargs):
            if command == ["git", "status", "--porcelain"]:
                return subprocess.CompletedProcess(command, 0, " M room.js\n M tests/room-client.test.cjs\n", "")
            if command == app.TARGET_COMMAND and not self.access.red_evidence:
                return subprocess.CompletedProcess(command, 1, "not ok 1 - 存储写入失败：新增断言\ncode: ERR_ASSERTION\n# fail 1\n", "")
            return subprocess.CompletedProcess(command, 0, "# pass 1\n# fail 0\n", "")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "local-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1"}, clear=True), \
             patch("jieqi_agent.preflight", return_value=self.access), patch("jieqi_agent.PROJECT", self.root), \
             patch("jieqi_agent.LAB", self.root), patch("jieqi_agent.AsyncOpenAI", side_effect=client), \
             patch("jieqi_agent.subprocess.run", side_effect=process), \
             patch("socket.socket.connect", side_effect=AssertionError("禁止真实网络")), \
             patch("socket.socket.connect_ex", side_effect=AssertionError("禁止真实网络")):
            self.assertEqual(asyncio.run(app.run(checkpoint, budget_rmb=2)), 0)
        records = [p for p in self.root.glob("agent_jieqi_state_*.json") if p != checkpoint]
        saved = json.loads(records[0].read_text())
        self.assertTrue(saved["verified"])
        self.assertEqual(saved["budget_rmb"], 2)
        self.assertEqual(saved["usage"]["requests"], 8)
        self.assertEqual(saved["usage"]["tools"], 7)
        self.assertEqual(saved["usage"]["input_tokens"], 50866 + 6 * 24000)
        self.assertGreater(saved["usage"]["known_estimated_rmb"], .187713)
        self.assertTrue(self.access.current[app.TEST].startswith("original\n"))
        self.assertEqual(self.access.current[app.TEST].count('test("存储写入失败'), 1)
        self.assertEqual([r["returncode"] for r in saved["commands"]], [1, 0, 0])

    def make_repaired_checkpoint(self):
        path = self.make_written_checkpoint()
        prior = json.loads(path.read_text())
        args = {"path": app.TEST, "old_text": "assert.equal(1, 1);",
                "new_text": "assert.deepEqual(resumes[0][1], { sessionToken: scenario.token, takeover: true });",
                "expected_sha256": app.digest(self.access.current[app.TEST])}
        output = self.access.write_file(**args)
        prior["tools"].append({"call_id": "fixed_test", "name": "write_file", "arguments": args, "output": output})
        result = subprocess.CompletedProcess(app.TARGET_COMMAND, 1,
            "not ok 1 - 存储写入失败：新增断言\ncode: ERR_ASSERTION\n# tests 1\n# fail 1\n", "")
        with patch("jieqi_agent.subprocess.run", return_value=result):
            output = self.access.terminal(app.TARGET_COMMAND)
        prior["tools"].append({"call_id": "red_test", "name": "terminal", "arguments": {"command": app.TARGET_COMMAND}, "output": output})
        args = {"path": app.CODE, "old_text": "original", "new_text": "updated",
                "expected_sha256": app.digest(self.access.current[app.CODE])}
        output = self.access.write_file(**args)
        prior["tools"].append({"call_id": "repair", "name": "write_file", "arguments": args, "output": output})
        prior.update({"red_evidence": self.access.red_evidence, "commands": self.access.commands,
            "diffs": {n: app.build_diff(self.access.initial[n], self.access.current[n], n)["unified_diff"] for n in app.WRITABLE},
            "usage": {"requests": 10, "tools": 5, "input_tokens": 328711, "output_tokens": 5811,
                      "known_estimated_rmb": 1.073298, "unreported_requests": 0, "warnings": []},
            "last_model_input_bytes": 81133, "last_model_input_tokens": 37495})
        path.write_text(json.dumps(prior))
        return path

    def test_repaired_checkpoint_restores_real_baseline_and_rejects_changed_disk(self):
        path = self.make_repaired_checkpoint()
        with patch("jieqi_agent.PROJECT", self.root), patch("jieqi_agent.LAB", self.root):
            restored = app.ProjectAccess(self.root)
            prior, history = app.restore_read_checkpoint(path, restored)
            self.assertEqual(restored.initial[app.CODE], "original\n")
            self.assertEqual(restored.current[app.CODE], "updated\n")
            self.assertEqual(len(restored.commands), 1)
            self.assertEqual(sum(x.get("type") == "function_call_output" for x in history), 5)
            (self.root / app.CODE).write_text("external\n")
            with self.assertRaises(ValueError):
                app.restore_read_checkpoint(path, app.ProjectAccess(self.root))

    def test_frozen_comparison_only_allows_equivalent_normalization_and_keeps_original_evidence(self):
        path = self.make_repaired_checkpoint()
        with patch("jieqi_agent.PROJECT", self.root), patch("jieqi_agent.LAB", self.root):
            restored = app.ProjectAccess(self.root)
            app.restore_read_checkpoint(path, restored)
        plain = "assert.deepEqual(resumes[0][1], { sessionToken: scenario.token, takeover: true });"
        normalized = "assert.deepEqual(JSON.parse(JSON.stringify(resumes[0][1])), { sessionToken: scenario.token, takeover: true });"
        before = app.digest(restored.current[app.TEST])
        with self.assertRaises(ValueError):
            restored.write_file(app.TEST, plain, normalized.replace("true", "false"), before)
        restored.write_file(app.TEST, plain, normalized, before)
        self.assertEqual(restored.red_evidence["original_tests_sha256"], before)
        self.assertEqual(restored.red_evidence["tests_sha256"], app.digest(restored.current[app.TEST]))
        self.assertEqual(len(restored.frozen_test_adjustments), 1)
        self.assertTrue(restored.current[app.TEST].startswith("original\n"))

    def test_repaired_checkpoint_finishes_via_sdk_without_repeating_red_or_repair(self):
        checkpoint = self.make_repaired_checkpoint()
        bodies = []
        plain = "assert.deepEqual(resumes[0][1], { sessionToken: scenario.token, takeover: true });"
        normalized = "assert.deepEqual(JSON.parse(JSON.stringify(resumes[0][1])), { sessionToken: scenario.token, takeover: true });"
        def transport(request):
            body = json.loads(request.content)
            bodies.append(body)
            index = len(bodies)
            if index == 1:
                self.assertEqual(sum(x.get("type") == "function_call_output" for x in body["input"]), 5)
                name, args = "write_file", {"path": app.TEST, "old_text": plain, "new_text": normalized,
                    "expected_sha256": app.digest(self.access.current[app.TEST])}
            elif index in (2, 3):
                name, args = "terminal", {"command": app.TARGET_COMMAND if index == 2 else app.ALL_COMMAND}
            else:
                self.assertEqual(index, 4)
                output = [{"type": "message", "role": "assistant", "id": "finished", "status": "completed",
                    "content": [{"type": "output_text", "text": "验收完成", "annotations": []}]}]
            if index != 4:
                output = [{"type": "function_call", "id": f"function_{index}", "call_id": f"finish_{index}",
                           "name": name, "arguments": json.dumps(args), "status": "completed"}]
            return httpx2.Response(200, json={"id": f"response_{index}", "object": "response", "created_at": 0,
                "status": "completed", "model": "gpt-6-sol", "output": output,
                "usage": {"input_tokens": 26000, "output_tokens": 100, "total_tokens": 26100}})
        def client(**kwargs):
            self.assertEqual(kwargs["max_retries"], 0)
            return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport), trust_env=False), **kwargs)
        def process(command, **kwargs):
            if command == ["git", "status", "--porcelain"]:
                return subprocess.CompletedProcess(command, 0, " M room.js\n M tests/room-client.test.cjs\n", "")
            return subprocess.CompletedProcess(command, 0, "# tests 1\n# pass 1\n# fail 0\n", "")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "local-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1"}, clear=True), \
             patch("jieqi_agent.preflight", return_value=self.access), patch("jieqi_agent.PROJECT", self.root), \
             patch("jieqi_agent.LAB", self.root), patch("jieqi_agent.AsyncOpenAI", side_effect=client), \
             patch("jieqi_agent.subprocess.run", side_effect=process), \
             patch("socket.socket.connect", side_effect=AssertionError("禁止真实网络")), \
             patch("socket.socket.connect_ex", side_effect=AssertionError("禁止真实网络")):
            self.assertEqual(asyncio.run(app.run(checkpoint, budget_rmb=2)), 0)
        records = [p for p in self.root.glob("agent_jieqi_state_*.json") if p != checkpoint]
        saved = json.loads(records[0].read_text())
        self.assertTrue(saved["verified"])
        self.assertEqual(saved["usage"]["requests"], 14)
        self.assertEqual(saved["usage"]["input_tokens"], 328711 + 104000)
        self.assertEqual([c["returncode"] for c in saved["commands"]], [1, 0, 0])
        self.assertEqual(len(saved["frozen_test_adjustments"]), 1)

    def test_actual_sdk_loop_records_red_edit_green_and_full_pass_without_network(self):
        self.exercise_sdk()

    def test_resume_reuses_read_result_and_accumulates_prior_cost_without_network(self):
        self.exercise_sdk(resume=True)

    def exercise_sdk(self, resume=False):
        checkpoint = None
        if resume:
            checkpoint_output = self.access.read_files(list(app.READ_RANGES))
            checkpoint = self.root / "agent_jieqi_state_checkpoint.json"
            checkpoint.write_text(json.dumps({"project": str(self.root), "verified": False,
                "red_evidence": None, "commands": [], "diffs": {n: "" for n in app.WRITABLE},
                "usage": {"requests": 1, "tools": 1, "input_tokens": 23975, "output_tokens": 148,
                          "known_estimated_rmb": .074145, "unreported_requests": 0,
                          "warnings": [{"turn": 1, "local_bytes": 2774}]},
                "tools": [{"call_id": "call_1", "name": "read_files",
                           "arguments": {"paths": list(app.READ_RANGES)}, "output": checkpoint_output}]}))
        bodies = []
        def http_handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            index = len(bodies) + int(resume)
            if resume and len(bodies) == 1:
                saved = [x for x in body["input"] if x.get("type") == "function_call_output"]
                self.assertEqual(len(saved), 1)
                self.assertEqual(ast.literal_eval(saved[0]["output"]), checkpoint_output)
            self.assertEqual(body["model"], "gpt-6-sol")
            self.assertEqual(body["max_output_tokens"], app.OUTPUT_TOKENS)
            self.assertFalse(body["store"])
            if index == 1:
                name, args = "read_files", {"paths": list(app.READ_RANGES)}
            elif index == 2:
                name, args = "write_file", {"path": app.TEST, "old_text": "",
                    "new_text": 'test("存储写入失败：新增断言", () => {});\n',
                    "expected_sha256": app.digest(self.access.current[app.TEST])}
            elif index == 3:
                name, args = "terminal", {"command": app.TARGET_COMMAND}
            elif index == 4:
                result = next(ast.literal_eval(x["output"]) for x in body["input"]
                              if x.get("type") == "function_call_output" and x["call_id"] == "call_3")
                self.assertEqual(result["returncode"], 1)
                self.assertTrue(result["red_evidence"])
                name, args = "write_file", {"path": app.CODE, "old_text": "original", "new_text": "updated",
                    "expected_sha256": app.digest(self.access.current[app.CODE])}
            elif index in (5, 6):
                name, args = "terminal", {"command": app.TARGET_COMMAND if index == 5 else app.ALL_COMMAND}
            else:
                self.assertEqual(index, 7)
                output = [{"type": "message", "role": "assistant", "id": "message_done", "status": "completed",
                           "content": [{"type": "output_text", "text": "修复已按测试验收。", "annotations": []}]}]
            if index != 7:
                output = [{"type": "function_call", "id": f"function_{index}", "call_id": f"call_{index}",
                           "name": name, "arguments": json.dumps(args), "status": "completed"}]
            return httpx2.Response(200, json={"id": f"response_{index}", "object": "response", "created_at": 0,
                "status": "completed", "model": "gpt-6-sol", "output": output,
                "usage": {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200}})
        def client_factory(**kwargs):
            self.assertEqual(kwargs["max_retries"], 0)
            return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(http_handler), trust_env=False), **kwargs)
        def process(command, **kwargs):
            if command == ["git", "status", "--porcelain"]:
                return subprocess.CompletedProcess(command, 0, " M room.js\n M tests/room-client.test.cjs\n", "")
            if command == app.TARGET_COMMAND and not self.access.red_evidence:
                return subprocess.CompletedProcess(command, 1, "not ok 1 - 存储写入失败：新增断言\ncode: ERR_ASSERTION\n# fail 1\n", "")
            return subprocess.CompletedProcess(command, 0, "# pass 1\n# fail 0\n", "")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "local-test-placeholder", "OPENAI_BASE_URL": "https://api.likeai520.cc/v1"}, clear=True), \
             patch("jieqi_agent.preflight", return_value=self.access), patch("jieqi_agent.PROJECT", self.root), \
             patch("jieqi_agent.LAB", self.root), patch("jieqi_agent.AsyncOpenAI", side_effect=client_factory), \
             patch("jieqi_agent.subprocess.run", side_effect=process), \
             patch("socket.socket.connect", side_effect=AssertionError("不能建立外网连接")), \
             patch("socket.socket.connect_ex", side_effect=AssertionError("不能建立外网连接")):
            self.assertEqual(asyncio.run(app.run(checkpoint)), 0)
        records = [p for p in self.root.glob("agent_jieqi_state_*.json") if p != checkpoint]
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertTrue(record["verified"])
        self.assertEqual(record["usage"]["requests"], 7)
        self.assertEqual(record["usage"]["tools"], 6)
        self.assertEqual(len(bodies), 6 if resume else 7)
        if resume:
            self.assertEqual(record["usage"]["input_tokens"], 23975 + 600)
            self.assertEqual(record["usage"]["output_tokens"], 148 + 600)
            self.assertGreater(record["usage"]["known_estimated_rmb"], .074145)
        self.assertEqual([r["returncode"] for r in record["commands"]], [1, 0, 0])
        self.assertNotIn("local-test-placeholder", records[0].read_text())


class BudgetTests(unittest.TestCase):
    def test_response_cost_above_one_yuan_can_continue_but_two_yuan_stops(self):
        recorder = app.ProjectRecorder(budget_rmb=2)
        recorder.model_input_bytes.append(1000)
        recorder.model_inputs.append({})
        first = SimpleNamespace(usage=SimpleNamespace(input_tokens=400000, output_tokens=100), output=[])
        second = SimpleNamespace(usage=SimpleNamespace(input_tokens=400000, output_tokens=100), output=[])
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(recorder.on_llm_end(None, None, first))
        self.assertGreater(recorder.spent_usd() * 10, 1)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(app.LoopStopped):
            asyncio.run(recorder.on_llm_end(None, None, second))

    def test_two_yuan_limit_is_used_before_and_after_model_requests(self):
        prior = {"usage": {"requests": 2, "input_tokens": 50866, "output_tokens": 2341,
            "warnings": [{"local_bytes": 13431, "input_tokens": 26891}]}, "tools": []}
        recorder = app.ProjectRecorder(prior, budget_rmb=2)
        self.assertEqual(recorder.max_model_requests, 8)
        self.assertAlmostEqual(recorder.spent_usd() * 10, .187713)
        with contextlib.redirect_stdout(io.StringIO()):
            recorder.check_projected_cost(8, 30000)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(app.LoopStopped):
            recorder.check_projected_cost(8, 100000)

    def test_initial_seven_requests_fit_below_one_yuan(self):
        with contextlib.redirect_stdout(io.StringIO()):
            app.ProjectRecorder().check_projected_cost(7, 5000)

    def test_more_than_budget_stops_before_request(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(app.LoopStopped):
            app.ProjectRecorder().check_projected_cost(8, 50000)

    def test_recorded_read_resume_fits_without_raising_budget(self):
        prior = {"usage": {"requests": 1, "input_tokens": 23975, "output_tokens": 148,
                           "warnings": [{"local_bytes": 2774}]}, "tools": []}
        recorder = app.ProjectRecorder(prior)
        self.assertEqual(recorder.max_model_requests, 6)
        self.assertAlmostEqual(recorder.spent_usd() * 10, .074145)
        with contextlib.redirect_stdout(io.StringIO()):
            recorder.check_projected_cost(6, 15201)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(app.LoopStopped):
            recorder.check_projected_cost(6, 50000)


if __name__ == "__main__":
    unittest.main()
