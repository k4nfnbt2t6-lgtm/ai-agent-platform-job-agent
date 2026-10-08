"""自动测试练习：代码可修改，测试固定，只运行本次两个文件。"""

import ast
import json
import os
import sys
import tempfile
from pathlib import Path

from agents import function_tool

from file_tools import MAX_TEXT_BYTES, FileAccess
from repair_tools import PYTHON_BOOTSTRAP
from terminal_tool import run_terminal
from tools import create_file_tools


BROKEN_CODE = (
    "def add(a, b):\n"
    "    return a - b\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    print(add(37, 58))\n"
)
ALLOWED_CODE = {
    ast.dump(ast.parse(BROKEN_CODE)),
    ast.dump(ast.parse(BROKEN_CODE.replace("return a - b", "return a + b"))),
}


def test_content(code_module):
    return (
        "import unittest\n"
        f"from {code_module} import add\n"
        "\n"
        "\n"
        "class AddTests(unittest.TestCase):\n"
        "    def test_add_two_numbers(self):\n"
        "        self.assertEqual(add(37, 58), 95)\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    unittest.main()\n"
    )


def prepare_autotest_files(project_dir):
    root = Path(project_dir).resolve(strict=True)
    descriptor, name = tempfile.mkstemp(prefix="agent_test_practice_", suffix=".py", dir=root)
    code_file = Path(name)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(BROKEN_CODE.encode("utf-8"))
    descriptor, name = tempfile.mkstemp(prefix="agent_test_cases_", suffix=".py", dir=root)
    test_file = Path(name)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(test_content(code_file.stem).encode("utf-8"))
    return code_file, test_file


def check_code(source):
    if len(source.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("练习代码不能超过 2,048 字节。")
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError("代码语法不符合本课练习。") from error
    if ast.dump(tree) not in ALLOWED_CODE:
        raise ValueError("本课只允许修改 add 的加减运算，不能增加其他代码或硬编码结果。")


# 固定的启动代码加载已检查的代码和不可修改的测试快照，真实执行 unittest。
# 不从项目中自动搜索或导入其他文件，不执行检查后可能被替换的文件文本。
TEST_BOOTSTRAP = """import json, sys, types, unittest
payload = json.load(sys.stdin)
for filename, source in ((payload['code_path'], payload['source']), (payload['test_path'], payload['test_source'])):
    name = filename.removesuffix('.py')
    module = types.ModuleType(name)
    module.__file__ = filename
    sys.modules[name] = module
    exec(compile(source, filename, 'exec'), module.__dict__)
unittest.main(module=payload['test_path'].removesuffix('.py'), argv=['unittest', '-v'], exit=True)
"""


class AutoTestAccess(FileAccess):
    def __init__(self, project_dir, code_file, test_file):
        super().__init__(project_dir, code_file)
        self.tests = FileAccess(project_dir, test_file)
        self.expected_tests = test_content(self.allowed_file.stem)

    def test_snapshot(self):
        actual = self.tests.snapshot()
        if actual != self.expected_tests:
            raise ValueError("测试内容已变化，不能用被修改的测试作为验收依据。")
        return actual

    def read_file(self, path):
        result = super().read_file(path)
        if not result.get("ok"):
            return result
        try:
            return {**result, "test_path": self.tests.relative_path, "test_content": self.test_snapshot()}
        except (OSError, ValueError):
            return {"ok": False, "error": "固定测试无法安全读取，已拒绝操作。"}

    def write_file(self, path, content, expected_content):
        try:
            check_code(content)
            self.test_snapshot()
        except (OSError, ValueError) as error:
            return {"ok": False, "error": f"修改被拒绝：{type(error).__name__}，仅允许修改练习代码，测试必须保持原样。"}
        return super().write_file(path, content, expected_content)

    @property
    def program_command(self):
        return ["python", self.relative_path]

    @property
    def test_command(self):
        return ["python", "-m", "unittest", self.tests.allowed_file.stem, "-v"]

    def run(self, command):
        if command not in (self.program_command, self.test_command):
            return {"returncode": None, "stdout": "", "stderr": "命令未执行：只允许运行本次练习程序或指定 unittest 测试。"}
        try:
            source = self.snapshot()
            check_code(source)
            tests = self.test_snapshot()
        except (OSError, ValueError):
            return {"returncode": None, "stdout": "", "stderr": "命令未执行：代码、测试内容或文件身份检查失败。"}
        if command == self.program_command:
            mode = "program"
            bootstrap, stdin = PYTHON_BOOTSTRAP, source
            arguments = [self.relative_path]
        else:
            mode = "tests"
            bootstrap = TEST_BOOTSTRAP
            stdin = json.dumps({"source": source, "code_path": self.relative_path,
                                "test_source": tests, "test_path": self.tests.relative_path})
            arguments = []
        result = run_terminal(
            [sys.executable, "-I", "-B", "-c", bootstrap, *arguments],
            timeout=3, cwd=self.root, env={}, input_text=stdin,
        )
        return {**result, "mode": mode, "code_path": self.relative_path,
                "test_path": self.tests.relative_path, "source": source}


def create_autotest_tools(access):
    read_file, write_file = create_file_tools(access)

    @function_tool
    def terminal(command: list[str]) -> dict:
        """运行本次练习程序或固定测试，返回真实 returncode、stdout、stderr。

        Args:
            command: ["python", 练习文件名] 或 ["python", "-m", "unittest", 测试文件名去掉.py, "-v"]。
        """
        return access.run(command)

    return read_file, write_file, terminal
