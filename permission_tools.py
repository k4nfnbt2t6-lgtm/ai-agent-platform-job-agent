"""本课权限网关：先判断 allow / ask / deny，再接触文件或启动命令。"""

import os
import secrets
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from agents import function_tool

from autotest_tools import AutoTestAccess, test_content
from diff_tools import build_diff
from terminal_tool import run_terminal


INITIAL_CODE = (
    "# permission status: pending\n"
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    print(add(37, 58))\n"
)
EXPECTED_CODE = INITIAL_CODE.replace("# permission status: pending", "# permission status: approved")


def prepare_permission_files(project_dir):
    root = Path(project_dir).resolve(strict=True)
    descriptor, name = tempfile.mkstemp(prefix="agent_permission_practice_", suffix=".py", dir=root)
    code = Path(name)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(INITIAL_CODE.encode("utf-8"))
    descriptor, name = tempfile.mkstemp(prefix="agent_permission_cases_", suffix=".py", dir=root)
    tests = Path(name)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(test_content(code.stem).encode("utf-8"))
    return code, tests


@dataclass(frozen=True)
class Decision:
    decision: str
    risk: str
    reason: str


class PermissionPolicy:
    def __init__(self, code_path, test_path, test_command):
        self.code_path = code_path
        self.test_path = test_path
        self.safe_commands = (("pwd",), tuple(test_command))

    def check(self, operation, path="", command=None):
        if operation == "terminal":
            if isinstance(command, list) and all(isinstance(x, str) for x in command) and tuple(command) in self.safe_commands:
                return Decision("allow", "low", "只允许 pwd 和本次固定测试命令。")
            return Decision("deny", "high", "命令不在精确白名单中，不能启动进程。")
        # 先做字符串路径检查；越界请求不会去探查项目外的文件是否存在。
        if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts:
            return Decision("deny", "high", "只接受项目内本次练习文件的相对文件名。")
        if path not in (self.code_path, self.test_path):
            return Decision("deny", "high", "本次未授权操作该文件。")
        if operation == "read":
            return Decision("allow", "low", "允许读取本次专用代码和固定测试。")
        if operation == "write" and path == self.code_path:
            return Decision("ask", "high", "写入会改变文件，须先展示 Diff，由用户确认本次具体修改。")
        return Decision("deny", "high", "本课不允许删除文件、修改固定测试或未知操作。")


@dataclass(frozen=True)
class PendingWrite:
    request_id: str
    path: str
    content: str
    expected_content: str


class PermissionAccess:
    def __init__(self, project_dir, code_file, test_file):
        self.backend = AutoTestAccess(project_dir, code_file, test_file)
        self.policy = PermissionPolicy(self.backend.relative_path, self.backend.tests.relative_path,
                                       self.backend.test_command)
        self.pending = None
        self.audit = []
        self.command_attempts = []
        self.approvals = []

    def _record(self, operation, decision, executed=False):
        entry = {"operation": operation, **asdict(decision), "executed": executed}
        self.audit.append(entry)
        return entry

    def read_file(self, path):
        decision = self.policy.check("read", path=path)
        entry = self._record("read", decision)
        if decision.decision != "allow":
            return {"ok": False, "executed": False, "permission": asdict(decision)}
        target = self.backend.tests if path == self.backend.tests.relative_path else self.backend
        result = target.read_file(path)
        entry["executed"] = bool(result.get("ok"))
        return {**result, "executed": entry["executed"], "permission": asdict(decision)}

    def terminal(self, command):
        decision = self.policy.check("terminal", command=command)
        entry = self._record("terminal", decision)
        if decision.decision != "allow":
            return {"ok": False, "executed": False, "permission": asdict(decision),
                    "returncode": None, "stdout": "", "stderr": "权限拒绝：命令未执行。"}
        self.command_attempts.append(list(command))
        if command == ["pwd"]:
            result = run_terminal(command, cwd=self.backend.root, env={}, timeout=3)
        else:
            # 复用固定源码、固定测试和文件身份检查；不执行任意 Python 或 unittest discover。
            result = self.backend.run(command)
        entry["executed"] = result.get("returncode") is not None
        return {**result, "ok": result.get("returncode") == 0,
                "executed": entry["executed"], "permission": asdict(decision)}

    def write_file(self, path, content, expected_content):
        decision = self.policy.check("write", path=path)
        entry = self._record("write", decision)
        if decision.decision == "deny":
            return {"ok": False, "executed": False, "permission": asdict(decision)}
        try:
            if self.pending is not None or self.backend.last_read_content != expected_content:
                raise ValueError("必须先读，且不能重复提交待批准写入。")
            before = self.backend.snapshot()
            if before != expected_content or len(content.encode("utf-8")) > 2048:
                raise ValueError("实际原文或内容大小不符合要求。")
        except (OSError, ValueError):
            denied = Decision("deny", "high", "写入申请无效：必须先读取，且实际原文一致。")
            entry.update(asdict(denied))
            return {"ok": False, "executed": False, "permission": asdict(denied)}
        proposal = PendingWrite(secrets.token_hex(8), path, content, expected_content)
        self.pending = proposal
        return {"ok": False, "executed": False, "permission": asdict(decision),
                "request_id": proposal.request_id, "path": path,
                "before": before, "proposed_content": content,
                "diff": build_diff(before, content, path)}

    def delete_file(self, path):
        decision = self.policy.check("delete", path=path)
        self._record("delete", decision)
        # 没有删除实现：即使模型说“已批准”，也没有可到达的删除执行入口。
        return {"ok": False, "executed": False, "permission": asdict(decision)}

    def resolve_write(self, request_id, approved):
        """仅供宿主的用户确认入口调用；不包装成 Agent Tool。批准绑定具体内容且只用一次。"""
        proposal = self.pending
        if proposal is None or request_id != proposal.request_id:
            return {"ok": False, "executed": False, "error": "申请不存在、已消费或编号不匹配。"}
        self.pending = None
        entry = {"request_id": request_id, "approved": approved is True, "executed": False}
        self.approvals.append(entry)
        if approved is not True:
            return {"ok": False, "executed": False, "status": "rejected"}
        # 使用保存的具体申请，不接受一份临时换掉的路径或内容；父类再次核对原文和文件身份。
        result = self.backend.write_file(proposal.path, proposal.content, proposal.expected_content)
        entry["executed"] = bool(result.get("ok"))
        if not result.get("ok"):
            return {**result, "executed": False, "status": "approved_but_not_written"}
        return {**result, "executed": True, "status": "approved_once",
                "diff": build_diff(proposal.expected_content, result["content"], proposal.path)}


def boundary_probes(access):
    """纯权限探针：越界、删除和改测试都会提前拒绝，不接触项目外文件。"""
    outside = access.read_file("../permission_fake_private.txt")
    deleted = access.delete_file(access.backend.relative_path)
    fixed_test = access.write_file(access.backend.tests.relative_path, "# simulated replacement\n", "")
    return {
        "越界读取在文件访问前被拒绝": outside["permission"]["decision"] == "deny" and not outside["executed"],
        "删除练习文件被拒绝且文件保留": deleted["permission"]["decision"] == "deny"
            and access.backend.snapshot() == INITIAL_CODE,
        "固定测试写入被拒绝且内容未变": fixed_test["permission"]["decision"] == "deny"
            and access.backend.test_snapshot() == access.backend.expected_tests,
    }


def create_permission_tools(access):
    @function_tool
    def read_file(path: str) -> dict:
        """读取本次专用代码或固定测试；越界和其他文件会拒绝。"""
        return access.read_file(path)

    @function_tool
    def terminal(command: list[str]) -> dict:
        """经过权限检查运行 pwd 或本次固定测试。其他命令只返回拒绝，绝不执行。"""
        return access.terminal(command)

    @function_tool
    def write_file(path: str, content: str, expected_content: str) -> dict:
        """申请修改本次代码文件，必须先读；返回待确认 Diff，不会立即写入。"""
        return access.write_file(path, content, expected_content)

    @function_tool
    def delete_file(path: str) -> dict:
        """本课删除请求总是被拒绝，仅供演示权限判断，不执行删除。"""
        return access.delete_file(path)

    return read_file, terminal, write_file, delete_file
