"""本节的 Skill 目录、按需加载工具和只读程序检查工具。"""

import io
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agents import function_tool

from file_tools import FileAccess
from repair_tools import RepairFileAccess


PRACTICE_SOURCE = "a = 37\nb = 58\nprint(a + b)\n"


def read_metadata(stream):
    """只读 YAML 头部。本课仅支持 name/description 两个单行字段。"""
    if stream.readline().strip() != "---":
        raise ValueError("Skill 缺少元信息头部。")
    metadata = {}
    for _ in range(8):
        line = stream.readline()
        if line.strip() == "---":
            if set(metadata) != {"name", "description"}:
                break
            name, description = metadata["name"], metadata["description"]
            if len(name) > 64 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or not description:
                break
            return metadata
        key, separator, value = line.partition(":")
        if not separator or key.strip() in metadata or len(line.encode("utf-8")) > 1_200:
            break
        metadata[key.strip()] = value.strip()
    raise ValueError("Skill 元信息必须是有效 name 和单行 description。")


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    access: FileAccess


class SkillCatalog:
    def __init__(self, project_dir, directory_name="lab_skills"):
        project = Path(project_dir).resolve(strict=True)
        requested = Path(directory_name)
        if requested.is_absolute() or ".." in requested.parts:
            raise ValueError("Skill 目录必须是项目内相对目录。")
        directory = project / requested
        if any(p.is_symlink() for p in (directory, *directory.parents)) or not directory.resolve().is_relative_to(project):
            raise ValueError("Skill 目录不能经过链接或越出项目。")
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("本课 Skill 目录必须位于项目内且不是符号链接。")
        self.specs = {}
        for path in sorted(directory.glob("*/SKILL.md")):
            if path.parent.is_symlink() or path.is_symlink():
                raise ValueError("本课不加载链接到外部的 Skill。")
            access = FileAccess(path.parent, path)
            # 发现阶段读取到头部结束即停止，不把正文载入初始模型请求。
            with path.open(encoding="utf-8") as stream:
                metadata = read_metadata(stream)
            name = metadata["name"]
            if path.parent.name != name or name in self.specs:
                raise ValueError("Skill 名称必须与目录一致且不能重复。")
            self.specs[name] = SkillSpec(name, metadata["description"], access)
        if not self.specs:
            raise ValueError("没有发现可用的课程 Skill。")

    def metadata(self):
        return [{"name": spec.name, "description": spec.description} for spec in self.specs.values()]

    def load(self, name):
        if name not in self.specs:
            return {"ok": False, "error": "只能加载本课目录中登记的 Skill 名称。"}
        try:
            spec = self.specs[name]
            instructions = spec.access.snapshot()
            metadata = read_metadata(io.StringIO(instructions))
            if metadata != {"name": spec.name, "description": spec.description}:
                raise ValueError("Skill 元信息自发现后已变化。")
            return {"ok": True, "name": name, "instructions": instructions}
        except (OSError, ValueError):
            return {"ok": False, "error": "Skill 文件无法安全读取或元信息已变化。"}


def prepare_skill_file(project_dir):
    descriptor, name = tempfile.mkstemp(prefix="agent_skill_", suffix=".py", dir=project_dir)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(PRACTICE_SOURCE)
    return Path(name)


def create_skill_tools(catalog, file_access):
    @function_tool
    def load_skill(name: str) -> dict:
        """按名称读取 Skill 的完整工作说明；根据目录中的用途判断是否需要加载。

        Args:
            name: 可用 Skill 目录中的名称，不能使用文件路径。
        """
        return catalog.load(name)

    @function_tool
    def read_file(path: str) -> dict:
        """读取本次获准的项目内 Python 练习文件，不读取其他文件。

        Args:
            path: 任务指定的相对文件名。
        """
        return file_access.read_file(path)

    @function_tool
    def terminal(command: list[str]) -> dict:
        """实际运行本次获准的 Python 练习程序，返回 returncode、stdout、stderr。

        Args:
            command: 仅接受 ["python", 任务指定的相对文件名]。
        """
        return file_access.run_python(command)

    return load_skill, read_file, terminal


def practice_access(project_dir, practice_file):
    # 复用已有的 AST 白名单及隔离执行；本节不向模型开放写入工具。
    return RepairFileAccess(project_dir, practice_file)
