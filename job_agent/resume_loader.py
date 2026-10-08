"""只读取用户直接指定的本地简历；不联网、不运行文件内指令。"""

import io
import logging
import os
import stat
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from agent_platform.workspace import contains_credentials

from .analysis import validate_text
from .permissions import MAX_RESUME_FILE_BYTES, RESUME_SUFFIXES


class ResumeLoadError(ValueError):
    """可直接在终端显示的简历加载错误，不含材料内容。"""


def _pdf_text(data):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ResumeLoadError("PDF 解析依赖 pypdf 未安装；请使用项目虚拟环境并安装 requirements.txt。") from None
    # pypdf 的修复警告可能包含原文片段，解析期间不向终端输出这些诊断。
    logger = logging.getLogger("pypdf")
    previous_level = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ResumeLoadError("PDF 已加密；请先在本地导出未加密版本，或使用手动粘贴。")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if not text.strip():
            raise ResumeLoadError("PDF 未提取到文字，可能是扫描图片版；请提供文字版 PDF、DOCX、TXT、MD，或手动粘贴。")
        return text
    finally:
        logger.setLevel(previous_level)


def _docx_text(data):
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_RESUME_FILE_BYTES:
            raise ResumeLoadError("DOCX 解压后的正文过大，超过 10 MB 上限。")
        xml = archive.read(info)
    # 简历只需要正文文字；不展开自定义实体、读取外部资源或执行宏。
    declarations = xml.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declarations or b"<!ENTITY" in declarations:
        raise ResumeLoadError("DOCX 正文包含不支持的 XML 声明，无法安全解析。")
    root = ElementTree.fromstring(xml)
    if root.tag != namespace + "document" or root.find(namespace + "body") is None:
        raise ResumeLoadError("DOCX 正文结构不正确，解析失败；请重新导出。")
    paragraphs = []
    for paragraph in root.iter(namespace + "p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == namespace + "t":
                parts.append(node.text or "")
            elif node.tag == namespace + "tab":
                parts.append("\t")
            elif node.tag in (namespace + "br", namespace + "cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def load_resume(filename):
    """返回（经过校验的正文，文件名），正文仅交给当前会话复用。"""
    if not filename or not str(filename).strip():
        raise ResumeLoadError("请指定本地简历文件路径。")
    path = Path(filename).expanduser().absolute()
    if path.suffix.lower() not in RESUME_SUFFIXES or path.name.startswith("."):
        raise ResumeLoadError("简历格式不支持；仅支持 .pdf、.docx、.txt、.md，不读取隐藏文件或 .env。")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ResumeLoadError("简历文件不能经过符号链接，请直接指定原文件。")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ResumeLoadError("简历必须是独立普通文件。")
            data = stream.read(MAX_RESUME_FILE_BYTES + 1)
    except FileNotFoundError:
        raise ResumeLoadError(f"简历文件不存在：{path}") from None
    except OSError:
        raise ResumeLoadError(f"无法读取简历文件，请检查文件类型和读取权限：{path}") from None
    if not data:
        raise ResumeLoadError("简历文件为空。")
    if len(data) > MAX_RESUME_FILE_BYTES:
        raise ResumeLoadError("简历文件超过 10 MB 上限，请缩小文件或导出文字版。")
    try:
        if path.suffix.lower() == ".pdf":
            text = _pdf_text(data)
        elif path.suffix.lower() == ".docx":
            text = _docx_text(data)
        else:
            text = data.decode("utf-8-sig")
    except ResumeLoadError:
        raise
    except UnicodeDecodeError:
        raise ResumeLoadError("简历文本解析失败；TXT、MD 请保存为 UTF-8 编码。") from None
    except Exception:
        raise ResumeLoadError("简历解析失败；文件可能损坏或与扩展名不符，请重新导出或手动粘贴。") from None
    if not text.strip():
        raise ResumeLoadError("简历未提取到有效文字，文件正文为空；请换文件或手动粘贴。")
    try:
        text = validate_text(text, "简历提取文字")
    except ValueError as error:
        raise ResumeLoadError(str(error)) from None
    if contains_credentials(text):
        raise ResumeLoadError("简历疑似包含凭据，已拒绝导入。")
    return text, path.name
