"""本课文件工具：只允许读写本次创建的项目内练习文件。"""

import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


MAX_TEXT_BYTES = 2_048
INITIAL_CONTENT = (
    "title: AI Agent file practice\n"
    "status: pending\n"
    "note: keep this line unchanged\n"
)
EXPECTED_CONTENT = INITIAL_CONTENT.replace("status: pending\n", "status: completed\n", 1)


def prepare_practice_file(project_dir):
    """用唯一文件名创建新样例，保留之前的练习结果。"""
    root = Path(project_dir).resolve(strict=True)
    descriptor, name = tempfile.mkstemp(prefix="agent_practice_", suffix=".txt", dir=root)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(INITIAL_CONTENT.encode("utf-8"))
    return Path(name)


class FileAccess:
    def __init__(self, project_dir, allowed_file):
        self.root = Path(project_dir).resolve(strict=True)
        self.allowed_file = Path(allowed_file).absolute()
        if self.allowed_file.parent != self.root or self.allowed_file.is_symlink():
            raise ValueError("练习文件必须直接位于项目目录中，且不能是符号链接。")
        info = self.allowed_file.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("练习文件必须是独立的普通文件。")
        self.identity = (info.st_dev, info.st_ino)
        self.relative_path = self.allowed_file.name
        self.last_read_content = None

    def _checked_path(self, path):
        requested = Path(path)
        if not path or requested.is_absolute() or ".." in requested.parts:
            raise ValueError("只接受项目内的相对路径，不能使用绝对路径或 ..。")
        target = self.root / requested
        if target != self.allowed_file:
            raise ValueError("本课只允许操作本次创建的练习文件。")
        if target.is_symlink():
            raise ValueError("不能操作符号链接。")
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(self.root) or resolved != self.allowed_file:
            raise ValueError("文件必须位于当前项目目录内。")
        return target

    @contextmanager
    def _open(self, path, writable=False):
        target = self._checked_path(path)
        flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW
        descriptor = os.open(target, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != self.identity
            ):
                raise ValueError("练习文件已被替换或链接，操作已拒绝。")
            if info.st_size > MAX_TEXT_BYTES:
                raise ValueError("练习文件超过 2,048 字节上限。")
            stream = os.fdopen(descriptor, "r+b" if writable else "rb")
        except Exception:
            os.close(descriptor)
            raise
        with stream:
            yield stream

    def snapshot(self):
        """本地核对文件实际内容，不调用模型，也不产生工具调用记录。"""
        with self._open(self.relative_path) as stream:
            data = stream.read(MAX_TEXT_BYTES + 1)
        if len(data) > MAX_TEXT_BYTES:
            raise ValueError("练习文件超过 2,048 字节上限。")
        return data.decode("utf-8")

    def read_file(self, path):
        try:
            self._checked_path(path)
            content = self.snapshot()
            self.last_read_content = content
            return {"ok": True, "path": self.relative_path, "content": content}
        except (OSError, ValueError):
            return {"ok": False, "error": "读取被拒绝：路径、文件身份、大小或 UTF-8 编码不符合要求。"}

    def write_file(self, path, content, expected_content):
        try:
            self._checked_path(path)
            if self.last_read_content is None or expected_content != self.last_read_content:
                raise ValueError("必须先读取文件，并提交刚才读到的完整原内容。")
            data = content.encode("utf-8")
            if len(data) > MAX_TEXT_BYTES:
                raise ValueError("新内容超过 2,048 字节上限。")
            with self._open(path, writable=True) as stream:
                original = stream.read(MAX_TEXT_BYTES + 1).decode("utf-8")
                if original != expected_content:
                    raise ValueError("文件自读取后已变化，不能覆盖。")
                stream.seek(0)
                stream.write(data)
                stream.truncate()
                stream.flush()
                stream.seek(0)
                actual = stream.read(MAX_TEXT_BYTES + 1).decode("utf-8")
            self.last_read_content = None
            return {
                "ok": actual == content,
                "path": self.relative_path,
                "bytes_written": len(data),
                "content": actual,
            }
        except (OSError, ValueError):
            return {"ok": False, "error": "写入被拒绝：仅限本次练习文件，须先读后写、原内容一致且新内容不超过 2,048 字节。"}
