"""v3 入口参数与搜索报告；沿用 v2 简历加载、确认和 Runtime。"""

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_platform.terminal_ui import PROJECT
from agent_platform.workspace import contains_credentials
from .discovery import analyze_candidates, print_priority_list
from .resume_loader import load_resume
from .runtime import JobRuntime
from .search import JobCandidate, PublicFetcher, load_sources, search_public
from .source_health import SourceHealthStore, print_source_checks


def is_search(args):
    return bool(getattr(args, "search", False) or getattr(args, "search_demo", False) or getattr(args, "search_fixture", None))


def validate_search_args(args):
    if not is_search(args):
        if any(getattr(args, key, False) for key in ("sources", "allow_network", "search_only", "guangzhou_only")) or getattr(args, "search_limit", 8) != 8 or getattr(args, "analyze_limit", 2) != 2 or getattr(args, "city_min", 2) != 2:
            raise ValueError("搜索选项需要 --search 或 --search-demo。")
        return
    if args.jd or args.list_jobs or args.skill != "all" or getattr(args, "llm_dry_run", False):
        raise ValueError("搜索入口自动执行三个 Skill；不能与 --jd、--list-jobs、--skill 或 --llm-dry-run 混用。")
    if not 1 <= getattr(args, "search_limit", 8) <= 20 or not 1 <= getattr(args, "analyze_limit", 2) <= 5:
        raise ValueError("候选上限 1-20，新分析上限 1-5。")
    if not 1 <= getattr(args, "city_min", 2) <= 20:
        raise ValueError("城市扩展阈值需要 1-20 个有效候选。")
    if getattr(args, "search_demo", False) and getattr(args, "search_fixture", None):
        raise ValueError("人工样例和指定 fixture 二选一。")
    offline = getattr(args, "search_demo", False) or getattr(args, "search_fixture", None)
    if offline and (getattr(args, "sources", None) or getattr(args, "allow_network", False)):
        raise ValueError("离线岗位输入不能混用真实网络或来源配置。")
    if not offline and (not getattr(args, "sources", None) or not getattr(args, "allow_network", False)):
        raise ValueError("真实搜索需要已核验 --sources 和明确的 --allow-network；当前未发起网络请求。")
    if not getattr(args, "search_only", False) and not args.resume and not getattr(args, "search_demo", False):
        raise ValueError("自动匹配需要 --resume；仅搜索可加 --search-only。")


def read_json_file(filename):
    # 复用 v2 的独立普通文件、格式、凭据与 64KB 校验，不从网页接受路径。
    path = Path(filename).expanduser().absolute()
    if path.suffix.lower() != ".json":
        raise ValueError("搜索配置与 fixture 需要 .json 文件。")
    # read_authorized_text 只开放 TXT/MD；JSON 使用相同的文件安全策略。
    import os
    import stat
    from .analysis import validate_text
    if path.name.startswith(".") or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("搜索 JSON 必须是用户指定的普通文件，不能经过链接。")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("搜索 JSON 必须是独立普通文件。")
        text = validate_text(stream.read(64_001).decode("utf-8"), "搜索 JSON")
    if contains_credentials(text):
        raise ValueError("搜索 JSON 疑似包含凭据。")
    return text


async def collect_search(args):
    """共同搜索入口；只读取公开源或离线输入，不调用分析或模型。"""
    demo = getattr(args, "search_demo", False)
    fixture = PROJECT / "job_agent/examples/search-jobs.example.json" if demo else getattr(args, "search_fixture", None)
    if fixture:
        items = json.loads(read_json_file(fixture))
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError("搜索 fixture 应为最多 100 个岗位的 JSON 数组。")
        from .city_policy import select_cities, selected_with_aliases
        from .discovery import candidate_filter, similar_job
        imported = [JobCandidate(**item) for item in items]
        selection = select_cities(imported, getattr(args, "city_min", 2), getattr(args, "guangzhou_only", False),
                                  getattr(args, "search_limit", 8), eligible=lambda job: candidate_filter(job)["eligible"], same_job=similar_job)
        candidates = selected_with_aliases(selection, imported, getattr(args, "search_limit", 8), similar_job)
        search = {"candidates": candidates, "issues": [], "http_requests": 0}
        search["city_policy"] = selection["policy"]
        search["excluded"] = [{"candidate": job.to_dict(), "reasons": candidate_filter(job)["reasons"] or ["广州-only 已关闭其他城市扩展。" if getattr(args, "guangzhou_only", False) else "前序城市已足够，不进入核心候选。"]}
                              for job in [*selection["excluded"], *selection["deferred"]]]
        print("人工岗位/离线输入：没有真实岗位搜索。")
    else:
        sources = load_sources(read_json_file(args.sources))
        print("只读公开来源：" + "；".join(source["name"] + " " + source["url"] for source in sources))
        search = await search_public(sources, PublicFetcher(sources, allow_network=args.allow_network), getattr(args, "search_limit", 8),
                                     getattr(args, "city_min", 2), getattr(args, "guangzhou_only", False))
    return search


async def run_search(args, resume, emit, approve_llm):
    demo = getattr(args, "search_demo", False)
    fixture = demo or getattr(args, "search_fixture", None)
    if demo and resume is None and not getattr(args, "search_only", False):
        if getattr(args, "mode", "local") == "llm":
            raise ValueError("付费 LLM 模式需明确 --resume；离线人工搜索演示默认仅 local。")
        resume = load_resume(PROJECT / "job_agent/examples/resume.example.md")[0]
    search = await collect_search(args)
    mode = "local" if getattr(args, "search_only", False) else getattr(args, "mode", "local")
    runtime = JobRuntime(PROJECT, emit, demo=True, state_dir=args.state_dir, session_id=args.session,
                         mode=mode, model=getattr(args, "model", None), fallback=getattr(args, "fallback", "local"), approve_llm=approve_llm)
    try:
        SourceHealthStore(runtime.workspace.memory).record(search.get("source_checks", []))
        result = await analyze_candidates(runtime, search["candidates"], resume, getattr(args, "analyze_limit", 2), getattr(args, "search_only", False),
                                          min(getattr(args, "city_min", 2), getattr(args, "search_limit", 8)), getattr(args, "guangzhou_only", False))
        result["rejected"].extend(search.get("excluded", []))
        result["city_policy"] = search["city_policy"]
        result.update(search_issues=search["issues"], http_requests=search["http_requests"],
                      source_checks=search.get("source_checks", []), source_health=SourceHealthStore(runtime.workspace.memory).list(),
                      input_kind="artificial_or_imported" if fixture else "public_network", analysis_usage=runtime.usage())
        print_priority_list(result)
        print_source_checks(result["source_checks"])
        path = runtime.workspace.root / ("priority-list-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".json")
        with path.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        path.chmod(0o600)
        print("本地完整报告：" + str(path))
        # 所有来源失败不能作为成功退出；保留报告与历史，供系统/验收判断。
        return 1 if not search["candidates"] and search["issues"] else 0
    finally:
        await runtime.close()
        print("Job Agent v3 已退出；" + json.dumps(runtime.usage(), ensure_ascii=False))
