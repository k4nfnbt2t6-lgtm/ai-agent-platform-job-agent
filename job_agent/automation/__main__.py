"""独立入口：不接管已启用的launchd，不在导入时读取密钥或运行任务。"""

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_platform.terminal_ui import PROJECT
from job_agent.llm import LLMConfig
from job_agent.resume_loader import load_resume
from job_agent.analysis import fingerprint
from job_agent.search import JobCandidate
from .adapters import ManualHandoff, MockATS
from .applications import ApplicationService
from .engine import AutomationRuntime
from .workflow import search_once, workflow
from .mock import MockResponsesClient, RESUME, candidates
from .policy import LLMConsent, Limits


def parser():
    result = argparse.ArgumentParser(description="Job Agent可选自动化：默认离线；真实投递接口关闭。")
    result.add_argument("command", choices=("demo", "check", "status", "stop", "resume", "prepare", "reconcile"))
    result.add_argument("--state-dir", default=".agent-platform/job-automation-dev")
    result.add_argument("--mode", choices=("local", "mock-llm", "llm"), default="local")
    result.add_argument("--resume")
    result.add_argument("--sources", default=str(PROJECT / "job_agent/sources/public-careers.json"))
    result.add_argument("--search-fixture")
    result.add_argument("--allow-network", action="store_true")
    result.add_argument("--guangzhou-only", action="store_true")
    result.add_argument("--search-limit", type=int, default=5)
    result.add_argument("--city-min", type=int, default=2)
    result.add_argument("--analyze-limit", type=int, default=2)
    result.add_argument("--daily-budget", type=float, default=0)
    result.add_argument("--daily-requests", type=int, default=0)
    result.add_argument("--daily-applications", type=int, default=2)
    result.add_argument("--consent", help="本机人工确认的外发/付费授权JSON（不是API Key）")
    result.add_argument("--job", type=int)
    result.add_argument("--facts", help="本机人工确认的申请字段JSON")
    result.add_argument("--adapter", choices=("manual", "mock"), default="manual")
    result.add_argument("--application-mode", choices=("off", "manual", "mock", "browser-mock"), default="off",
                        help="仅本地人工材料或本地模拟，绝不提交真实申请")
    result.add_argument("--attempt")
    result.add_argument("--confirm-resume", action="store_true")
    return result


async def run(args):
    limits = Limits(args.daily_budget, args.daily_requests, args.daily_applications, args.analyze_limit)
    if not 1 <= args.search_limit <= 8:
        raise ValueError("候选岗位上限需要1-8。")
    resume = None
    if args.command == "demo":
        if args.mode == "llm" or args.allow_network or args.resume or args.consent:
            raise PermissionError("demo只使用内置人工材料，不允许真实网络或真实简历。")
        resume = RESUME
    elif args.command in ("check", "prepare"):
        if not args.resume:
            raise ValueError("本命令需要 --resume 本地文件。")
        resume = load_resume(args.resume)[0]
    config = consent = client = None
    if args.command in ("demo", "check") and args.mode == "mock-llm":
        limits = Limits(2, 12, args.daily_applications, args.analyze_limit)
        config = LLMConfig("offline-placeholder", "https://api.example.invalid/v1", "gpt-6-sol", .30, 1.50,
                           max_requests=12, budget_rmb=2)
        consent = LLMConsent.for_materials(resume, config, limits, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    confirmed_data_transfer=True, confirmed_paid_calls=True)
        client = MockResponsesClient(resume)
    elif args.command == "check" and args.mode == "llm":
        if not args.consent:
            raise PermissionError("真实自动LLM默认关闭；须先确认数据范围、预算，再提供 --consent。")
        config = LLMConfig.from_env()
        consent = LLMConsent(**json.loads(Path(args.consent).read_text()))
    runtime = AutomationRuntime(PROJECT, lambda *_: None, state_dir=args.state_dir, session_id="automation",
                                limits=limits, llm_config=config, consent=consent, resume=resume,
                                client_factory=client.factory if client else None, simulation=client is not None)
    try:
        store = runtime.automation
        if args.command == "stop":
            store.stop()
            return {"stopped": True}
        if args.command == "resume":
            store.resume(confirmed=args.confirm_resume)
            return {"stopped": False}
        if args.command == "status":
            return store.status()
        if args.command == "reconcile":
            if not args.attempt:
                raise ValueError("恢复需要 --attempt 申请记录ID。")
            return await ApplicationService(store, limits, MockATS(store)).reconcile(args.attempt)
        if args.command == "prepare":
            if args.mode != "local":
                raise PermissionError("自动材料仅local；LLM材料请使用现有/tailor的独立授权入口。")
            if not args.job:
                raise ValueError("准备材料需要 --job 岗位编号。")
            job = store.memory.library.get(args.job)
            reports = store.memory.list_reports(job["jd"])
            report = next((r for r in reversed(reports) if r.get("resume_match", {}).get("matching", {}).get("resume_fingerprint") == fingerprint(resume)), None)
            candidate = JobCandidate(job["company"], job["title"], job["city"], job["recruitment_type"], job["jd"], job["source"],
                job["detail_url"] or job["source_url"], datetime.now(timezone.utc).isoformat())
            adapter = MockATS(store) if args.adapter == "mock" else ManualHandoff()
            facts = json.loads(Path(args.facts).read_text()) if args.facts else {}
            prepared = await ApplicationService(store, limits, adapter).prepare(runtime, args.job, candidate, report, resume, facts)
            return {key: value for key, value in prepared.items() if key != "packet"}
        if args.command == "demo":
            jobs = candidates(datetime.now(timezone.utc))
            search = {"candidates": jobs, "http_requests": 0, "issues": []}
        else:
            args.search_demo = False
            search = await search_once(runtime, args, resume)
        adapter = None
        facts = json.loads(Path(args.facts).read_text()) if args.facts else {}
        if args.command == "demo":
            if args.application_mode == "browser-mock":
                from job_agent.browser.adapter import OfflineBrowserATS
                adapter = OfflineBrowserATS(store)
            else:
                adapter = MockATS(store)
            facts = {"name": {"value": "人工候选人", "quote": "姓名：人工候选人", "confirmed": True},
                     "email": {"value": "candidate@example.invalid", "quote": "邮箱：candidate@example.invalid", "confirmed": True}}
        elif args.application_mode != "off":
            if args.application_mode == "browser-mock":
                from job_agent.browser.adapter import OfflineBrowserATS
                adapter = OfflineBrowserATS(store)
            else:
                adapter = MockATS(store) if args.application_mode == "mock" else ManualHandoff()
        result = await workflow(runtime, search, resume, adapter=adapter, facts=facts,
                                city_min=args.city_min, guangzhou_only=args.guangzhou_only)
        return {**result["summary"], "offline_mock": client is not None, "paid_api_calls": 0 if args.mode != "llm" else runtime.llm.requests,
                "estimated_fee_rmb": 0 if args.mode != "llm" else runtime.llm.usage()["estimated_rmb"],
                "http_requests": search.get("http_requests", 0), "source_issues": search.get("issues", [])}
    finally:
        await runtime.close()


def main():
    args = parser().parse_args()
    try:
        result = asyncio.run(run(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, PermissionError, OSError) as error:
        # 不输出原始HTTP异常、路径内容或资料正文。
        print(json.dumps({"error_kind": type(error).__name__, "message": "未完成；请检查授权、额度、材料或状态。无自动重试。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
