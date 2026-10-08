from pathlib import Path

from agents import function_tool

from terminal_tool import run_terminal


PROJECT_DIR = Path(__file__).resolve().parent


@function_tool
def add(a: int, b: int) -> int:
    """计算两个整数的和。"""
    return a + b


@function_tool
def terminal(command: list[str]) -> dict:
    """查看当前项目目录，仅支持 ["pwd"]、["ls"] 或 ["ls", "."]。

    Args:
        command: 命令及参数组成的列表，必须是上面列出的三种之一。
    """
    if command not in (["pwd"], ["ls"], ["ls", "."]):
        return {
            "returncode": None,
            "stdout": "",
            "stderr": '命令未执行：本课仅允许 ["pwd"]、["ls"] 或 ["ls", "."]。',
        }
    return run_terminal(command, timeout=5, cwd=PROJECT_DIR)


def create_file_tools(file_access):
    """每次运行只把新建的练习文件交给这两个工具。"""

    @function_tool
    def read_file(path: str) -> dict:
        """读取本次练习文本文件，返回 UTF-8 文本内容。

        Args:
            path: 任务中指定的项目相对文件名，仅允许本次练习文件。
        """
        return file_access.read_file(path)

    @function_tool
    def write_file(path: str, content: str, expected_content: str) -> dict:
        """写入本次练习文件的完整新内容，必须先使用 read_file。

        Args:
            path: 与 read_file 相同的项目相对文件名。
            content: 完整的新文件内容，保留任务要求不变的行及换行符。
            expected_content: read_file 刚才返回的完整原内容，原样传入。
        """
        return file_access.write_file(path, content, expected_content)

    return read_file, write_file
