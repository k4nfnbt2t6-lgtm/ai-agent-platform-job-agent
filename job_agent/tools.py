"""三个 Skill 的本地业务工具；不开放通用文件、网络、Shell 或简历修改。"""

from agents import function_tool

from agent_platform.workspace import safe_result

from .analysis import analyze_jd as analyze_text, match_resume as compare_resume, skill_gap_analysis as find_gaps
from .permissions import SKILL_ORDER, PREPARATION_SKILLS, TOOL_NAMES
from .preparation import resume_tailor as tailor_text, interview_prep as prepare_interview
from .llm import LLMError, LLMBudgetError, checked_gaps, checked_jd, checked_match, evidence_segments, grouped_requirements
from .schemas import GapOutput, JDOutput, MatchOutput


async def get_analysis(workspace):
    if workspace.analysis_result is not None:
        return workspace.analysis_result
    if workspace.jd is None:
        raise ValueError("请先由用户提供 JD。")
    backend = workspace.llm
    cached = workspace.memory.get_llm(workspace.jd, backend.config.model) if backend else workspace.memory.get(workspace.jd)
    if cached:
        workspace.analysis_result = {"ok": True, **cached, "cache_hit": True}
    else:
        if backend:
            output = await backend.ask("analyze-jd", {"jd": workspace.jd}, JDOutput)
            report = checked_jd(output, workspace.jd)
            record = workspace.memory.remember_llm(workspace.jd, report, workspace.source_label, backend.config.model)
        else:
            report = analyze_text(workspace.jd)
            record = workspace.memory.remember(workspace.jd, report, workspace.source_label)
        workspace.analysis_result = {"ok": True, "analysis": report, "jd_id": record["jd_id"],
                                     "cache_hit": False if backend else not record["new_record"] and not record["refreshed"],
                                     "record_updated": record["refreshed"]}
    return workspace.analysis_result


async def get_match(workspace):
    if workspace.resume is None:
        raise ValueError("匹配与差距分析需要用户提供简历或个人能力文本。")
    if workspace.match_result is None:
        analysis = await get_analysis(workspace)
        if workspace.llm:
            output = await workspace.llm.ask("match-resume", {"jd_analysis": analysis["analysis"],
                "technical_requirements": list(grouped_requirements(analysis["analysis"]).values()),
                "resume_segments": evidence_segments(workspace.resume)}, MatchOutput)
            matching = checked_match(output, analysis["analysis"], workspace.resume)
        else:
            matching = compare_resume(analysis["analysis"], workspace.resume)
        workspace.match_result = {"ok": True, "jd_id": analysis["jd_id"],
                                  "matching": matching}
    return workspace.match_result


async def get_gaps(workspace):
    matching = await get_match(workspace)
    if workspace.llm:
        output = await workspace.llm.ask("skill-gap-analysis", {"jd_analysis": workspace.analysis_result["analysis"],
                                         "matching": matching["matching"]}, GapOutput)
        workspace.gap_result = checked_gaps(output, matching["matching"])
    else:
        workspace.gap_result = find_gaps(matching["matching"])
    return {"ok": True, "jd_id": matching["jd_id"], "gaps": workspace.gap_result}


async def business_result(workspace, operation):
    try:
        return safe_result(await operation)
    except LLMError as error:
        if workspace.llm:
            workspace.llm.failed = True
            workspace.llm.last_error = str(error)
            workspace.llm.last_error_kind = "budget" if isinstance(error, LLMBudgetError) else "generation"
        # 给统一 Recorder 明确的安全错误，不让 SDK 包装异常隐藏 HTTP/证据失败原因。
        return {"ok": False, "error": str(error)}


def create_tools(workspace):
    @function_tool(failure_error_function=None)
    async def load_skill(names: list[str]) -> dict:
        """仅加载当前工作流登记的 Skill，不加载 Platform 的代码 Skill。"""
        selected = SKILL_ORDER if workspace.selected == "all" else (workspace.selected,)
        allowed = set(selected) & set(workspace.skills.specs)
        if not names or len(names) > 3 or len(set(names)) != len(names) or not set(names) <= allowed:
            return {"ok": False, "error": "只能加载 Job Agent 当前工作流的登记 Skill，不允许重复或越界。"}
        results = [workspace.skills.load(name) for name in names]
        if not all(result.get("ok") for result in results):
            return {"ok": False, "error": "Skill 未通过安全加载。"}
        result = safe_result({"ok": True, "name": ", ".join(names), "skills": results})
        if result.get("ok"):
            workspace.loaded.update(names)
        return result

    @function_tool(failure_error_function=None)
    async def analyze_jd() -> dict:
        """分析本次用户提供的 JD 并记录岗位；重复内容直接复用提取结果。"""
        workspace.require_skill("analyze-jd")
        return await business_result(workspace, get_analysis(workspace))

    @function_tool(failure_error_function=None)
    async def match_resume() -> dict:
        """使用当前简历对比 JD，附技术、学历、经验的文本依据；不复用旧简历匹配。"""
        workspace.require_skill("match-resume")
        return await business_result(workspace, get_match(workspace))

    @function_tool(failure_error_function=None)
    async def skill_gap_analysis() -> dict:
        """根据当前匹配区分学习差距与待确认信息，给出本岗位准备建议和验收方式。"""
        workspace.require_skill("skill-gap-analysis")
        return await business_result(workspace, get_gaps(workspace))

    async def preparation_result(name, operation):
        workspace.require_skill(name)
        if workspace.preparation_job is None or workspace.resume is None:
            raise ValueError("求职准备需要宿主选定岗位和当前简历。")
        if workspace.llm:
            from .preparation_llm import generate
            if not workspace.preparation_approved or workspace.preparation_plan is None:
                raise ValueError("缺少本次准备材料和费用确认，禁止请求模型。")
            workspace.preparation_approved = False  # 单次授权，不允许重复工具调用继续收费。
            data = await business_result(workspace, generate(workspace.llm, workspace.preparation_plan,
                                                            workspace.preparation_job, workspace.resume))
        else:
            data = safe_result(operation(workspace.preparation_job, workspace.resume))
        if data.get("ok") is False:
            return data
        workspace.preparation_result = data
        # 完整个人资料不放入工具返回/会话；宿主直接另存到本地私有报告。
        return {"ok": True, "skill": name, "job_id": data["job_id"], "mode": data["mode"]}

    @function_tool(failure_error_function=None)
    async def resume_tailor() -> dict:
        """整理真实简历证据和前后对比；文件另存由宿主完成，不提供通用写文件权限。"""
        return await preparation_result("resume-tailor", tailor_text)

    @function_tool(failure_error_function=None)
    async def interview_prep() -> dict:
        """生成本岗位的技术与真实项目面试准备；参考答案不当作个人经历。"""
        return await preparation_result("interview-prep", prepare_interview)

    tools = [load_skill, analyze_jd, match_resume, skill_gap_analysis, resume_tailor, interview_prep]
    if {tool.name for tool in tools} != TOOL_NAMES:
        raise ValueError("Job Agent 工具与权限配置不一致。")
    if workspace.selected in PREPARATION_SKILLS:
        return [load_skill, next(tool for tool in tools if tool.name == workspace.selected.replace("-", "_"))]
    return tools[:4]  # 原有分析/LLM 工作流继续只开放三个分析工具。


def report(workspace):
    data = {"mode": "llm" if workspace.llm else "local_rules", "jd_analysis": workspace.analysis_result,
            "resume_match": workspace.match_result}
    if workspace.selected in ("all", "skill-gap-analysis") and workspace.match_result:
        data["skill_gaps"] = workspace.gap_result or find_gaps(workspace.match_result["matching"])
    return data
