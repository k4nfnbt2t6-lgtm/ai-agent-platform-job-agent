"""首次真实项目任务：只读获准片段，只改两个文件，只运行指定测试。"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from agents import Agent, MaxTurnsExceeded, ModelSettings, OpenAIResponsesModel, RunConfig, Runner, function_tool
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.types.shared import Reasoning

from agent import LoopRecorder, LoopStopped
from diff_tools import build_diff


LAB = Path(__file__).resolve().parent
PROJECT = Path(os.environ.get("JIEQI_PROJECT_DIR", str(Path.home() / "Desktop" / "jieqi.online")))
CODE = "room.js"
TEST = "tests/room-client.test.cjs"
WRITABLE = {CODE, TEST}
# 这些是真实源码片段；不提供修复答案或预先写好的回归测试。
READ_RANGES = {
    CODE: [(26, 33), (66, 79), (86, 110), (136, 180), (271, 295)],
    TEST: [(1, 41), (341, 375), (769, 782)],
    "client-session.js": [(19, 44)],
    "tests/helpers.cjs": [(7, 33), (105, 110)],
}
TARGET_COMMAND = ["node", "--test", "--test-reporter=tap", TEST]
ALL_COMMAND = ["npm", "test"]
MAX_REQUESTS, MAX_TOOLS, OUTPUT_TOKENS = 7, 6, 2048
TASK = (
    "修复揭棋项目：浏览器 localStorage 写入失败后，同一页面短暂断线无法恢复已加入房间。"
    "范围只有 room.js 与 tests/room-client.test.cjs；不承诺刷新或关闭后的恢复。"
    "先用 read_files 阅读四个获准文件片段；再自主编写追加回归测试，测试名以‘存储写入失败’开头，"
    "使用现有 page/storage 帮助函数，模拟 localStorage.setItem 抛出 QuotaExceededError。"
    "测试覆盖创建和加入房间后同页面断线恢复：使用本页面凭证、只恢复一次、恢复成功后身份正确。"
    "用 terminal 运行定向测试，取得真实断言失败后才修改 room.js；测试一旦确认复现就固定不再改。"
    "修复后再次运行定向测试，通过后运行 npm test。保持原有跨窗口、主动退出、过期身份及不重发走棋规则。"
    "保留全部原测试；目标缺陷复现前可以局部修正本任务新增测试的执行方式，禁止删除断言来通过测试。"
    "复现后固定测试。不修改服务端、棋规、依赖、配置或任何其他文件。"
    "每轮一个工具，工具返回的真实结果决定下一步。最终据真实测试与 Diff 报告修改及验收情况。"
    "拒绝操作或费用不足时停止，不绕过权限、不切换接口、不自动重试网络请求。"
)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProjectAccess:
    def __init__(self, root=PROJECT):
        self.root = root.resolve(strict=True)
        self.initial, self.current, self.identities = {}, {}, {}
        self.read_grants = set()
        self.red_evidence = None
        self.green = self.all_green = False
        self.commands = []
        self.allow_comparison_normalization = False
        self.frozen_test_adjustments = []
        for name in READ_RANGES:
            path = self.path(name)
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("获准源码必须是独立普通文件。")
            text = path.read_text(encoding="utf-8")
            self.initial[name] = self.current[name] = text
            self.identities[name] = (info.st_dev, info.st_ino)

    def path(self, name):
        if name not in READ_RANGES:
            raise ValueError("路径未授权；只接受四个明确列出的项目相对路径。")
        path = self.root / name
        if any(part.is_symlink() for part in (path, *path.parents) if part != self.root.parent):
            raise ValueError("不接受符号链接。")
        if path.resolve(strict=True) != path or not path.is_relative_to(self.root):
            raise ValueError("路径不在授权项目中。")
        return path

    def checked(self, name):
        path = self.path(name)
        info = path.stat(follow_symlinks=False)
        if (info.st_dev, info.st_ino) != self.identities[name] or info.st_nlink != 1:
            raise ValueError("源码文件身份已变化，停止以保护现有改动。")
        text = path.read_text(encoding="utf-8")
        if text != self.current[name]:
            raise ValueError("文件被其他程序修改，停止以保护现有改动。")
        return text

    def read_files(self, paths):
        if not paths or len(paths) != len(set(paths)):
            raise ValueError("请提交不重复的获准文件名。")
        result = []
        for name in paths:
            text = self.checked(name)
            if re.search(r"sk-[A-Za-z0-9_-]{12,}", text):
                raise ValueError("获准文件含疑似密钥，停止读取上传；请先本地检查，不显示密钥。")
            lines = text.splitlines(keepends=True)
            self.read_grants.add(name)
            ranges = list(READ_RANGES[name])
            if name == TEST and text != self.initial[TEST]:
                ranges.append((len(self.initial[TEST].splitlines()) + 1, len(lines)))
            result.append({"path": name, "sha256": digest(text), "total_lines": len(lines),
                "fragments": [{"start_line": start, "end_line": end,
                               "content": "".join(lines[start - 1:end])} for start, end in ranges]})
        return {"ok": True, "files": result}

    def write_file(self, path, old_text, new_text, expected_sha256):
        if path not in WRITABLE or path not in self.read_grants:
            raise ValueError("写入未授权，或尚未读取该文件。")
        before = self.checked(path)
        if digest(before) != expected_sha256:
            raise ValueError("文件原内容校验失败，不覆盖。")
        if not new_text or re.search(r"sk-[A-Za-z0-9_-]{12,}", new_text):
            raise ValueError("拒绝空修改或疑似密钥。")
        if path == TEST:
            if self.red_evidence:
                # 仅允许本次已核实的跨 VM 对象比较做等价归一化，预期值不可变。
                expected = "{ sessionToken: scenario.token, takeover: true }"
                plain = f"assert.deepEqual(resumes[0][1], {expected});"
                normalized = f"assert.deepEqual(JSON.parse(JSON.stringify(resumes[0][1])), {expected});"
                tail = before[len(self.initial[TEST]):]
                if (not self.allow_comparison_normalization or old_text.strip() != plain
                        or new_text.strip() != normalized or tail.count(old_text) != 1):
                    raise ValueError("测试已固定；只允许获准的跨 VM 比较等价归一化，不改预期值。")
            original = self.initial[TEST]
            if not before.startswith(original):
                raise ValueError("原有测试发生变化，停止。")
            tail = before[len(original):]
            if old_text:
                if tail.count(old_text) != 1:
                    raise ValueError("只能唯一匹配并修正本任务追加的测试，不修改原测试。")
                changed_tail = tail.replace(old_text, new_text, 1)
                if changed_tail.count("assert.") < tail.count("assert."):
                    raise ValueError("不得通过减少新增测试断言修复用例。")
                after = original + changed_tail
            else:
                if "存储写入失败" not in new_text:
                    raise ValueError("新增测试须标明该真实缺陷。")
                after = before + "\n" + new_text
        else:
            if not self.red_evidence:
                raise ValueError("必须先取得新增测试的真实断言失败，才允许修复。")
            if not old_text or before.count(old_text) != 1:
                raise ValueError("局部修改必须唯一匹配原文。")
            after = before.replace(old_text, new_text, 1)
        if len(after.encode("utf-8")) > 128_000:
            raise ValueError("修改超出本次文件范围。")
        self.checked(path)
        descriptor = os.open(self.path(path), os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r+b") as stream:
            info = os.fstat(stream.fileno())
            if (info.st_dev, info.st_ino) != self.identities[path] or stream.read().decode("utf-8") != before:
                raise ValueError("写入前文件身份或原文变化，停止。")
            stream.seek(0)
            stream.write(after.encode("utf-8"))
            stream.truncate()
        self.current[path] = after
        if path == TEST and self.red_evidence:
            self.frozen_test_adjustments.append({"reason": "跨 VM 对象等价归一化",
                "before_sha256": digest(before), "after_sha256": digest(after)})
            self.allow_comparison_normalization = False
            self.red_evidence.setdefault("original_tests_sha256", self.red_evidence["tests_sha256"])
            self.red_evidence["tests_sha256"] = digest(after)
        self.green = self.all_green = False
        report = build_diff(before, self.checked(path), path)
        print("实际 Diff：\n" + report["unified_diff"], flush=True)
        return {"ok": True, "path": path, "sha256": digest(after), "diff": report["unified_diff"]}

    def terminal(self, command):
        if command not in (TARGET_COMMAND, ALL_COMMAND):
            raise ValueError("命令未授权，仅允许指定定向测试与 npm test。")
        if command == ALL_COMMAND and not self.green:
            raise ValueError("先通过定向测试，才运行全量回归。")
        for name in READ_RANGES:
            self.checked(name)
        env = {name: os.environ[name] for name in ("PATH", "LANG", "TMPDIR") if name in os.environ}
        try:
            process = subprocess.run(command, cwd=self.root, env=env, capture_output=True,
                                     text=True, timeout=90, shell=False)
        except subprocess.TimeoutExpired as error:
            raise ValueError("测试超过 90 秒，停止，不自动重试。") from error
        output = process.stdout + process.stderr
        # 日志完整保存在本机记录，模型收到测试摘要及失败依据。
        failed = re.findall(r"^not ok \d+ - (.+)$", output, flags=re.M)
        summary = [line for line in output.splitlines() if re.match(r"^(?:#|ℹ) (?:tests|suites|pass|fail|cancelled|skipped|todo|duration_ms)\b", line)]
        details = ""
        if process.returncode:
            start = output.find("not ok")
            details = output[max(0, start):max(0, start) + 3000]
        record = {"command": command, "returncode": process.returncode,
                  "summary": summary, "failure_details": details, "failed_tests": failed}
        self.commands.append(record | {"full_output": output})
        if command == TARGET_COMMAND:
            if not self.red_evidence and process.returncode == 1 and failed \
                    and all(title.startswith("存储写入失败") for title in failed) \
                    and "ERR_ASSERTION" in output and "ERR_TEST_FAILURE" not in output:
                self.red_evidence = {"tests_sha256": digest(self.current[TEST]), "command_index": len(self.commands) - 1}
            if process.returncode == 0 and self.red_evidence:
                self.green = True
        elif process.returncode == 0:
            self.all_green = True
        return {"ok": True, **record, "red_evidence": bool(self.red_evidence),
                "targeted_passed": self.green, "all_passed": self.all_green}

    def verified(self):
        return bool(self.red_evidence and self.green and self.all_green
                    and digest(self.checked(TEST)) == self.red_evidence["tests_sha256"]
                    and self.checked(TEST).startswith(self.initial[TEST])
                    and self.checked(CODE) != self.initial[CODE])


class ProjectRecorder(LoopRecorder):
    def __init__(self, prior=None, budget_rmb=1):
        usage = prior["usage"] if prior else {}
        self.budget_rmb = budget_rmb
        finishing = bool(prior and prior.get("red_evidence"))
        # 有真实复现及已写入补丁时，为收尾预留六轮，不重开整套任务。
        total_requests, total_tools = (usage.get("requests", 0) + 6, len(prior["tools"]) + 5) if finishing else ((10, 9) if budget_rmb == 2 else (MAX_REQUESTS, MAX_TOOLS))
        super().__init__(max_model_requests=total_requests - usage.get("requests", 0),
                         max_tool_calls=total_tools,
                         previous_input_tokens=usage.get("input_tokens", 0),
                         previous_output_tokens=usage.get("output_tokens", 0),
                         previous_requests=usage.get("requests", 0))
        last_warning = usage.get("warnings", [{}])[-1] if usage.get("warnings") else {}
        self.prior_input_bytes = prior.get("last_model_input_bytes", last_warning.get("local_bytes", 0)) if prior else 0
        self.prior_last_tokens = prior.get("last_model_input_tokens", last_warning.get("input_tokens", self.previous_input_tokens)) if prior else 0
        self.history = []
        if prior:
            self.tool_calls = list(prior["tools"])
            self.usage_warnings = list(usage["warnings"])

    def check_projected_cost(self, remaining_requests, input_bytes):
        # 已观测输入包含既有 Context；仅把新增长字节加入，避免重复计整段历史。
        if self.responses:
            index = len(self.responses) - 1
            growth = max(0, input_bytes - self.model_input_bytes[index])
            estimate = max(30000, self.responses[-1].usage.input_tokens + growth)
        elif self.previous_requests:
            growth = max(0, input_bytes - self.prior_input_bytes)
            estimate = max(30000, self.prior_last_tokens + growth)
        else:
            estimate = max(30000, input_bytes)
        projected = self.spent_usd() + remaining_requests * (estimate * 0.30 + OUTPUT_TOKENS * 1.50) / 1_000_000
        print(f"实战累计费用预估：约 {projected * 10:.4f} 元；已知约 {self.spent_usd() * 10:.4f} 元", flush=True)
        if projected > self.budget_rmb / 10:
            raise LoopStopped(f"预计完成剩余流程超过 {self.budget_rmb} 元，停止后续调用，等待预算确认。")

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        await super().on_llm_start(context, agent, system_prompt, input_items)
        self.history = json.loads(json.dumps(input_items, ensure_ascii=False))

    async def on_llm_end(self, context, agent, response):
        # 此入口的预算独立于旧课程的 1 元配置；累计以前的收费用量。
        self.responses.append(response)
        self.history.extend(item.model_dump(exclude_none=True) for item in response.output)
        incoming, outgoing = response.usage.input_tokens, response.usage.output_tokens
        print(f"本轮平台用量：输入 {incoming} token，输出 {outgoing} token", flush=True)
        if incoming <= 0 or outgoing < 0:
            raise LoopStopped("缺少可核对用量，停止后续请求；请核对平台账单。")
        size = self.model_input_bytes[-1]
        if (size < 16000 and incoming >= 20000) or outgoing > OUTPUT_TOKENS:
            self.usage_warnings.append({"turn": self.previous_requests + len(self.model_inputs),
                "input_tokens": incoming, "output_tokens": outgoing, "local_bytes": size})
            print("token 用量提醒：已记录偏高统计；继续按累计费用检查。", flush=True)
        if self.spent_usd() >= self.budget_rmb / 10:
            raise LoopStopped(f"已返回用量估算费用达到 {self.budget_rmb} 元，停止后续调用。")
        count = sum(getattr(item, "type", "") == "function_call" for item in response.output)
        if count > 1:
            raise LoopStopped("每轮只允许一个工具，本轮工具未执行。")
        if count:
            self.check_projected_cost(self.max_model_requests - len(self.model_inputs), size)

    async def on_tool_start(self, context, agent, tool):
        await super().on_tool_start(context, agent, tool)
        self.tool_calls[-1]["turn"] += self.previous_requests

    async def on_tool_end(self, context, agent, tool, result):
        record = next(r for r in self.tool_calls if r["call_id"] == context.tool_call_id)
        record["output"] = result
        self.history.append({"type": "function_call_output", "call_id": context.tool_call_id, "output": str(result)})
        print("工具真实结果：", json.dumps(result, ensure_ascii=False), flush=True)
        if not isinstance(result, dict) or not result.get("ok"):
            raise LoopStopped("工具未完成，停止。")


def create_tools(access):
    @function_tool(failure_error_function=None)
    def read_files(paths: list[str]) -> dict:
        """读取获准文件的真实相关片段和整文件 SHA256，可一次读取四个文件。"""
        return access.read_files(paths)

    @function_tool(failure_error_function=None)
    def write_file(path: str, old_text: str, new_text: str, expected_sha256: str) -> dict:
        """局部修改授权文件。追加测试 old_text 为空；修正新增测试或源码时唯一匹配 old_text。原测试不可改，复现后测试固定。SHA 来自读取或写入。"""
        return access.write_file(path, old_text, new_text, expected_sha256)

    @function_tool(failure_error_function=None)
    def terminal(command: list[str]) -> dict:
        """只运行获准定向测试或 npm test。非零返回码及失败断言交回模型，不自动重试。"""
        return access.terminal(command)
    return [read_files, write_file, terminal]


def preflight(resume=None):
    result = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT, capture_output=True, text=True, timeout=5)
    changed = {line[3:] for line in result.stdout.splitlines()}
    if result.returncode or (changed and not resume) or not changed <= WRITABLE:
        raise ValueError("真实项目须先保持干净；已有或中断改动不覆盖、不自动重做，请先查看记录。")
    access = ProjectAccess()
    print("实战项目：", PROJECT)
    print("可写文件：", sorted(WRITABLE))
    print("只读辅助文件：", sorted(set(READ_RANGES) - WRITABLE))
    print("允许命令：", TARGET_COMMAND, ALL_COMMAND)
    print("已有改动须与续跑记录逐项匹配；费用按命令指定的已授权累计预算检查，无自动重试。")
    return access


def restore_read_checkpoint(record_path, access):
    """核对读取或首次追加测试检查点，不重复执行过去的工具操作。"""
    path = Path(record_path).expanduser().resolve(strict=True)
    if path.parent != LAB or not path.name.startswith("agent_jieqi_state_") or path.suffix != ".json":
        raise ValueError("只接受本实验目录内的实战记录。")
    prior = json.loads(path.read_text(encoding="utf-8"))
    if prior.get("red_evidence"):
        return restore_execution_checkpoint(prior, access)
    usage, records = prior["usage"], prior["tools"]
    if (prior["project"] != str(PROJECT) or prior["verified"] or prior["red_evidence"]
            or prior["commands"] or prior["diffs"].get(CODE)
            or usage["requests"] not in (1, 2) or usage["tools"] != usage["requests"] or usage["unreported_requests"] != 0
            or usage["input_tokens"] <= 0 or usage["output_tokens"] < 0
            or len(records) != usage["tools"] or records[0]["name"] != "read_files"):
        raise ValueError("该记录不是已读取或首次追加测试的检查点；不自动重启，请先核对实际改动及费用。")
    call = records[0]
    if set(call["arguments"]["paths"]) != set(READ_RANGES):
        raise ValueError("检查点未读取完整获准上下文。")
    if len(records) == 2:
        write = records[1]
        args, result = write["arguments"], write["output"]
        if (write["name"] != "write_file" or args["path"] != TEST or args["old_text"]
                or not args["new_text"] or "存储写入失败" not in args["new_text"]
                or not isinstance(result, dict) or not result.get("ok")
                or result["sha256"] != digest(access.current[TEST])):
            raise ValueError("当前测试与已记录的追加写入不匹配，停止以保留现有改动。")
        suffix = "\n" + args["new_text"]
        if not access.current[TEST].endswith(suffix):
            raise ValueError("实际文件不是记录中原测试加新增测试，停止。")
        original = access.current[TEST][:-len(suffix)]
        if digest(original) != args["expected_sha256"]:
            raise ValueError("原有测试校验失败，停止。")
        access.initial[TEST] = original
        if build_diff(original, access.current[TEST], TEST)["unified_diff"] != prior["diffs"][TEST]:
            raise ValueError("现有 Diff 与检查点不一致。")
    elif any(prior["diffs"].values()):
        raise ValueError("只读检查点不应包含写入改动。")
    saved_files = call["output"].get("files", [])
    if not call["output"].get("ok") or {f["path"] for f in saved_files} != set(READ_RANGES):
        raise ValueError("读取证据不完整。")
    # 旧记录的片段范围保持原样，新上下文另从当前磁盘取得。
    for saved in saved_files:
        baseline = access.initial[saved["path"]]
        lines = baseline.splitlines(keepends=True)
        if digest(baseline) != saved["sha256"] or len(lines) != saved["total_lines"]:
            raise ValueError("检查点源码与当前磁盘不一致，不复用旧内容。")
        for fragment in saved["fragments"]:
            if "".join(lines[fragment["start_line"] - 1:fragment["end_line"]]) != fragment["content"]:
                raise ValueError("检查点片段未匹配真实原文。")
    access.read_grants.update(READ_RANGES)
    expected_cost = (usage["input_tokens"] * .30 + usage["output_tokens"] * 1.50) / 1_000_000 * 10
    if abs(expected_cost - usage["known_estimated_rmb"]) > 0.000001:
        raise ValueError("检查点用量和预算记录不一致。")
    print(f"复用 {len(records)} 次已完成工具操作；此前费用约 {expected_cost:.6f} 元计入同一预算。")
    history = []
    for record in records:
        history.extend([
            {"type": "function_call", "call_id": record["call_id"], "name": record["name"],
             "arguments": json.dumps(record["arguments"], ensure_ascii=False)},
            {"type": "function_call_output", "call_id": record["call_id"], "output": str(record["output"])},
        ])
    if len(records) == 2:
        related = access.read_files([TEST, "tests/helpers.cjs"])
        history.append({"role": "user", "content":
            "从已追加测试的状态继续，不能再次追加整套相同测试。老师已无收费核对：原有38项通过，新增两项在点击按钮时报 "
            "Cannot click create-room/join-room in page fixture，ERR_TEST_FAILURE，尚未复现目标缺陷。"
            "先依据真实测试工具修正新增测试执行方式；沿用原测试对VM对象的比较约定，不猜DOM接口。"
            "保留两个场景及其断言，获取目标缺陷的真实断言失败后再修复room.js。"
            "下面是本地读取并核对的当前测试及相关最少源码上下文（不是新模型工具调用）："
            + json.dumps(related, ensure_ascii=False)})
    return prior, history


def restore_execution_checkpoint(prior, access):
    """从真实复现和修改后的状态收尾；只在内存逆推并核对历史，不回滚磁盘。"""
    usage, records = prior["usage"], prior["tools"]
    if (prior["project"] != str(PROJECT) or prior["verified"] or usage["unreported_requests"]
            or usage["tools"] != len(records) or usage["requests"] < len(records)
            or not records or records[0]["name"] != "read_files"):
        raise ValueError("修复检查点不完整，停止，不能自动重做。")
    expected_cost = (usage["input_tokens"] * .30 + usage["output_tokens"] * 1.50) / 1_000_000 * 10
    if abs(expected_cost - usage["known_estimated_rmb"]) > .000001:
        raise ValueError("历史费用记录不一致。")
    baseline = dict(access.current)
    for record in reversed(records):
        if record["name"] not in ("read_files", "write_file", "terminal") or not isinstance(record["output"], dict) or not record["output"].get("ok"):
            raise ValueError("检查点含未完成或未授权的工具操作。")
        if record["name"] != "write_file":
            continue
        args, output = record["arguments"], record["output"]
        name, old, new = args["path"], args["old_text"], args["new_text"]
        if name not in WRITABLE or digest(baseline[name]) != output["sha256"]:
            raise ValueError("磁盘内容或中间写入版本与记录不一致。")
        if old:
            if baseline[name].count(new) != 1:
                raise ValueError("无法唯一核对历史修改，停止保护现有文件。")
            previous = baseline[name].replace(new, old, 1)
        elif name == TEST and baseline[name].endswith("\n" + new):
            previous = baseline[name][:-len("\n" + new)]
        else:
            raise ValueError("历史写入不符合授权方式。")
        if digest(previous) != args["expected_sha256"] or build_diff(previous, baseline[name], name)["unified_diff"] != output["diff"]:
            raise ValueError("历史原文、Diff 或校验值不一致。")
        baseline[name] = previous
    saved = records[0]["output"]["files"]
    if {f["path"] for f in saved} != set(READ_RANGES) or any(digest(baseline[f["path"]]) != f["sha256"] for f in saved):
        raise ValueError("逆推基线与初次真实读取不一致。")
    for name in WRITABLE:
        if build_diff(baseline[name], access.current[name], name)["unified_diff"] != prior["diffs"][name]:
            raise ValueError("当前 Diff 与检查点不一致。")
    if not access.current[TEST].startswith(baseline[TEST]):
        raise ValueError("原有测试被改动。")
    version = dict(baseline)
    command_index, red_version = 0, None
    evidence = prior["red_evidence"]
    for record in records:
        args, output = record["arguments"], record["output"]
        if record["name"] == "write_file":
            name = args["path"]
            version[name] = version[name].replace(args["old_text"], args["new_text"], 1) if args["old_text"] else version[name] + "\n" + args["new_text"]
        elif record["name"] == "terminal":
            if args["command"] not in (TARGET_COMMAND, ALL_COMMAND):
                raise ValueError("历史包含未授权命令。")
            command = prior["commands"][command_index]
            if command["command"] != args["command"] or command["returncode"] != output["returncode"]:
                raise ValueError("历史命令证据不一致。")
            if command_index == evidence["command_index"]:
                if (args["command"] != TARGET_COMMAND or output["returncode"] != 1
                        or not output["failed_tests"] or not all(t.startswith("存储写入失败") for t in output["failed_tests"])
                        or "ERR_ASSERTION" not in command["full_output"] or "ERR_TEST_FAILURE" in command["full_output"]):
                    raise ValueError("没有目标缺陷的有效断言失败证据。")
                red_version = digest(version[TEST])
            command_index += 1
    if (command_index != len(prior["commands"]) or red_version != evidence.get("original_tests_sha256", evidence["tests_sha256"])
            or digest(access.current[TEST]) != evidence["tests_sha256"]):
        raise ValueError("固定测试版本或复现证据不一致。")
    access.initial = baseline
    access.read_grants.update(READ_RANGES)
    access.red_evidence = dict(evidence)
    access.commands = list(prior["commands"])
    access.frozen_test_adjustments = list(prior.get("frozen_test_adjustments", []))
    access.allow_comparison_normalization = not access.frozen_test_adjustments
    history = []
    for record in records:
        history.extend([
            {"type": "function_call", "call_id": record["call_id"], "name": record["name"], "arguments": json.dumps(record["arguments"], ensure_ascii=False)},
            {"type": "function_call_output", "call_id": record["call_id"], "output": str(record["output"])},
        ])
    related = access.read_files([CODE, TEST])
    history.append({"role": "user", "content":
        "从已复现且已修改的状态收尾，不重做之前步骤。上次停于次数额度，费用未超过2元。"
        "老师刚才本地运行定向测试：38通过，新增2项在deepStrictEqual失败，Values have same structure but are not reference-equal。"
        "恢复请求次数已经为1；当前失败是跨VM对象比较。仅允许对这一处比较做等价JSON归一化，保持sessionToken/takeover预期和所有断言；调整会保留原复现版本及单独记录。"
        "同时核对临时凭证在回到首页或身份失效后的清理，避免重新恢复已放弃的身份。"
        "合并同文件必要局部修改，避免重复读取。随后运行定向测试，通过后npm test，最后据真实证据报告。"
        "当前核对的源码片段：" + json.dumps(related, ensure_ascii=False)})
    print(f"恢复已复现并修改的状态；累计已知约 {expected_cost:.6f} 元，历史操作不重复。")
    return prior, history


async def run(resume=None, budget_rmb=1):
    if budget_rmb not in (1, 2):
        raise ValueError("只支持本次已讨论的 1 元或 2 元累计预算。")
    key, url = (os.environ.get(n, "").strip() for n in ("OPENAI_API_KEY", "OPENAI_BASE_URL"))
    if not key or url.rstrip("/") != "https://api.likeai520.cc/v1":
        raise ValueError("请在原 Terminal 设置 OPENAI_API_KEY 与已授权的 OPENAI_BASE_URL；不要发到聊天。")
    access = preflight(resume)
    prior, saved_items = restore_read_checkpoint(resume, access) if resume else (None, [])
    recorder = ProjectRecorder(prior, budget_rmb)
    print(f"本次累计预算上限 {budget_rmb} 元；最多 {recorder.max_model_requests} 次新请求，原费用已累计。")
    final, success = None, False
    try:
        async with AsyncOpenAI(api_key=key, base_url=url, timeout=120, max_retries=0) as client:
            agent = Agent(name="Jieqi Project Agent", instructions=TASK,
                model=OpenAIResponsesModel(model="gpt-6-sol", openai_client=client), tools=create_tools(access),
                model_settings=ModelSettings(max_tokens=OUTPUT_TOKENS, reasoning=Reasoning(effort="none"),
                    store=False, tool_choice="auto", parallel_tool_calls=False))
            task = "执行已授权修复。获准读取：" + ", ".join(READ_RANGES) \
                   + "。定向命令：" + json.dumps(TARGET_COMMAND) + "；全量命令：" + json.dumps(ALL_COMMAND)
            if saved_items:
                task = [{"role": "user", "content": task}, *saved_items,
                        {"role": "user", "content": "从上面真实执行记录继续，不重复过去操作，按当前进度完成失败测试、修复及回归验收。"}]
            async with asyncio.timeout(1100):
                result = await Runner.run(agent, task, hooks=recorder, max_turns=recorder.max_model_requests,
                                          run_config=RunConfig(tracing_disabled=True))
            final = result.final_output
            success = access.verified()
    except (ValueError, LoopStopped, MaxTurnsExceeded) as error:
        print("实战已停止：", str(error).replace(key, "[已隐藏]"))
    except APIStatusError as error:
        print("中转站 HTTP 错误：", error.status_code, "；停止，不重试。")
    except APIConnectionError as error:
        print("连接或超时异常：", type(error).__name__, "；停止，不重试，扣费需查平台账单。")
    except Exception as error:
        reason, seen = error, set()
        while id(reason) not in seen:
            seen.add(id(reason))
            nested = reason.__cause__ or reason.__context__
            if nested is None:
                break
            reason = nested
        detail = str(reason).replace(key, "[已隐藏]") if isinstance(reason, (ValueError, LoopStopped)) else type(reason).__name__
        print("实战未完成：", detail, "；保留当前修改，不自动重试。")
    diffs = {}
    for name in WRITABLE:
        try:
            diffs[name] = build_diff(access.initial[name], access.checked(name), name)["unified_diff"]
        except (ValueError, OSError):
            success = False
            diffs[name] = "无法安全核对该文件；其他程序可能修改了它，未覆盖。"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT, capture_output=True, text=True, timeout=5)
    changed = {line[3:] for line in status.stdout.splitlines()}
    success = success and status.returncode == 0 and changed <= WRITABLE
    usage = {"requests": recorder.previous_requests + len(recorder.model_inputs), "tools": len(recorder.tool_calls),
             "input_tokens": recorder.previous_input_tokens + sum(r.usage.input_tokens for r in recorder.responses),
             "output_tokens": recorder.previous_output_tokens + sum(r.usage.output_tokens for r in recorder.responses),
             "known_estimated_rmb": recorder.spent_usd() * 10,
             "unreported_requests": len(recorder.model_inputs) - len(recorder.responses),
             "warnings": recorder.usage_warnings}
    payload = {"project": str(PROJECT), "verified": success, "red_evidence": access.red_evidence,
               "resumed_from": str(resume) if resume else None,
               "budget_rmb": budget_rmb,
               "frozen_test_adjustments": access.frozen_test_adjustments,
               "last_model_input_bytes": recorder.model_input_bytes[-1] if recorder.model_input_bytes else recorder.prior_input_bytes,
               "last_model_input_tokens": recorder.responses[-1].usage.input_tokens if recorder.responses else recorder.prior_last_tokens,
               "usage": usage, "tools": recorder.tool_calls, "commands": access.commands,
               "diffs": diffs, "final": final}
    text = json.dumps(payload, ensure_ascii=False, indent=2).replace(key, "[已隐藏]")
    descriptor, name = tempfile.mkstemp(prefix="agent_jieqi_state_", suffix=".json", dir=LAB)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text + "\n")
    for diff in diffs.values():
        print(diff, end="")
    print("Agent 最终回答：", final)
    print("真实项目修复验收：", "通过" if success else "未完成；保留实际改动，请先查看记录")
    print("累计用量：", json.dumps(usage, ensure_ascii=False))
    print("实战记录：", name)
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="发起真实模型请求及授权修改；默认仅只读检查")
    parser.add_argument("--allow-source-upload", action="store_true", help="明确同意将限定源码片段和测试摘要发到当前中转站")
    parser.add_argument("--resume", help="从已读取或首次追加测试的本地记录续跑，累计先前费用")
    parser.add_argument("--budget-rmb", type=int, choices=(1, 2), default=1, help="用户明确授权的本次累计预算（包含此前费用）")
    args = parser.parse_args()
    try:
        if args.run:
            if not args.allow_source_upload:
                raise ValueError("真实源码发送到中转站须明确授权；未发送请求。")
            return asyncio.run(run(args.resume, args.budget_rmb))
        if args.resume:
            raise ValueError("续跑须同时指定 --run；不会在只读模式调用模型。")
        preflight()
        print("仅只读检查完成；未修改揭棋文件，未发送 API 请求。")
        return 0
    except ValueError as error:
        print("配置/授权检查：", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
