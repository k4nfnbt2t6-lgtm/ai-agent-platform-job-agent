"""岗位链接 + JD 本机导入备选入口；不会声称导入内容来自实时抓取。"""

import argparse
import json
import os
from pathlib import Path

from agent_platform.terminal_ui import PROJECT
from .cli import read_authorized_text
from .search import JobCandidate, recruitment_type, safe_url, utc_now


def private_json(value, output, project=PROJECT):
    path = Path(output).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.resolve().is_relative_to(project.resolve() / ".agent-platform"):
        raise ValueError("输出只允许写入本项目.agent-platform，且不能经过链接。")
    if path.suffix != ".json":
        raise ValueError("输出需要.json文件。")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    return path


def import_jd(*, jd_file, url, company, title, city, output, kind="full", declared_type="", project=PROJECT):
    jd, _ = read_authorized_text(jd_file)
    url = safe_url(url)
    job = JobCandidate(company, title, city, recruitment_type(title, jd, declared_type),
        jd, "人工核验导入（非实时搜索）", url, utc_now(), jd_kind=kind,
        source_url=url, detail_url=url, parsing_evidence={"method": "manual_jd_import", "live_fetch": False,
            "qualification": "沿用JD证据；未注明届别/实习身份/期限时仍须确认"})
    private_json([job.to_dict()], output, project)
    return job


def main():
    parser = argparse.ArgumentParser(description="将手工取得的公开JD及原始链接导入现有搜索流程，不联网。")
    parser.add_argument("--jd", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--city", required=True)
    parser.add_argument("--type", default="", choices=("", "实习", "校招", "社招", "未知"))
    parser.add_argument("--kind", default="full", choices=("full", "summary"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        import_jd(jd_file=args.jd, url=args.url, company=args.company, title=args.title, city=args.city,
                  output=args.output, kind=args.kind, declared_type=args.type)
        print("已在本机保存带来源的岗位输入。不是实时搜索；未调用API或发送申请。")
        return 0
    except (ValueError, OSError):
        print("导入未完成：请检查JD文本、公开链接及独立输出文件；不会覆盖已有文件。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
