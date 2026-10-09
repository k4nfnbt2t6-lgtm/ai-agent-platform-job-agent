"""人工材料的离线端到端演示，不接收真实简历或网站参数。"""

import argparse
import asyncio
import json
from datetime import datetime

from agent_platform.terminal_ui import PROJECT
from job_agent.automation.engine import AutomationRuntime
from job_agent.automation.mock import RESUME, candidates
from job_agent.automation.policy import Limits
from job_agent.automation.workflow import workflow
from job_agent.update_store import CHINA
from .adapter import OfflineBrowserATS
from .offline import render_snapshot


def synthetic_page(now):
    job = candidates(now)[0]
    posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": job.title,
               "hiringOrganization": {"name": job.company},
               "jobLocation": {"address": {"addressLocality": "广州"}},
               "employmentType": "INTERN", "description": job.jd, "url": job.url,
               "validThrough": job.valid_through}
    # 模拟内联JS生成结构化岗位信息；初始静态页面不含JobPosting节点。
    payload = json.dumps(posting, ensure_ascii=False).replace("<", "\\u003c")
    return """<!doctype html><meta charset="utf-8"><div id="jobs"></div><script>
const node=document.createElement('script'); node.type='application/ld+json';
node.textContent=JSON.stringify(POSTING); document.head.appendChild(node);
document.querySelector('#jobs').dataset.ready='true';
</script>""".replace("POSTING", payload)


async def demo(state_dir):
    now = datetime.now(CHINA)
    source = {"name": "人工离线浏览器招聘页", "url": "https://careers.example.invalid/jobs", "company": "人工自动化科技"}
    parsed = await render_snapshot(synthetic_page(now), source, source["url"],
                                   ready_selector='#jobs[data-ready="true"]', now=now.isoformat())
    runtime = AutomationRuntime(PROJECT, lambda *_: None, state_dir=state_dir, limits=Limits())
    try:
        adapter = OfflineBrowserATS(runtime.automation)
        facts = {"name": {"value": "人工候选人", "quote": "姓名：人工候选人", "confirmed": True},
                 "email": {"value": "candidate@example.invalid", "quote": "邮箱：candidate@example.invalid", "confirmed": True}}
        result = await workflow(runtime, {"candidates": parsed["jobs"], "issues": parsed["issues"], "http_requests": 0},
                                RESUME, adapter=adapter, facts=facts, guangzhou_only=True)
        summary = result["summary"]
        summary["browser_audit"] = adapter.last_audit
        summary["rendering"] = parsed["rendering"]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        await runtime.close()


def main():
    parser = argparse.ArgumentParser(description="Playwright离线人工材料演示；不会访问招聘网站或收费API。")
    parser.add_argument("--state-dir", default=".agent-platform/browser-demo")
    args = parser.parse_args()
    try:
        asyncio.run(demo(args.state_dir))
    except (RuntimeError, ValueError, PermissionError, TimeoutError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
