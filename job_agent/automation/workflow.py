"""搜索、自动分析、准备材料、模拟投递的编排；所有外部能力显式注入。"""

from pathlib import Path

from job_agent.analysis import analyze_jd, match_resume
from job_agent.city_policy import city_fit
from job_agent.runtime import JobRuntime
from job_agent.search import JobCandidate
from job_agent.search_cli import collect_search
from job_agent.source_health import SourceHealthStore
from .applications import ApplicationService
from .decisions import decide
from .engine import check_candidates
from .store import GuardError
from .reports import save_report


def candidate_available(runtime, resume, job):
    previous = next((old for old in runtime.workspace.memory.library.list()
                     if old["company"] == job.company and old["title"] == job.title
                     and old["city"] == city_fit(job)["city"]
                     and (not old["detail_url"] or old["detail_url"] == (job.detail_url or job.url))), None)
    if previous and previous["status"] not in ("discovered", "shortlisted"):
        return False
    jd = analyze_jd(job.analysis_text())
    report = {"jd_analysis": {"analysis": jd}, "resume_match": {"matching": match_resume(jd, resume)}}
    return decide(job, report, previous or {"status": "discovered"}, now=runtime.automation.clock(), resume=resume)["action"] != "skip"


async def search_once(runtime, args, resume):
    # 广州已投递/过期/硬资格不符不占城市扩展阈值；过滤不调用模型。
    return await collect_search(args, eligible=lambda job: candidate_available(runtime, resume, job))


async def workflow(runtime, search, resume, *, adapter=None, facts=None, city_min=2, guangzhou_only=False):
    SourceHealthStore(runtime.workspace.memory).record(search.get("source_checks", []))
    result = await check_candidates(runtime, search["candidates"], resume, city_min=city_min, guangzhou_only=guangzhou_only)
    summary = result["summary"]
    summary["applications"] = []
    if adapter is not None:
        service = ApplicationService(runtime.automation, runtime.limits, adapter)
        materials = JobRuntime(runtime.workspace.project, lambda *_: None, demo=True,
                               state_dir=runtime.workspace.root, session_id="automation-materials")
        try:
            for row in result["discovery"]["ranked"]:
                job_id = row["job_id"]
                candidate = JobCandidate(**row["candidate"])
                existing = runtime.automation.db.execute("SELECT id,state FROM auto_applications WHERE job_id=? AND mode='simulation'", (job_id,)).fetchone()
                if existing:
                    summary["applications"].append({"job_id": job_id, "state": "already_recorded", "attempt_id": existing[0]})
                    continue
                try:
                    runtime.automation.check_running()
                    prepared = await service.prepare(materials, job_id, candidate, row["report"], resume, facts or {})
                    if prepared["state"] == "ready":
                        attempt = await service.submit(prepared, candidate, row["report"], resume)
                        summary["applications"].append({**{key: attempt[key] for key in ("job_id", "state")},
                            "materials_file": Path(prepared["packet_path"]).relative_to(runtime.workspace.root).as_posix()})
                    else:
                        summary["applications"].append({"job_id": job_id, "state": prepared["state"],
                            **({"materials_file": Path(prepared["packet_path"]).relative_to(runtime.workspace.root).as_posix()} if prepared.get("packet_path") else {})})
                except (GuardError, PermissionError, ValueError) as error:
                    summary["applications"].append({"job_id": job_id, "state": "blocked", "error_kind": type(error).__name__})
        finally:
            await materials.close()
    summary.update(http_requests=search.get("http_requests", 0), source_issue_count=len(search.get("issues", [])))
    summary["source_input_kind"] = search.get("input_kind", "provided_candidates_not_live_verified")
    simulated = bool(runtime.llm and runtime.llm.simulation)
    summary["real_api_requests"] = runtime.llm.requests if runtime.llm and not simulated else 0
    summary["estimated_real_fee_rmb"] = runtime.llm.usage()["estimated_rmb"] if runtime.llm and not simulated else 0
    paths = save_report(runtime.workspace.root, summary)
    summary["report_files"] = {key: Path(value).relative_to(runtime.workspace.root).as_posix() for key, value in paths.items()}
    # 日志/摘要只有岗位信息和状态，没有简历/表单答案/原始异常。
    runtime.automation.save_summary(summary)
    return result
