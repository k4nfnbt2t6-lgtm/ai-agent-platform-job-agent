"""本地样例工作区与工具适配；不向 Agent 开放真实项目源码。"""

import asyncio
import json
import os
import re
import secrets
from dataclasses import asdict
from pathlib import Path

from agents import function_tool
from pydantic import StrictInt

from background_tools import BackgroundTasks
from autotest_tools import BROKEN_CODE, test_content
from diff_tools import build_diff
from file_tools import FileAccess, INITIAL_CONTENT, MAX_TEXT_BYTES
from goal_tools import Example as GoalExample, GoalAccess, PlannedStep, Report, prepare_goal_files
from memory_tools import EMPTY_MEMORY, MEMORY_KEY, MemoryGrant, MemoryStore, json_text
from permission_tools import Decision, PendingWrite, PermissionAccess
from repair_tools import RepairFileAccess, check_practice_code
from scheduled_tools import ScheduledTasks
from skill_tools import PRACTICE_SOURCE, SkillCatalog
from subagent_tools import Example, SubagentAccess
from terminal_tool import run_terminal


def contains_credentials(text):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    return bool(key and key in text or re.search(
        r"sk-[A-Za-z0-9_-]{12,}|(?:api[_ -]?key|password|密码|令牌)\s*[:：=]|-----BEGIN .*PRIVATE KEY-----", text, re.I))


def safe_result(result):
    if contains_credentials(json.dumps(result, ensure_ascii=False)):
        return {"ok": False, "error": "样例或工具结果疑似包含凭据，已阻止交给模型。"}
    return result


class Workspace:
    def __init__(self, project, state_dir=None):
        self.project = Path(project).resolve(strict=True)
        directory = Path(state_dir) if state_dir else self.project / ".agent-platform"
        if not directory.is_absolute():
            directory = self.project / directory
        # 路径链中有符号链接就拒绝，避免把状态写到工作区外。
        if any(p.is_symlink() for p in (directory, *directory.parents)):
            raise ValueError("状态目录不能经过符号链接。")
        if not directory.resolve().is_relative_to(self.project):
            raise ValueError("状态目录必须位于 ai-agent-lab 内。")
        directory.mkdir(parents=True, exist_ok=True)
        self.root = directory.resolve(strict=True)
        samples = {
            "note.txt": INITIAL_CONTENT,
            "practice.py": PRACTICE_SOURCE,
            "memory.json": json_text(EMPTY_MEMORY) + "\n",
            "report.json": json.dumps({"numbers": [120, 80, 200], "dataset": "platform-demo"}) + "\n",
            "calculator.py": BROKEN_CODE,
            "calculator_tests.py": test_content("calculator"),
        }
        for name, content in samples.items():
            path = self.root / name
            if not path.exists() and not path.is_symlink():
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(content)
        self.files = {name: (RepairFileAccess if name == "practice.py" else FileAccess)(self.root, self.root / name)
                      for name in ("note.txt", "practice.py", "report.json")}
        self.memory = MemoryStore(self.root, self.root / "memory.json")
        self.permission = PermissionAccess(self.root, self.root / "calculator.py", self.root / "calculator_tests.py")
        self.memory.snapshot()
        self.skills = SkillCatalog(self.project)
        # SQLite 会创建旁文件；不接受已有链接/硬链接作为数据库。
        database = self.root / "sessions.sqlite"
        if database.exists() or database.is_symlink():
            info = database.lstat()
            if database.is_symlink() or not database.is_file() or info.st_nlink != 1:
                raise ValueError("会话数据库必须是独立普通文件。")
        self.pending = None
        self.audit = []
        self.managers = []
        self.goals = []

    def read(self, path):
        if path in ("calculator.py", "calculator_tests.py"):
            return safe_result(self.permission.read_file(path))
        if path not in self.files:
            return {"ok": False, "error": "只允许新工作区中登记的样例文件。"}
        return safe_result(self.files[path].read_file(path))

    def propose(self, path, content, expected_content):
        if contains_credentials(content) or contains_credentials(expected_content):
            return {"ok": False, "error": "样例不能写入疑似凭据。"}
        decision = Decision("ask", "high", "展示 Diff 后，由当前用户确认具体修改。")
        if path == "calculator.py" and self.pending is None:
            result = self.permission.write_file(path, content, expected_content)
            if result.get("request_id"):
                self.pending = self.permission.pending
                return {**result, "ok": True, "status": "awaiting_approval"}
            return result
        if path not in ("note.txt", "practice.py") or self.pending is not None:
            return {"ok": False, "error": "只可修改指定样例，且同时只能有一个待确认修改。"}
        access = self.files[path]
        if access.last_read_content != expected_content or access.snapshot() != expected_content:
            return {"ok": False, "error": "必须先读取，且原文不能在读取后发生变化。"}
        if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
            return {"ok": False, "error": "样例新内容不能超过 2048 字节。"}
        if path == "practice.py":
            check_practice_code(content)
        self.pending = PendingWrite(secrets.token_hex(8), path, content, expected_content)
        return {"ok": True, "status": "awaiting_approval", "executed": False,
                "permission": asdict(decision), "request_id": self.pending.request_id,
                "diff": build_diff(expected_content, content, path)}

    def resolve(self, request_id, approved):
        # 宿主确认入口；不会注册为模型工具。一次确认只能应用这一份内容。
        proposal = self.pending
        if proposal is None or request_id != proposal.request_id:
            return {"ok": False, "error": "申请不存在或编号不匹配。"}
        self.pending = None
        if approved is not True:
            result = self.permission.resolve_write(request_id, False) if proposal.path == "calculator.py" else {"ok": False, "status": "rejected", "executed": False}
        elif proposal.path == "calculator.py":
            result = self.permission.resolve_write(request_id, True)
            if result.get("ok"):
                # 同一具体修复的实际验收不需要再请求模型。
                result["tests"] = self.terminal(self.permission.backend.test_command)
        else:
            result = self.files[proposal.path].write_file(proposal.path, proposal.content, proposal.expected_content)
            result["executed"] = bool(result.get("ok"))
        self.audit.append({"request_id": request_id, "approved": approved is True,
                           "executed": result.get("executed", False)})
        return result

    def terminal(self, command):
        if command in (self.permission.backend.program_command, self.permission.backend.test_command):
            result = self.permission.terminal(command)
            # 真实测试失败是修复任务的证据；未执行的命令仍为错误。
            return safe_result({**result, "ok": result["returncode"] is not None, "test_passed": result["returncode"] == 0})
        if command in (["pwd"], ["ls"], ["ls", "."]):
            return run_terminal(command, cwd=self.project, timeout=3, env={})
        if command == ["python", "practice.py"]:
            return safe_result(self.files["practice.py"].run_python(command))
        return {"returncode": None, "stdout": "", "stderr": "命令被拒绝：只允许目录查看和登记样例的精确执行/测试命令。"}

    def tasks(self):
        return {"ok": True, "tasks": [job.snapshot() for manager in self.managers for job in manager.jobs.values()]}

    async def start_task(self, numbers, delay_seconds=None):
        if len(self.managers) >= 8:
            return {"ok": False, "error": "本进程最多创建 8 个任务。"}
        if delay_seconds is None:
            manager = BackgroundTasks(numbers, 0, 0, delay=0.3)
            result = manager.start(numbers)
        else:
            manager = ScheduledTasks(numbers, delay_seconds=delay_seconds)
            result = manager.schedule(numbers, delay_seconds)
        self.managers.append(manager)
        await asyncio.sleep(0)
        return result

    async def task_result(self, task_id):
        for manager in self.managers:
            if task_id in manager.jobs:
                return await manager.get_result(task_id, wait=True)
        return {"ok": False, "error": "未知任务编号；任务仅在当前进程存活期间有效。"}

    def check_goal(self, morning, afternoon, target):
        BackgroundTasks.validate_numbers([morning, afternoon, target])
        if len(self.goals) >= 8:
            return {"ok": False, "error": "本进程最多核对 8 个目标。"}
        paths = prepare_goal_files(self.root, GoalExample(morning, afternoon, target, "platform-" + secrets.token_hex(4)))
        access = GoalAccess(self.root, paths)
        self.goals.append(access.goal)
        steps = [PlannedStep(action="read_file", target=p.name, description="读取 " + p.name) for p in paths]
        steps += [PlannedStep(action="add", target="", description="汇总两份来源"),
                  PlannedStep(action="verify_report", target="", description="按证据验收报告")]
        actions = [lambda: access.set_plan(steps), lambda: access.read_file(paths[0].name),
                   lambda: access.read_file(paths[1].name), lambda: access.add(morning, afternoon),
                   lambda: access.verify_report(Report(**access.expected_report()))]
        for action in actions:
            result = action()
            if not result.get("ok"):
                access.goal.stop("核对失败")
                return result
        access.goal.complete(result["report"])
        return {"ok": True, "report": result["report"], "goal": access.goal.snapshot()}

    def child_access(self):
        source = self.files["report.json"].snapshot()
        if contains_credentials(source):
            raise ValueError("人工报表疑似包含凭据，未交给子 Agent。")
        data = json.loads(source)
        BackgroundTasks.validate_numbers(data["numbers"])
        return SubagentAccess(self.root, self.root / "report.json",
                              Example(data["numbers"], 450, data["dataset"], "platform-parent"))

    async def close(self):
        self.pending = None
        self.permission.pending = None
        for manager in self.managers:
            await manager.close()


def create_tools(workspace, task):
    # 写记忆的授权来自当前用户原话，不能来自模型、历史记录或工具内容。
    match = re.fullmatch(r"记住[：:]\s*(.{1,160})", task.strip())
    grant = MemoryGrant(match[1]) if match else None

    @function_tool(failure_error_function=None)
    def read_file(path: str) -> dict:
        """读取登记的 note.txt、practice.py、report.json、calculator.py 或 calculator_tests.py 样例；不能读取真实项目源码。"""
        return workspace.read(path)

    @function_tool(failure_error_function=None)
    def write_file(path: str, content: str, expected_content: str) -> dict:
        """先读取样例，再申请修改；只返回 Diff，须当前用户确认后宿主才写入。"""
        return workspace.propose(path, content, expected_content)

    @function_tool(failure_error_function=None)
    def terminal(command: list[str]) -> dict:
        """只允许 pwd、ls、ls .、python practice.py、python calculator.py 或 python -m unittest calculator_tests -v；样例经校验再隔离执行。"""
        return workspace.terminal(command)

    @function_tool(failure_error_function=None)
    def read_memory() -> dict:
        """从独立本地存储读取用户明确保存的一条学习偏好。"""
        return safe_result(workspace.memory.read(MEMORY_KEY))

    @function_tool(failure_error_function=None)
    def write_memory(value: str) -> dict:
        """仅保存当前用户以“记住：完整偏好”明确指定的文本，不覆盖已有记忆。"""
        if contains_credentials(value):
            return {"ok": False, "error": "记忆不能保存疑似凭据。"}
        store = MemoryStore(workspace.root, workspace.root / "memory.json", grant)
        return store.write(MEMORY_KEY, value)

    @function_tool(failure_error_function=None)
    def load_skill(name: str) -> dict:
        """按名称加载已有 lab_skills 的说明；Skill 不能扩大权限。"""
        return safe_result(workspace.skills.load(name))

    @function_tool(failure_error_function=None)
    async def start_background(numbers: list[StrictInt]) -> dict:
        """启动本地整数汇总并立即返回任务编号；接受 1–8 个绝对值不超过 10000 的整数。"""
        return await workspace.start_task(numbers)

    @function_tool(failure_error_function=None)
    async def schedule_task(numbers: list[StrictInt], delay_seconds: float) -> dict:
        """延迟大于 0、最多 5 秒汇总整数，进程退出后不会继续执行。"""
        return await workspace.start_task(numbers, delay_seconds)

    @function_tool(failure_error_function=None)
    def list_tasks() -> dict:
        """查看当前进程内后台与定时任务的实际状态。"""
        return workspace.tasks()

    @function_tool(failure_error_function=None)
    async def get_task_result(task_id: str) -> dict:
        """等待指定任务的结果，最多 6 秒；不反复请求模型。"""
        return await workspace.task_result(task_id)

    @function_tool(failure_error_function=None)
    def check_goal(morning: StrictInt, afternoon: StrictInt, target: StrictInt) -> dict:
        """以用户的三个人工整数创建两份样例报表，复用 Goal 的计划、读取、计算、证据验收。"""
        return workspace.check_goal(morning, afternoon, target)

    return [read_file, write_file, terminal, read_memory, write_memory, load_skill,
            start_background, schedule_task, list_tasks, get_task_result, check_goal]
