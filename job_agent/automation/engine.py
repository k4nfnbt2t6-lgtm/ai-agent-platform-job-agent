"""搜索后自动分析的单次工作单元；原有launchd与local每日任务保持不变。"""

from dataclasses import asdict
from pathlib import Path

from job_agent.analysis import ANALYSIS_VERSION, analyze_jd, fingerprint, match_resume
from job_agent.discovery import DiscoveryStore, candidate_filter, rank_job, sort_ranked
from job_agent.city_policy import select_cities, city_fit
from job_agent.runtime import JobRuntime
from job_agent.schemas import SCHEMA_VERSION
from job_agent.scheduler import DailyUpdates, check_lock
from .analysis import MeteredAnalyzer
from .decisions import decide
from .policy import digest
from .store import AutomationStore, GuardError


class AutomationRuntime(JobRuntime):
    """新增可选运行时，业务仍由JobRuntime的同一Agent/Skill工具循环完成。"""
    def __init__(self, *args, limits, llm_config=None, consent=None, resume=None,
                 client_factory=None, clock=None, simulation=False, **kwargs):
        if any(key in kwargs for key in ("mode", "analysis_backend", "approve_llm")):
            raise ValueError("自动化模式由llm_config和显式授权共同决定。")
        project = Path(args[0] if args else kwargs["project"]).resolve()
        directory = Path(kwargs.get("state_dir") or ".agent-platform/job-automation-dev")
        directory = directory if directory.is_absolute() else project / directory
        if not directory.resolve().is_relative_to(project / ".agent-platform"):
            raise ValueError("自动化私有数据必须保存在项目.agent-platform内。")
        kwargs["state_dir"] = directory
        if kwargs.pop("demo", True) is not True:
            raise ValueError("自动化工具循环必须使用本地编排模型。")
        super().__init__(*args, mode="local", demo=True, **kwargs)
        self.automation = AutomationStore(self.workspace.memory, clock)
        self.limits = limits
        if llm_config is not None:
            if consent is None or resume is None:
                self.workspace.memory.close()
                raise PermissionError("自动 LLM 需要明确材料、预算和外发授权。")
            backend = MeteredAnalyzer(llm_config, self.automation, limits, consent, resume, client_factory=client_factory, simulation=simulation)
            # 初始化阶段检查，发送前仍再次检查有效期和绑定材料。
            try:
                backend.check_consent()
            except BaseException:
                self.workspace.memory.close()
                raise
            self.llm, self.approve_llm, self.requested_mode = backend, backend.approve, "llm"

    async def _analyze(self, jd, resume=None, selected="all", source_label="自动发现"):
        backend = self.llm
        if backend is None:
            self.automation.check_running()
            return await super()._analyze(jd, resume, selected, source_label)
        if selected != "all":
            raise GuardError("自动岗位分析必须完成三个核心Skill。")
        calls = 3 - bool(self.workspace.memory.get_llm(jd, backend.cache_model))
        key = digest({"jd": fingerprint(jd), "resume": fingerprint(resume), "model": backend.config.model,
                      "endpoint": backend.config.base_url, "schema": SCHEMA_VERSION, "rules": ANALYSIS_VERSION,
                      "policy": backend.config.policy("match-resume"), "limits": asdict(self.limits)})
        if backend.plan_id is None:
            backend.start_plan(key, jd, resume, calls)
        result = None
        try:
            result = await super()._analyze(jd, resume, selected, source_label)
            return result
        finally:
            backend.finish(bool(result and (result.get("semantic_complete") or result.get("mock_checks_complete"))))

    def decorate_analysis_report(self, data):
        data["automation_endpoint"] = self.llm.config.base_url if self.llm else None
        data["automation_simulated"] = bool(self.llm and self.llm.simulation)
        if data["automation_simulated"]:
            data["mock_checks_complete"] = data["semantic_complete"]
            data["semantic_complete"] = False
            if data["mode"] == "llm":
                data["mode"] = "mock_llm"
        return data


async def check_candidates(runtime, candidates, resume, *, city_min=2, guangzhou_only=False):
    directory = runtime.workspace.root / "automation-lock"
    if directory.is_symlink():
        raise GuardError("自动化锁目录不能经过链接。")
    directory.mkdir(mode=0o700, exist_ok=True)
    with check_lock(directory) as acquired:
        if not acquired:
            raise GuardError("同一状态目录已有自动化检查，跳过并发执行。")
        return await _check_candidates(runtime, candidates, resume, city_min=city_min, guangzhou_only=guangzhou_only)


async def _check_candidates(runtime, candidates, resume, *, city_min=2, guangzhou_only=False):
    """只接受搜索层已解析的岗位；搜与模型分离，先本地资格预检。"""
    runtime.automation.check_running()
    if runtime.llm:
        runtime.llm.consent.check(resume, runtime.llm.config, runtime.limits, runtime.automation.clock())
        runtime.llm.check_consent()
        # 每次检查使用新的有界客户端会话；每日累计额度仍由SQLite控制。
        # 旧失败岗位由持久化claim阻止重试，费用未知则全局暂停新的付费计划。
        old = runtime.llm
        runtime.llm = MeteredAnalyzer(old.config, runtime.automation, runtime.limits, old.consent, resume,
                                      client_factory=old.client_factory, simulation=old.simulation)
        runtime.approve_llm = runtime.llm.approve
    prefiltered, skipped = [], []
    store = DiscoveryStore(runtime.workspace.memory)
    identities = {}
    for job in candidates:
        identity, canonical, duplicate = store.remember(job)
        identities[identity] = (canonical, duplicate)
    for identity, (job, duplicate) in identities.items():
        local = analyze_jd(job.analysis_text())
        matching = match_resume(local, resume)
        preview = {"jd_analysis": {"analysis": local}, "resume_match": {"matching": matching}}
        library_job = runtime.workspace.memory.library.get(runtime.workspace.memory.library.linked("candidate", identity))
        decision = decide(job, preview, library_job, now=runtime.automation.clock(), resume=resume)
        if decision["action"] == "skip":
            skipped.append({"company": job.company, "title": job.title, "reasons": decision["reasons"]})
        else:
            prefiltered.append(job)
    selection = select_cities(prefiltered, city_min, guangzhou_only, limit=8)
    context = store.context_key(runtime, resume)
    if runtime.llm:
        context = digest([context, runtime.llm.cache_model, runtime.llm.config.policy("match-resume")])
    result = {"ranked": [], "pending": [], "rejected": [], "new_analyses": 0,
              "duplicates": sum(int(row[1]) for row in identities.values()), "failure_kind": None,
              "city_policy": selection["policy"]}
    for job in sorted(selection["selected"], key=lambda job: city_fit(job)["priority"]):
        identity = next(key for key, value in identities.items() if value[0] is job)
        cached = store.existing(identity, context)
        report = cached[1] if cached and cached[0] == "completed" else None
        reused = report is not None
        if cached and not reused:
            result["pending"].append({"candidate_id": identity, "reason": "此前执行中/失败，未自动重试。"})
            continue
        if report is None:
            # v2/v3已保存的成功分析也可复用；检查简历、模式、schema及规则版本。
            for previous in runtime.workspace.memory.list_reports(job.analysis_text()):
                if (previous.get("mode") == ("mock_llm" if runtime.llm and runtime.llm.simulation else "llm" if runtime.llm else "local_rules")
                    and previous.get("model") == (runtime.llm.config.model if runtime.llm else None)
                    and (not runtime.llm or previous.get("automation_endpoint", runtime.llm.config.base_url) == runtime.llm.config.base_url)
                    and previous.get("schema_version") == SCHEMA_VERSION and previous.get("skill_gaps")
                    and previous.get("resume_match", {}).get("matching", {}).get("resume_fingerprint") == fingerprint(resume)
                    and (runtime.llm or previous["jd_analysis"]["analysis"].get("analysis_version") == ANALYSIS_VERSION)):
                    report, reused = previous, True
                    break
        if report is None and result["new_analyses"] >= runtime.limits.jobs_per_run:
            result["pending"].append({"candidate_id": identity, "reason": "达到本轮岗位分析上限。"})
            continue
        try:
            if report is None and runtime.llm:
                backend = runtime.llm
                calls = 3 - bool(runtime.workspace.memory.get_llm(job.analysis_text(), backend.cache_model))
                # 先预留预算，再声明任务；额度不足不锁死明日尚未付费的岗位。
                key = digest([identity, context])
                backend.start_plan(key, job.analysis_text(), resume, calls)
            if not cached and not store.claim(identity, context):
                if runtime.llm:
                    runtime.llm.finish(False)
                result["pending"].append({"candidate_id": identity, "reason": "其他进程已有执行记录。"})
                continue
            if report is None:
                result["new_analyses"] += 1
                report = await runtime.analyze(job.analysis_text(), resume, "all", job.source)
            if report is None:
                store.finish(identity, context, None, "incomplete")
                result["pending"].append({"candidate_id": identity, "reason": "分析未完成，未自动重试。"})
                continue
            store.finish(identity, context, report)
            runtime.workspace.memory.save_report(job.analysis_text(), resume, "all", report)
            ranking = rank_job(job, report)
            job_id = runtime.workspace.memory.library.linked("candidate", identity)
            runtime.workspace.memory.library.record_result(job_id, fingerprint(job.analysis_text()), report, ranking)
            result["ranked"].append({"job_id": job_id, "candidate": job.to_dict(), "report": report,
                                     "ranking": ranking, "reused_analysis": reused})
        except (GuardError, PermissionError, ValueError) as error:
            if runtime.llm:
                runtime.llm.finish(False)
            result["failure_kind"] = type(error).__name__
            result["pending"].append({"candidate_id": identity, "reason": "未发起后续请求；请检查授权、额度或既有异常。"})
    sort_ranked(result["ranked"])
    rows = []
    for row in result["ranked"]:
        from job_agent.search import JobCandidate
        job = JobCandidate(**row["candidate"])
        decision = decide(job, row["report"], runtime.workspace.memory.library.get(row["job_id"]), now=runtime.automation.clock(), resume=resume)
        row["decision"] = decision
        rows.append({"job_id": row["job_id"], "company": job.company, "title": job.title,
                     "city": decision["city"]["city"], "url": job.url,
                     "overall_match": decision["overall_match"], "recommendation": row["ranking"]["category"],
                     "decision": decision["action"], "reasons": decision["reasons"],
                     "review_items": decision["review_items"], "reused": row["reused_analysis"]})
    summary = {"version": 1, "mode": "mock_llm" if runtime.llm and runtime.llm.simulation else runtime.requested_mode, "new_analyses": result["new_analyses"],
               "duplicates": result["duplicates"], "local_skipped": len(skipped), "jobs": rows,
               "pending": len(result["pending"]), "failure_kind": result.get("failure_kind"),
               "external_applications": 0, "budget": runtime.automation.status()["reserved_or_spent_rmb"]}
    runtime.automation.save_summary(summary)
    return {"summary": summary, "discovery": result, "local_skipped": skipped}


class AutomationUpdates(DailyUpdates):
    """复用v5的due检查、锁、后台回调和恢复；不注册/修改系统任务。

    v5父类保持local，只有显式传入的本次分析worker可以启用已授权LLM。
    使用独立state_dir及调度配置；现有launchd不会调用这个类。
    """
    def __init__(self, runtime, args, *, analysis_runtime, **kwargs):
        if runtime.workspace.root != analysis_runtime.workspace.root:
            raise ValueError("自动调度与分析必须共用同一专用状态目录。")
        self.analysis_runtime = analysis_runtime
        if "search" not in kwargs:
            async def selected_search(arguments):
                from job_agent.resume_loader import load_resume
                from .workflow import search_once
                resume = load_resume(arguments.resume)[0]
                return await search_once(analysis_runtime, arguments, resume)
            kwargs["search"] = selected_search
        super().__init__(runtime, args, **kwargs)

    async def _analyze_updates(self, worker, search, resume, run_id):
        before = {j["job_id"]: j for j in self.analysis_runtime.workspace.memory.library.list()}
        result = await check_candidates(self.analysis_runtime, search["candidates"], resume,
            city_min=self.args.city_min, guangzhou_only=getattr(self.args, "guangzhou_only", False))
        for job in self.analysis_runtime.workspace.memory.library.list():
            old = before.get(job["job_id"])
            kind = "new" if old is None else "updated" if old["jd_hash"] != job["jd_hash"] else None
            if kind:
                self.store.add_item(run_id, job["job_id"], kind, self.clock())
        from job_agent.update_store import make_summary
        issues = list(search.get("issues", []))
        if result["summary"]["failure_kind"] or result["summary"]["pending"]:
            issues.append({"reason": "部分分析等待授权、额度或异常核对；没有自动重试付费。"})
        summary = make_summary(self.store.rows(run_id), analyzed=result["summary"]["new_analyses"],
                               unchanged=sum(r["reused"] for r in result["summary"]["jobs"]),
                               issues=issues, http_requests=search.get("http_requests", 0),
                               city_policy=search.get("city_policy"))
        summary["mode"] = self.analysis_runtime.requested_mode
        summary["estimated_rmb"] = self.analysis_runtime.llm.usage()["estimated_rmb"] if self.analysis_runtime.llm else 0
        summary["automation"] = result["summary"]
        return summary

    def print_summary(self, summary):
        data = summary.get("automation")
        if data is None:
            print("自动分析中断或失败；请查看持久化请求记录，未自动重试。")
        else:
            print(f"自动分析模式 {data['mode']}；新增分析 {data['new_analyses']}；今日预留/已估算费用 {data['budget']:.6f} 元。")
