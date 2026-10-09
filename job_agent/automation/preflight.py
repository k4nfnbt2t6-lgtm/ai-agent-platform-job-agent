"""付费启用前的本地计划；不创建客户端、不发请求、不自动确认授权。"""

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from job_agent.channels import private_json
from job_agent.cli import read_authorized_text
from job_agent.llm import LLMAnalyzer, LLMConfig
from job_agent.resume_loader import load_resume
from .policy import LLMConsent, Limits


def plan(resume, jd, config, limits, *, now=None):
    analyzer = LLMAnalyzer(config)
    estimate = analyzer.estimate(jd, resume, 3)
    analyzer.preflight(estimate)
    expires = ((now or datetime.now(timezone.utc)) + timedelta(days=1)).isoformat()
    consent = LLMConsent.for_materials(resume, config, limits, expires)
    batch_cost = estimate["projected_rmb"] * limits.jobs_per_run
    batch_requests = 3 * limits.jobs_per_run
    return {"enabled": False, "data_scope": "完整指定简历、岗位JD、证据分段和必要的中间分析；不上传源码或其他本地文件",
            "limits": asdict(limits), "model": config.model, "base_url": config.base_url,
            "policies": {s: config.policy(s) for s in ("analyze-jd", "match-resume", "skill-gap-analysis")},
            "estimate_per_job": estimate, "max_batch_estimate_rmb": batch_cost,
            "max_batch_requests": batch_requests,
            "fits_daily_budget": batch_cost <= limits.daily_rmb,
            "fits_daily_requests": limits.daily_requests >= batch_requests,
            "process_limits": {"budget_rmb": config.budget_rmb, "max_requests": config.max_requests},
            "fits_process_limits": batch_cost <= config.budget_rmb and batch_requests <= config.max_requests,
            "consent_template": asdict(consent), "paid_requests": 0,
            "notice": "模板的两项确认均为false。须人工确认材料、价格、每日预算、次数、有效期后单独保存授权；本命令不启用LLM。"}


def main():
    parser = argparse.ArgumentParser(description="只估算和生成未授权计划，不调用API。")
    parser.add_argument("--resume", required=True)
    parser.add_argument("--jd", required=True)
    parser.add_argument("--daily-budget", type=float, default=0)
    parser.add_argument("--daily-requests", type=int, default=0)
    parser.add_argument("--analyze-limit", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        config = LLMConfig.from_env(require_key=False)
        value = plan(load_resume(args.resume)[0], read_authorized_text(args.jd)[0], config,
                     Limits(args.daily_budget, args.daily_requests, jobs_per_run=args.analyze_limit))
        private_json(value, args.output)
        print("计划已保存，付费与资料外发仍未授权。请审核范围、模型策略和估算费用。")
        return 0
    except (ValueError, PermissionError, OSError):
        print("计划未完成，请检查本地材料、模型/价格配置及额度。未发送请求。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
