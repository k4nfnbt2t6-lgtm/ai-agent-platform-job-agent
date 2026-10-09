"""人工语料与离线Responses替身，只验证链路，不声称验证真实语义质量。"""

import json
from types import SimpleNamespace

from job_agent.analysis import analyze_jd, match_resume, validate_text
from job_agent.search import JobCandidate

RESUME = """姓名：人工候选人
邮箱：candidate@example.invalid
2027届本科在读，专业不限测试背景。
技能：Python基础
正在学习：LLM API、MCP
项目：AI Agent Platform
使用Python实现Tool Calling和输入校验，并补充异常处理测试。
项目：Job Agent
使用SQLite保存岗位并实现去重，保留岗位状态。
项目：Jieqi Agent
在AI辅助下复现同页面重连异常并补充测试。
"""


def candidates(now):
    base = {"company": "人工自动化科技", "title": "AI Agent开发实习生", "city": "广州", "recruitment_type": "实习",
            "jd": "工作地点：广州\n岗位职责：实现智能助手的工具调用和错误处理。\n任职要求：2027届本科在读，专业不限\n技术要求：Python、Tool Calling\n加分项：LLM API、MCP",
            "source": "本地模拟招聘平台（不是真实招聘）", "url": "https://careers.example.invalid/jobs/1",
            "discovered_at": now.isoformat(), "valid_through": "2099-12-31T23:59:59+08:00"}
    first = JobCandidate(**base)
    other = JobCandidate(**{**base, "company": "人工校招科技", "title": "AI Agent校招工程师", "recruitment_type": "校招",
                            "url": "https://careers.example.invalid/jobs/2"})
    return [first, first, other]


class MockResponsesClient:
    """与客户端相同的上下文/Responses接口，完全没有网络传输。"""
    def __init__(self, resume=RESUME, *, failure=None):
        self.resume = validate_text(resume, "人工简历")
        self.failure, self.calls = failure, []

    def factory(self, **kwargs):
        if kwargs.get("max_retries") != 0:
            raise AssertionError("离线验收也必须关闭收费自动重试。")
        return self

    async def __aenter__(self):
        self.responses = self
        return self

    async def __aexit__(self, *args):
        return False

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure == "timeout":
            raise TimeoutError("人工超时（含敏感正文也不应被日志记录）")
        request = json.loads(kwargs["input"][1]["content"])
        skill, payload = request["skill"], request["materials"]
        if skill == "analyze-jd":
            jd = payload["jd"]
            local = analyze_jd(jd)
            def statement(text):
                evidence = next((line for line in jd.splitlines() if text in line), text)
                return {"text": text, "evidence": evidence}
            value = {"company": statement(local["company"]) if local["company"] else None,
                     "title": statement(local["title"]) if local["title"] else None,
                     "responsibilities": [statement(x) for x in local["responsibilities"]],
                     "technical_requirements": [{key: row[key] for key in ("skill", "level", "evidence")} for row in local["technical_requirements"]],
                     "education_requirements": [{**statement(x), "level": local["qualification_levels"].get(x, "required")} for x in local["education_requirements"]],
                     "experience_requirements": [{**statement(x), "level": local["qualification_levels"].get(x, "required")} for x in local["experience_requirements"]],
                     "other_requirements": [], "keywords": local["keywords"], "limitations": ["离线替身，未验证模型真实语义质量。"]}
        elif skill == "match-resume":
            local = match_resume(payload["jd_analysis"], self.resume)
            def row(item, field):
                refs = [s["id"] for s in payload["resume_segments"] if s["quote"] in item["resume_evidence"]]
                return {field: item[field], "status": item["status"], "evidence_ids": refs, "reason": item["reason"]}
            value = {"technical_matches": [row(x, "skill") for x in local["technical_matches"]],
                     "education": [row(x, "requirement") for x in local["education"]],
                     "experience": [row(x, "requirement") for x in local["experience"]], "limitations": []}
        elif skill == "skill-gap-analysis":
            value = {"preparations": [{"skill": row["skill"], "preparation": "结合本岗位要求练习并保留真实代码与测试证据。",
                     "acceptance": "能解释实现、复现异常、通过针对性测试。"} for row in payload["matching"]["technical_matches"] if row["status"] != "matched"],
                     "note": "离线模拟建议，不代表已经掌握。"}
        else:
            raise ValueError("离线替身只支持三个现有分析Skill。")
        if self.failure == "schema":
            value = {"invalid": True}
        raw = json.dumps(value, ensure_ascii=False)
        return SimpleNamespace(usage=None if self.failure == "usage" else SimpleNamespace(input_tokens=1500, output_tokens=500),
                               status="completed", output_text=raw, model_dump_json=lambda: raw)
