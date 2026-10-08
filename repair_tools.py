"""修复练习工具：只读写并运行本次创建的三行加法程序。"""

import ast
import os
import sys
import tempfile
from pathlib import Path

from agents import function_tool

from file_tools import MAX_TEXT_BYTES, FileAccess
from terminal_tool import run_terminal
from tools import create_file_tools


BROKEN_CONTENT = "a = 37\nb = 58\nprint(a + c)\n"
PYTHON_BOOTSTRAP = "import sys; exec(compile(sys.stdin.read(), sys.argv[1], 'exec'))"
# 白名单允许三个变量之间的加法；不允许导入、循环、文件或网络操作。
ALLOWED_PROGRAMS = {
    ast.dump(ast.parse(f"a = 37\nb = 58\nprint({left} + {right})\n"))
    for left in ("a", "b", "c") for right in ("a", "b", "c")
}


def check_practice_code(content):
    """只检查语法结构，不执行代码，也不代替模型修复。"""
    if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("练习代码不能超过 2,048 字节。")
    try:
        tree = ast.parse(content)
    except SyntaxError as error:
        raise ValueError("新文本不是有效 Python 代码。") from error
    if ast.dump(tree) not in ALLOWED_PROGRAMS:
        raise ValueError("本课只允许保留 a=37、b=58，并在 print 中对 a/b/c 两个变量相加。")


def prepare_repair_file(project_dir):
    root = Path(project_dir).resolve(strict=True)
    descriptor, name = tempfile.mkstemp(prefix="agent_repair_", suffix=".py", dir=root)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(BROKEN_CONTENT.encode("utf-8"))
    return Path(name)


class RepairFileAccess(FileAccess):
    def write_file(self, path, content, expected_content):
        try:
            check_practice_code(content)
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        return super().write_file(path, content, expected_content)

    def run_python(self, command):
        if command != ["python", self.relative_path]:
            return {"returncode": None, "stdout": "", "stderr": "命令未执行：只允许运行本次 Python 练习文件。"}
        try:
            source = self.snapshot()
            check_practice_code(source)
        except (OSError, ValueError) as error:
            return {"returncode": None, "stdout": "", "stderr": f"命令未执行：练习文件检查失败（{type(error).__name__}）。"}
        # 执行刚刚检查过的实际文件文本，防止检查与启动之间文件被替换。
        # -I 使用隔离模式，-B 禁止生成缓存；env={} 不把 API Key 传给子进程。
        result = run_terminal(
            [sys.executable, "-I", "-B", "-c", PYTHON_BOOTSTRAP, self.relative_path],
            timeout=3,
            cwd=self.root,
            env={},
            input_text=source,
        )
        return {**result, "path": self.relative_path, "source": source}


def create_repair_tools(file_access):
    read_file, write_file = create_file_tools(file_access)

    @function_tool
    def terminal(command: list[str]) -> dict:
        """使用当前 Python 解释器运行本次练习文件，返回 returncode、stdout、stderr。

        Args:
            command: 只接受 ["python", 任务指定的练习文件名]，不支持额外参数或其他命令。
        """
        return file_access.run_python(command)

    return read_file, write_file, terminal
