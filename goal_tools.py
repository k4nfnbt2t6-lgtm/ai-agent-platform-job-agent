"""Goals 课程：目标、模型计划、已完成证据与待办步骤单独维护。"""

import copy
import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agents import function_tool
from pydantic import BaseModel, StrictBool, StrictInt

from file_tools import FileAccess


class PlannedStep(BaseModel):
    action: Literal["read_file", "add", "verify_report"]
    target: str
    description: str


class Report(BaseModel):
    total: StrictInt
    target: StrictInt
    met: StrictBool
    batch: str
    sources: list[str]


@dataclass(frozen=True)
class Example:
    morning: int
    afternoon: int
    target: int
    batch: str

    @classmethod
    def random(cls):
        morning, afternoon = secrets.randbelow(400) + 100, secrets.randbelow(400) + 100
        offset = 40 if secrets.randbelow(2) else -40
        return cls(morning, afternoon, morning + afternoon + offset, "goal-" + secrets.token_hex(6))


def prepare_goal_files(project_dir, example):
    root = Path(project_dir).resolve(strict=True)
    files = []
    for period, data in (
        ("morning", {"period": "morning", "count": example.morning, "batch": example.batch}),
        ("afternoon", {"period": "afternoon", "count": example.afternoon,
                       "target": example.target, "batch": example.batch}),
    ):
        descriptor, name = tempfile.mkstemp(prefix=f"agent_goal_{period}_", suffix=".json", dir=root)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.write("\n")
        files.append(Path(name))
    return tuple(files)


@dataclass
class Goal:
    objective: str
    criteria: list[str]
    goal_id: str = field(default_factory=lambda: "goal-" + secrets.token_hex(6))
    status: str = "active"
    steps: list = field(default_factory=list)
    verified_report: dict | None = None
    stop_reason: str | None = None
    history: list = field(default_factory=list)

    def __post_init__(self):
        self.record()

    def snapshot(self):
        pending = [step for step in self.steps if step["status"] != "completed"]
        next_step = pending[0]["description"] if pending else (
            "制定计划" if not self.steps else "输出最终报告并接受验收" if self.status == "ready_for_report" else None)
        return copy.deepcopy({
            "goal_id": self.goal_id, "objective": self.objective, "criteria": self.criteria,
            "status": self.status, "steps": self.steps,
            "completed_steps": len(self.steps) - len(pending), "pending_steps": len(pending),
            "next_step": next_step, "verified_report": self.verified_report, "stop_reason": self.stop_reason,
        })

    def record(self):
        self.history.append(self.snapshot())

    def plan(self, steps, allowed_files):
        if self.steps or self.status != "active":
            raise ValueError("已有计划不能重新初始化或抹掉进度。")
        raw = [step.model_dump() for step in steps]
        if len(raw) != 4 or [s["action"] for s in raw] != ["read_file", "read_file", "add", "verify_report"]:
            raise ValueError("本课计划应覆盖读取两份来源、计算和核对报告四个子任务。")
        if {s["target"] for s in raw[:2]} != set(allowed_files) or any(s["target"] for s in raw[2:]):
            raise ValueError("读取步骤应指定两份不同来源；计算和验收步骤 target 应为空字符串。")
        if any(not s["description"].strip() or len(s["description"]) > 120 for s in raw):
            raise ValueError("每个子任务须有简短、非空描述。")
        self.steps = [{**s, "status": "pending", "evidence": None} for s in raw]
        self.record()

    def require_next(self, action, target=""):
        pending = [s for s in self.steps if s["status"] == "pending"]
        if self.status != "active" or not pending or (pending[0]["action"], pending[0]["target"]) != (action, target):
            raise ValueError("请求不符合当前 Goal 的下一步，不能跳过待完成步骤。")
        return pending[0]

    def mark_done(self, action, evidence, target=""):
        step = self.require_next(action, target)
        step["status"] = "completed"
        step["evidence"] = copy.deepcopy(evidence)
        self.record()

    def complete(self, final_report):
        if self.status != "ready_for_report" or final_report != self.verified_report:
            raise ValueError("最终报告未通过已验证证据的验收，Goal 不能标记完成。")
        self.status = "complete"
        self.record()

    def stop(self, reason):
        if self.status != "complete":
            self.status = "stopped"
            self.stop_reason = reason
            self.record()


class GoalAccess:
    def __init__(self, project_dir, paths):
        self.files = {path.name: FileAccess(project_dir, path) for path in paths}
        self.initial_texts = {name: access.snapshot() for name, access in self.files.items()}
        self.data = {}
        self.total = None
        objective = (
            "核对两份人工报表：" + "、".join(self.files) + "。汇总 count，"
            "与下午报表的 target 比较，报告 total、target、是否达标 met、共同 batch 和两个 sources。"
        )
        self.goal = Goal(objective, ["实际读取两份指定来源，批次一致", "使用加法工具取得汇总证据",
                                     "按真实目标值判断达标情况", "最终报告与工具验收证据一致"])

    def set_plan(self, steps):
        try:
            self.goal.plan(steps, self.files)
            return {"ok": True, "goal": self.goal.snapshot()}
        except ValueError as error:
            return {"ok": False, "error": str(error), "goal": self.goal.snapshot()}

    def read_file(self, path):
        try:
            if path not in self.files:
                raise ValueError("只允许读取本次两份项目内人工报表。")
            self.goal.require_next("read_file", path)
            result = self.files[path].read_file(path)
            if not result.get("ok") or result["content"] != self.initial_texts[path]:
                raise ValueError("来源无法读取或已变化，不能据此标记步骤完成。")
            data = json.loads(result["content"])
            if not isinstance(data, dict):
                raise ValueError("练习数据必须是 JSON 对象。")
            expected_keys = {"period", "count", "batch"} | ({"target"} if data.get("period") == "afternoon" else set())
            if set(data) != expected_keys or data["period"] not in ("morning", "afternoon") or type(data["count"]) is not int:
                raise ValueError("练习数据格式不正确。")
            if not isinstance(data["batch"], str) or ("target" in data and type(data["target"]) is not int):
                raise ValueError("批次或目标值格式不正确。")
            if any(old["period"] == data["period"] or old["batch"] != data["batch"] for old in self.data.values()):
                raise ValueError("两份报表的时段或批次不一致。")
            self.data[path] = data
            self.goal.mark_done("read_file", {"path": path, "data": data}, path)
            return {**result, "goal": self.goal.snapshot()}
        except (OSError, ValueError, TypeError) as error:
            return {"ok": False, "error": str(error), "goal": self.goal.snapshot()}

    def add(self, a, b):
        try:
            self.goal.require_next("add")
            counts = [data["count"] for data in self.data.values()]
            if len(counts) != 2 or type(a) is not int or type(b) is not int or sorted((a, b)) != sorted(counts):
                raise ValueError("计算参数必须来自两份实际读取的报表。")
            self.total = a + b
            self.goal.mark_done("add", {"a": a, "b": b, "result": self.total})
            return {"ok": True, "result": self.total, "goal": self.goal.snapshot()}
        except ValueError as error:
            return {"ok": False, "error": str(error), "goal": self.goal.snapshot()}

    def verify_report(self, report):
        try:
            self.goal.require_next("verify_report")
            expected = self.expected_report()
            raw = report.model_dump()
            if len(raw["sources"]) != 2 or set(raw["sources"]) != set(self.files) or any(
                    raw[k] != expected[k] for k in ("total", "target", "met", "batch")):
                raise ValueError("报告与实际读取、计算证据不一致。")
            self.goal.mark_done("verify_report", {"report": raw})
            self.goal.verified_report = raw
            self.goal.status = "ready_for_report"
            self.goal.record()
            return {"ok": True, "report": raw, "goal": self.goal.snapshot()}
        except ValueError as error:
            return {"ok": False, "error": str(error), "goal": self.goal.snapshot()}

    def expected_report(self):
        if len(self.data) != 2 or self.total is None:
            raise ValueError("缺少真实来源或计算证据。")
        afternoon = next(data for data in self.data.values() if data["period"] == "afternoon")
        return {"total": self.total, "target": afternoon["target"], "met": self.total >= afternoon["target"],
                "batch": afternoon["batch"], "sources": list(self.files)}

    def save_record(self, project_dir, usage):
        """宿主仅新建本节记录文件，不向模型开放任意写入接口；记录不用于自动续跑。"""
        descriptor, name = tempfile.mkstemp(prefix="agent_goal_state_", suffix=".json", dir=project_dir)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"goal": self.goal.snapshot(), "history": self.goal.history, "usage": usage},
                      stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return Path(name)


def create_goal_tools(access):
    @function_tool
    def set_plan(steps: list[PlannedStep]) -> dict:
        """为当前 Goal 制定四个子任务：读取两份指定来源、add、verify_report。

        Args:
            steps: 描述由模型编写；读取步骤 target 为文件名，另两步 target 为空字符串。
        """
        return access.set_plan(steps)

    @function_tool
    def read_file(path: str) -> dict:
        """按计划读取本次人工报表；成功后记录实际证据并更新 Goal 进度。"""
        return access.read_file(path)

    @function_tool
    def add(a: StrictInt, b: StrictInt) -> dict:
        """计算两份实际报表数量之和，保存计算证据并更新 Goal 进度。"""
        return access.add(a, b)

    @function_tool
    def verify_report(report: Report) -> dict:
        """用真实来源和计算结果验收报告；通过后仍须输出最终报告，宿主才能完成 Goal。"""
        return access.verify_report(report)

    return set_plan, read_file, add, verify_report
