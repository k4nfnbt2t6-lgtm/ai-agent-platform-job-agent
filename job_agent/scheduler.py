"""v5 每日检查：调度和后台执行复用课程管理器，分析固定为 local。"""

import asyncio
import fcntl
import os
import stat
from contextlib import contextmanager
from copy import copy
from datetime import datetime
from pathlib import Path

from background_tools import BackgroundCallbacks
from scheduled_tools import ScheduledCallbacks
from .city_policy import select_cities, selected_with_aliases
from .discovery import DiscoveryStore, analyze_candidates, candidate_filter, similar_job
from .resume_loader import load_resume
from .runtime import JobRuntime
from .search_cli import collect_search, read_json_file, validate_search_args
from .search import JobCandidate, load_sources
from .source_health import SourceHealthStore, print_source_checks
from .update_store import UpdateStore, make_summary, print_update, utc_clock


@contextmanager
def check_lock(directory):
    """同一岗位库跨进程也只检查一次；不等待另一进程，不产生重试。"""
    fd = os.open(Path(directory) / "job-update.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("检查锁必须是独立普通文件。")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def validate_update_args(args):
    if getattr(args, "mode", "local") != "local" or getattr(args, "model", None) or getattr(args, "llm_dry_run", False):
        raise ValueError("v5 自动/立即检查只允许 local，不支持 scheduled LLM 或付费模型参数。")
    if getattr(args, "daily_at", None) and not getattr(args, "watch", False):
        raise ValueError("--daily-at 需要 --watch；一次 --check-now 不改变每日计划。")
    if getattr(args, "search_only", False):
        raise ValueError("每日检查需要完成 local 分析；只搜索请继续使用 v3 --search-only。")
    cloned = copy(args)
    cloned.search = True
    validate_search_args(cloned)
    from .update_store import daily_time
    if getattr(args, "daily_at", None):
        daily_time(args.daily_at)
    # 启动时验证显式材料；不发送请求，也不把文件内容写入计划。
    resume_path = args.resume
    if not resume_path and getattr(args, "search_demo", False):
        from agent_platform.terminal_ui import PROJECT
        resume_path = PROJECT / "job_agent/examples/resume.example.md"
    if not resume_path:
        raise ValueError("每日检查需要 --resume 指定本地简历。")
    load_resume(resume_path)
    fixture = getattr(args, "search_fixture", None)
    if fixture:
        import json
        items = json.loads(read_json_file(fixture))
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError("离线岗位输入需要最多 100 个岗位的数组。")
        for item in items:
            JobCandidate(**item)
    elif not getattr(args, "search_demo", False):
        load_sources(read_json_file(args.sources))


class DailyUpdates:
    def __init__(self, runtime, args, *, clock=utc_clock, search=collect_search):
        if runtime.requested_mode != "local" or runtime.llm is not None:
            raise ValueError("每日检查只使用 local Runtime，禁止自动付费。")
        self.runtime, self.args, self.clock, self.search = runtime, args, clock, search
        self.store = UpdateStore(runtime.workspace.memory)
        self.background, self.timer = BackgroundCallbacks(), ScheduledCallbacks()
        self.closed, self.watching = False, False
        with check_lock(runtime.workspace.root) as acquired:
            if acquired:
                self.store.recover_interrupted(clock())

    @property
    def running(self):
        return any(job.status in ("pending", "running") for job in self.background.jobs.values())

    def configure(self, at):
        return self.store.configure(at, self.clock())

    def start_check(self, origin="manual"):
        if self.closed:
            raise ValueError("每日检查已关闭。")
        if self.running:
            return {"ok": False, "reason": "已有检查运行，未重复启动；每日计划不变。"}
        return self.background.start_callback(lambda: self._execute(origin))

    async def tick(self):
        if self.closed or self.running:
            return False
        if self.store.claim_due(self.clock()):
            self.start_check("scheduled")
            return True
        return False

    def start_watch(self):
        if self.closed or self.watching:
            raise ValueError("调度器已关闭或已启动。")
        if not self.store.schedule()["daily_at"]:
            raise ValueError("首次启动需用 --daily-at HH:MM 设置北京时间。")
        self.watching = True
        self._arm()

    def _arm(self):
        if self.closed or not self.watching:
            return
        if any(job.status == "failed" for job in self.timer.jobs.values()):
            self.watching = False
            self.runtime.emit("error", "每日调度器失败，已停止当前计时器；保存的计划保留，请检查后重新启动 --watch。")
            return
        due = datetime.fromisoformat(self.store.schedule()["next_run_at"])
        delay = min(60.0, max(0.0, (due - self.clock()).total_seconds()))
        # 手动检查跨过到期点时保留到期点，稍后再触发，不忙轮询。
        if self.running:
            delay = 60.0
        self.timer.schedule_callback(self.tick, delay, on_done=self._arm)

    async def wait_current(self):
        if not self.background.workers:
            return None
        identity = next(iter(self.background.workers))
        await asyncio.shield(self.background.workers[identity])
        return self.background.jobs[identity].snapshot()

    async def _execute(self, origin):
        with check_lock(self.runtime.workspace.root) as acquired:
            if not acquired:
                return {"skipped": True, "reason": "另一个进程正在检查同一岗位库，本次未搜索或分析。"}
            self.store.recover_interrupted(self.clock())
            run_id = self.store.begin(origin, self.clock())
            worker = None
            try:
                async with asyncio.timeout(600):
                    resume_path = self.args.resume
                    if not resume_path and getattr(self.args, "search_demo", False):
                        resume_path = self.runtime.workspace.project / "job_agent/examples/resume.example.md"
                    resume = load_resume(resume_path)[0]
                    search = await self.search(self.args)
                    SourceHealthStore(self.runtime.workspace.memory).record(search.get("source_checks", []))
                    worker = JobRuntime(self.runtime.workspace.project, self.runtime.sink, demo=True, mode="local",
                                        state_dir=self.runtime.workspace.root, session_id="daily-updates")
                    summary = await self._analyze_updates(worker, search, resume, run_id)
                    summary["source_checks"] = search.get("source_checks", [])
                    summary["source_health"] = SourceHealthStore(self.runtime.workspace.memory).list()
                    status = "partial" if summary["issues"] else "completed"
                    if summary["issues"] and not search["candidates"]:
                        status = "failed"
                    self.store.finish(run_id, self.clock(), summary, status)
                    print("\n岗位检查结束：" + status)
                    self.print_summary(summary)
                    print_source_checks(summary["source_checks"])
                    return summary
            except asyncio.CancelledError:
                summary = make_summary(self.store.rows(run_id), issues=[{"reason": "检查已中断，已发现的岗位和投递进度保留。"}])
                self.store.finish(run_id, self.clock(), summary, "interrupted")
                raise
            except Exception as error:
                summary = make_summary(self.store.rows(run_id), issues=[{"reason": "检查失败（" + type(error).__name__ + "），本次不重试；请检查本地材料和来源。"}])
                self.store.finish(run_id, self.clock(), summary, "failed")
                self.print_summary(summary)
                return summary
            finally:
                if worker:
                    await worker.close()

    def print_summary(self, summary):
        print_update(summary)

    async def _analyze_updates(self, worker, search, resume, run_id):
        memory = worker.workspace.memory
        library = memory.library
        before = {job["job_id"]: job for job in library.list()}
        store = DiscoveryStore(memory)
        selection = select_cities(search["candidates"], getattr(self.args, "city_min", 2),
            getattr(self.args, "guangzhou_only", False), getattr(self.args, "search_limit", 8),
            eligible=lambda job: candidate_filter(job)["eligible"], same_job=similar_job)
        candidates = selected_with_aliases(selection, search["candidates"], getattr(self.args, "search_limit", 8), similar_job)
        worthy, unchanged, seen = [], 0, set()
        for job in candidates:
            identity, canonical, _ = store.remember(job)
            job_id = library.linked("candidate", identity)
            if job_id in seen:
                continue
            seen.add(job_id)
            current = library.get(job_id)
            previous = before.get(job_id)
            kind = "new" if previous is None else "updated" if previous["jd_hash"] != current["jd_hash"] else "pending_analysis"
            if kind == "pending_analysis" and (previous["overall_match"] is not None or previous["ranking"] is not None):
                unchanged += 1
                continue
            self.store.add_item(run_id, job_id, kind, self.clock())
            worthy.append(canonical)
            # v3 禁止静默重试付费；这里仅释放固定 local 的未完成标记，每次检查最多试一次。
            context = store.context_key(worker, resume)
            with memory.connection:
                memory.connection.execute("DELETE FROM discovery_analyses WHERE candidate_id=? AND context_key=? AND state!='completed'",
                                          (identity, context))
        result = await analyze_candidates(worker, worthy, resume, getattr(self.args, "analyze_limit", 2),
            city_min=getattr(self.args, "city_min", 2), guangzhou_only=getattr(self.args, "guangzhou_only", False)) if worthy else {"new_analyses": 0, "pending": []}
        issues = list(search.get("issues", []))
        issues.extend({"reason": row["reason"]} for row in result["pending"]
                      if "数量上限" not in row["reason"])
        return make_summary(self.store.rows(run_id), analyzed=result["new_analyses"], unchanged=unchanged,
                            issues=issues, http_requests=search.get("http_requests", 0), city_policy=search.get("city_policy", selection["policy"]))

    async def close(self):
        self.closed, self.watching = True, False
        await self.timer.close()
        await self.background.close()
