"""Subagent 课程：子任务输入与结果边界，只允许本次人工数据。"""

import copy
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agents import function_tool
from pydantic import BaseModel, ConfigDict, StrictInt

from agent import LoopStopped
from background_tools import BackgroundTasks
from file_tools import FileAccess


class Delegation(BaseModel):
    path: str


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    total: StrictInt
    count: StrictInt
    dataset: str
    receipt: str


@dataclass(frozen=True)
class Example:
    numbers: list[int]
    target: int
    dataset: str
    parent_marker: str

    @classmethod
    def random(cls):
        numbers = [secrets.randbelow(400) + 100 for _ in range(3)]
        offset = 40 if secrets.randbelow(2) else -40
        return cls(numbers, sum(numbers) + offset, "data-" + secrets.token_hex(6), "parent-" + secrets.token_hex(6))


def prepare_subagent_file(project_dir, example):
    root = Path(project_dir).resolve(strict=True)
    descriptor, name = tempfile.mkstemp(prefix="agent_subagent_data_", suffix=".json", dir=root)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"numbers": example.numbers, "dataset": example.dataset}, stream, ensure_ascii=False)
        stream.write("\n")
    return Path(name)


class SubagentAccess:
    def __init__(self, project_dir, path, example):
        self.file = FileAccess(project_dir, path)
        self.path = self.file.relative_path
        self.initial_text = self.file.snapshot()
        self.example = example
        self.delegations = []
        self.read_result = None
        self.sum_result = None
        self.child_summary = None
        self.child_run = None
        self.judgement = None

    @property
    def task(self):
        return (f"请委派子 Agent 汇总人工报表 {self.path}，再由你判断是否达到目标值 {self.example.target}。"
                f"最终报告还要包含主任务标记 {self.example.parent_marker}。不要把目标值或主任务标记传给子 Agent。")

    def build_input(self, packet):
        # 当前 SDK 的结构化 input_builder 参数包含 params，而不是任意对话历史。
        params = packet["params"]
        if self.delegations or params != {"path": self.path}:
            raise LoopStopped("只允许委派一次，并且只能使用本次人工数据文件。")
        child_input = f"请读取人工数据文件 {self.path}，调用 sum_numbers 汇总 numbers，返回 path、total、count、dataset、receipt。"
        self.delegations.append({"arguments": copy.deepcopy(params), "child_input": child_input})
        print("委派已启动；子 Agent 独立输入：", child_input, flush=True)
        return child_input

    def read_file(self, path):
        if not self.delegations or self.read_result is not None or path != self.path:
            return {"ok": False, "error": "子 Agent 只可读取一次指定人工文件。"}
        result = self.file.read_file(path)
        if not result.get("ok") or result["content"] != self.initial_text:
            return {"ok": False, "error": "来源无法读取或已变化。"}
        try:
            data = json.loads(result["content"])
            if not isinstance(data, dict) or set(data) != {"numbers", "dataset"} or not isinstance(data["dataset"], str):
                raise ValueError("格式不正确。")
            BackgroundTasks.validate_numbers(data["numbers"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "人工数据格式不正确。"}
        self.read_result = copy.deepcopy(result)
        return result

    def sum_numbers(self, numbers):
        if self.read_result is None or self.sum_result is not None:
            return {"ok": False, "error": "先读取真实来源，再汇总一次。"}
        data = json.loads(self.read_result["content"])
        if numbers != data["numbers"] or any(type(n) is not int for n in numbers):
            return {"ok": False, "error": "汇总参数必须与实际读取的 numbers 一致。"}
        self.sum_result = {"ok": True, "total": sum(numbers), "count": len(numbers),
                           "dataset": data["dataset"], "receipt": secrets.token_hex(6)}
        return copy.deepcopy(self.sum_result)

    def expected_summary(self):
        if self.sum_result is None:
            raise LoopStopped("子 Agent 尚未取得真实汇总证据。")
        return {"path": self.path, **{k: self.sum_result[k] for k in ("total", "count", "dataset", "receipt")}}

    def accept_child(self, text):
        try:
            summary = Summary.model_validate_json(text).model_dump()
        except (TypeError, ValueError):
            raise LoopStopped("子 Agent 的报告不是合格的汇总 JSON。") from None
        if summary != self.expected_summary():
            raise LoopStopped("子 Agent 报告与真实读取和计算证据不一致。")
        self.child_summary = summary
        print("子 Agent 已返回，宿主核对通过：", json.dumps(summary, ensure_ascii=False), flush=True)
        return json.dumps({"ok": True, "summary": summary}, ensure_ascii=False)

    async def extract_child(self, result):
        # 保存独立 RunResult，供检查子循环和实际工具结果是否交回。
        if len(result.raw_responses) != 3:
            raise LoopStopped("子 Agent 没有完整执行读取、汇总和返回三个轮次。")
        output = self.accept_child(result.final_output)
        self.child_run = result
        return output

    def judge_report(self, summary, target):
        data = summary.model_dump()
        if self.child_summary is None or data != self.child_summary or self.judgement is not None or type(target) is not int or target != self.example.target:
            return {"ok": False, "error": "主 Agent 必须使用已验收的子 Agent 结果和真实目标值，只比较一次。"}
        self.judgement = {**data, "target": target, "met": data["total"] >= target,
                          "margin": data["total"] - target, "parent_marker": self.example.parent_marker}
        return {"ok": True, "report": copy.deepcopy(self.judgement)}

    def snapshot(self):
        return copy.deepcopy({"path": self.path, "delegations": self.delegations,
            "read_result": self.read_result, "sum_result": self.sum_result,
            "child_summary": self.child_summary, "judgement": self.judgement})


def create_child_tools(access):
    @function_tool
    async def read_file(path: str) -> dict:
        """读取本次唯一人工 JSON；工具只对汇总子 Agent 开放。"""
        return access.read_file(path)

    @function_tool
    async def sum_numbers(numbers: list[StrictInt]) -> dict:
        """汇总真实读取的 numbers，并生成结果回执；不判断主任务是否达标。"""
        return access.sum_numbers(numbers)

    return read_file, sum_numbers


def create_parent_judge(access):
    @function_tool
    async def judge_report(summary: Summary, target: StrictInt) -> dict:
        """主 Agent 接回已验收汇总后，与主任务目标值比较并保留主任务标记。"""
        return access.judge_report(summary, target)
    return judge_report
