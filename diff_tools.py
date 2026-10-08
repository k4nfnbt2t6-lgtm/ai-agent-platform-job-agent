"""Diff 课程：比较真实文本，在写入工具成功后附上差异。"""

import difflib
import os
import tempfile
from pathlib import Path

from file_tools import FileAccess


INITIAL_SOURCE = (
    "# Diff practice\n"
    "# obsolete note: remove this line\n"
    "\n"
    "def add(a, b):\n"
    "    return a - b\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    print(add(37, 58))\n"
)
# 仅用于本地演示与验收，不把完整答案交给真实模型。
EXPECTED_SOURCE = (
    "# Diff practice\n"
    "\n"
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    # Show the result.\n"
    "    print(add(37, 58))\n"
)


def prepare_diff_file(project_dir):
    root = Path(project_dir).resolve(strict=True)
    descriptor, name = tempfile.mkstemp(prefix="agent_diff_", suffix=".py", dir=root)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(INITIAL_SOURCE.encode("utf-8"))
    return Path(name)


def build_diff(before, after, path):
    """统一 Diff 使用 + / -；结构化结果另外标识新增、删除和替换块。"""
    old_lines = before.splitlines(keepends=True)
    new_lines = after.splitlines(keepends=True)
    chunks = difflib.unified_diff(old_lines, new_lines,
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3)
    # 没有末尾换行时补上标准提示，不把两条差异粘成一条。
    unified = "".join(line if line.endswith("\n")
        else line + "\n\\ No newline at end of file\n" for line in chunks)
    changes = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        changes.append({
            "kind": {"insert": "added", "delete": "deleted", "replace": "modified"}[tag],
            "old_start": i1 + 1, "new_start": j1 + 1,
            "old_lines": old_lines[i1:i2], "new_lines": new_lines[j1:j2],
        })
    summary = {
        "added_lines": sum(len(c["new_lines"]) for c in changes if c["kind"] == "added"),
        "deleted_lines": sum(len(c["old_lines"]) for c in changes if c["kind"] == "deleted"),
        "modified_blocks": sum(c["kind"] == "modified" for c in changes),
        "replaced_old_lines": sum(len(c["old_lines"]) for c in changes if c["kind"] == "modified"),
        "replacement_new_lines": sum(len(c["new_lines"]) for c in changes if c["kind"] == "modified"),
        "unified_added_lines": sum(len(c["new_lines"]) for c in changes),
        "unified_deleted_lines": sum(len(c["old_lines"]) for c in changes),
    }
    return {"unified_diff": unified, "changes": changes, "summary": summary}


class DiffFileAccess(FileAccess):
    def write_file(self, path, content, expected_content):
        try:
            self._checked_path(path)
            before = self.snapshot()
            if before != expected_content:
                raise ValueError("原文不一致。")
        except (OSError, ValueError):
            return {"ok": False, "error": "写入被拒绝：路径或实际原文不符合要求。"}
        result = super().write_file(path, content, expected_content)
        if not result.get("ok"):
            return result
        # content 来自父类写入后读回的实际文本；模型没有 diff 参数可以伪造证据。
        return {**result, "before": before,
                "diff": build_diff(before, result["content"], self.relative_path)}


def show_diff(report):
    print("实际文件 Diff（a 为修改前，b 为修改后）：", flush=True)
    print(report["unified_diff"] or "（内容没有变化）\n", end="", flush=True)
    s = report["summary"]
    print(f"单独新增：{s['added_lines']} 行；单独删除：{s['deleted_lines']} 行；"
          f"修改：{s['modified_blocks']} 个替换块"
          f"（旧 {s['replaced_old_lines']} 行 → 新 {s['replacement_new_lines']} 行）。", flush=True)
    print(f"统一 Diff 的 + / - 合计：+{s['unified_added_lines']} / -{s['unified_deleted_lines']}。"
          "替换块中的旧行也算 -，新行也算 +。", flush=True)
