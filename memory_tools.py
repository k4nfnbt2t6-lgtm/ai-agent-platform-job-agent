"""本节 Memory：只保存用户明确授权的一条人工学习偏好。"""

import json
import os
import tempfile
from dataclasses import dataclass

from agents import function_tool

from file_tools import FileAccess


MEMORY_KEY = "learning_preference"
SOURCE_SESSION = "memory_write_session"
EMPTY_MEMORY = {"version": 1, "memories": []}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def prepare_memory_file(project_dir):
    """每次练习创建新文件，保留之前的记忆和课程代码。"""
    descriptor, name = tempfile.mkstemp(prefix="agent_memory_", suffix=".json", dir=project_dir)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json_text(EMPTY_MEMORY) + "\n")
    return name


@dataclass(frozen=True)
class MemoryGrant:
    # 授权来自本节用户任务；模型不能通过自己的工具参数扩大授权。
    value: str


class MemoryStore:
    def __init__(self, project_dir, memory_file, grant=None):
        self.file = FileAccess(project_dir, memory_file)
        self.grant = grant

    def snapshot(self):
        """每次从磁盘读取；不从旧 Session 或对象缓存取记忆。"""
        document = json.loads(self.file.snapshot())
        if not isinstance(document, dict) or set(document) != {"version", "memories"}:
            raise ValueError("记忆文件结构不符合本课格式。")
        if type(document["version"]) is not int or document["version"] != 1:
            raise ValueError("记忆文件版本不符合本课格式。")
        records = document["memories"]
        if not isinstance(records, list) or len(records) > 1:
            raise ValueError("本节最多保存一条记忆。")
        for record in records:
            if not isinstance(record, dict) or set(record) != {
                "key", "value", "source_session", "source", "scope",
            }:
                raise ValueError("记忆记录结构不符合本课格式。")
            if (record["key"] != MEMORY_KEY or not isinstance(record["value"], str)
                    or not record["value"] or record["source_session"] != SOURCE_SESSION
                    or record["source"] != "explicit_user_request" or record["scope"] != "lesson"):
                raise ValueError("记忆内容或来源不符合本课格式。")
        return document

    def write(self, key, value):
        if self.grant is None or key != MEMORY_KEY or value != self.grant.value:
            return {"ok": False, "error": "只允许保存本轮用户明确授权的完整学习偏好。"}
        try:
            document = self.snapshot()
            if document["memories"]:
                raise ValueError("本次练习只允许写入一次，不覆盖已有记忆。")
            record = {"key": key, "value": value, "source_session": SOURCE_SESSION,
                      "source": "explicit_user_request", "scope": "lesson"}
            # 复用课程文件工具的路径、文件身份和先读后写检查。
            original = self.file.read_file(self.file.relative_path)
            if not original.get("ok") or json.loads(original["content"]) != document:
                raise ValueError("文件已变化或无法安全读取。")
            written = self.file.write_file(self.file.relative_path,
                json_text({"version": 1, "memories": [record]}) + "\n", original["content"])
            if not written.get("ok"):
                raise ValueError("记忆写入失败。")
            return {"ok": True, "memory": record}
        except (OSError, ValueError):
            return {"ok": False, "error": "记忆写入被拒绝：文件状态或格式不符合要求。"}

    def read(self, key):
        if key != MEMORY_KEY:
            return {"ok": False, "error": "本课只开放 learning_preference 这个记忆键。"}
        try:
            record = next(iter(self.snapshot()["memories"]), None)
            return {"ok": True, "memory": record}
        except (OSError, ValueError):
            return {"ok": False, "error": "记忆读取被拒绝：文件状态或格式不符合要求。"}


def create_write_tool(store):
    @function_tool
    def write_memory(key: str, value: str) -> dict:
        """将用户明确要求跨 Session 保存的学习偏好写入本节本地记忆。

        Args:
            key: 本课只允许 learning_preference。
            value: 用户明确授权保存的完整偏好，不能包含临时口令或其他内容。
        """
        return store.write(key, value)
    return write_memory


def create_read_tool(store):
    @function_tool
    def read_memory(key: str) -> dict:
        """从本节本地 JSON 读取学习偏好，不读取任何 Session 对话历史。

        Args:
            key: 本课只允许 learning_preference。
        """
        return store.read(key)
    return read_memory
