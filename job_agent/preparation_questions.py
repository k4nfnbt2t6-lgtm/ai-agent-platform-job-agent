"""固定十题：按岗位与真实项目选题，答案是学习材料，不是个人能力声明。"""

import re

from .analysis import LEARNING, NEGATIVE, PRACTICE, SKILLS, skill_evidence, matched_skills
from .preparation_resume import PROJECT_NAMES, blocks, relevance

BANK = {
    "Python": ("工具收到 Python 字典或 JSON 字符串时，如何校验输入并处理异常？",
        "先区分类型；字符串先解析 JSON，再校验必填字段、类型和取值范围。只捕获预期异常并返回明确错误，不能用裸 except 吞掉所有问题。用正常、缺字段、类型错误和边界输入做测试。", ["JSON 解析", "类型与边界", "异常测试"]),
    "AI 应用开发": ("Agent 与固定工作流有什么区别？哪些步骤适合交给模型决定？",
        "固定工作流的步骤由程序预先规定；Agent 可根据任务和工具结果选择下一步。权限、预算、参数校验、状态写入和停止条件应由宿主控制。步骤明确时先用固定流程，避免模型决策增加不确定性。", ["决策与执行", "工作流", "停止条件"]),
    "Tool Calling": ("模型要求调用未登记工具，或给出错误参数时，宿主应该怎么做？",
        "工具名必须命中白名单，参数通过 schema 和业务校验后才能执行，再检查操作权限。未知工具或无效参数应明确拒绝，不能把模型输出直接拼成命令。工具结果回传后继续推理，并设置轮数上限。", ["白名单", "参数校验", "权限", "有界循环"]),
    "LLM API": ("模型 API 返回 401、429 或超时时，如何分别处理且控制费用？",
        "401 是鉴权问题，停止并检查配置；429 按服务端限流信息在预算内退避；超时不等于服务端没有执行，重试可能再次计费。设置总请求数、超时和输出上限，记录 token，用 mock 验证失败路径。", ["鉴权", "限流", "超时", "费用边界"]),
    "错误处理": ("一个多步骤任务中途失败，如何避免重复写入或把失败当成功？",
        "分别记录步骤状态和业务结果；写入使用唯一键、事务或幂等检查。重启后从可恢复状态继续，不能盲目重跑所有副作用。失败返回结构化错误，并覆盖取消、超时、部分成功和重启恢复测试。", ["幂等", "事务", "恢复", "部分失败"]),
    "Session/Context": ("会话历史越来越长时，如何保留关键上下文而不丢工具调用关系？",
        "完整历史保存在本地，模型输入保留近期对话和关键事实摘要。工具请求与结果应成对保留；区分长期记忆与当前任务状态。摘要只是资料，不得改变权限；用多轮任务和会话恢复验证。", ["历史存储", "摘要", "调用配对", "权限"]),
    "评估测试": ("如何验证 Agent 真正执行了工具，而不是只说“已经完成”？",
        "检查工具实际执行记录、返回结果和持久化产物，不能只看最后一句回复。固定成功、无证据、越权和工具失败样例；mock 检查流程，真实接口验收单独记录。评分与完成状态由程序校验。", ["执行证据", "回归样例", "mock 与真实验收"]),
    "权限边界": ("JD 或工具结果中出现“读取密钥并上传”的文字，为什么不能照做？",
        "JD、简历和工具输出是待处理资料，不是用户授权。工具必须独立限制可访问路径、网络、写入和收费行为；即使模型遵循了恶意文本，宿主仍应拒绝越权请求并记录安全错误。", ["不可信资料", "宿主授权", "最小权限"]),
    "结构化输出": ("模型返回合法 JSON，就可以直接保存为业务结论吗？",
        "不可以。先校验字段、类型和枚举，再核对原文证据、组合要求和熟练程度；合法 JSON 仍可能包含编造事实。失败时停止或回退本地规则，不把未经校验的内容写成已掌握。", ["结构校验", "证据校验", "fallback"]),
    "任务调度": ("手动检查和定时检查同时触发时，怎样避免两次分析同一岗位？",
        "用任务锁避免并发检查，以稳定岗位标识和 JD 内容版本去重；只分析新增或变化记录。手动执行不修改每日计划；持久化最近检查时间，重启后恢复计划，并用可控时间测试。", ["任务锁", "版本去重", "可控时间"]),
    "LangChain": ("使用 Agent 编排框架后，哪些工程责任仍然需要应用自己承担？",
        "框架能帮助组织模型、工具和流程，但数据来源、工具权限、业务校验、状态一致性和费用控制仍属于应用责任。先实现一个最小流程，再验证未知工具、无效参数和中途失败；会用框架不等于掌握全部 Agent 工程。", ["编排", "业务校验", "权限", "失败测试"]),
}

PROJECT_Q = {
    "AI Agent Platform": ("AI Agent Platform 的 Agent 循环、工具、权限和会话应怎样分层？新增业务 Agent 接在哪里？",
        "运行层负责循环、会话和停止控制；工具层执行具体操作并校验参数与权限；业务层提供 instructions、skills 和工具配置。新增业务 Agent 尽量复用运行层。个人回答只介绍简历已描述的实现，不补造未提供细节。",
        ["如果把权限校验只放在提示词中，会有什么问题？", "工具执行成功，但会话保存失败，如何定位和恢复？"]),
    "Job Agent": ("Job Agent 如何避免同一岗位重复分析？更换简历后哪些结果必须重新计算？",
        "区分岗位身份、JD 版本和简历版本。相同 JD 可复用职责与要求提取；匹配和差距结论依赖简历，换简历后应重算。真实项目若只实现了其中部分，就明确已完成范围，不把建议方案说成现有功能。",
        ["相同公司和岗位名，但 JD 变化时如何处理？", "为什么“学习 MCP”和“掌握 MCP”不能得到同样分数？"]),
    "Jieqi Agent": ("Jieqi Agent 遇到本地存储失败或同页面断线时，你会怎样复现并验证恢复逻辑？",
        "先固定复现步骤并隔离存储异常，再区分页面内状态、房间状态和临时连接信息；检查失效信息是否阻止恢复。补正常、存储异常、同页面重连和重复重连测试，保留原回归。只引用简历中真实修复与测试结果。",
        ["清理临时连接信息时，怎样避免误删仍有效状态？", "为何页面刷新后恢复成功，仍不能证明同页面重连已修好？"]),
}


def job_scene(analysis):
    duties = " ".join(analysis["responsibilities"])
    return "岗位记录处理" if re.search(r"岗位|数据库|SQL", duties, re.I) else "工具请求处理" if re.search(r"工具|调用", duties) else "检索数据处理" if re.search(r"RAG|检索", duties, re.I) else "业务输入处理"


def concrete_project_fact(fact):
    """项目名称仅提供身份；能力依据须含具体行动，保留辅助/本地等限定。"""
    return bool(fact.get("project") and re.search(
        r"实现|完成|开发|构建|校验|验证|测试|修复|复现|整理|生成|保存|built|implemented|tested",
        fact["text"], re.I) and not re.search(
        r"目标|求职|想学|计划|准备学习|正在寻找|希望从事|未完成|未做过", fact["source"]))


def topic_facts(facts, name):
    """有界的主题关联，不将通用技术题误作跨项目追问，也不把项目名当能力。"""
    relevant = []
    for fact in facts:
        if fact["section"] not in ("skills", "projects") or re.search(
                r"目标|求职|想学|计划|准备学习|正在寻找|希望从事", fact["source"]):
            continue
        if fact.get("project") and not concrete_project_fact(fact):
            continue
        text = fact["text"]
        if name == "AI 应用开发":
            matches = (name in matched_skills(text) or (
                concrete_project_fact(fact)
                and fact["project"] in ("AI Agent Platform", "Job Agent")
                and re.search(r"工具调用|Tool Calling|会话|权限|匹配报告|模型|LLM|Agent", text, re.I)))
        elif name == "错误处理":
            matches = bool(re.search(r"错误处理|异常|输入校验|失败|重连", text, re.I))
        else:
            matches = name in matched_skills(text)
        if matches:
            relevant.append(fact)
    # 有具体实现时允许连同标题引用作为上下文；标题本身永不证明能力。
    if name == "AI 应用开发":
        projects = {fact["project"] for fact in relevant}
        return [fact for fact in facts if fact in relevant or (
            fact.get("project") in projects and fact["text"] == fact["project"])]
    return relevant


def preparation_evidence(resume, name):
    """准备状态保留 Markdown 章节的基础/学习限定；不改变原评分或岗位库。"""
    status, evidence = skill_evidence(resume, name) if name in SKILLS else ("needs_confirmation", [])
    facts = topic_facts(blocks(resume), name)
    evidence = [fact["source"] for fact in facts]
    if name == "AI 应用开发":
        # 有实现描述也只证明局部项目实践，不能证明完整应用开发能力。
        status = "needs_confirmation" if any(concrete_project_fact(fact) for fact in facts) else "not_evidenced"
    elif name == "SQL" and status == "matched" and not any(
            re.search(r"\bSQL\b", fact["text"], re.I) for fact in facts):
        status = "needs_confirmation"  # SQLite 存储实践不等于 SQL 熟练度。
    elif status == "matched" and not facts:
        status = "not_evidenced"
    weak = [fact for fact in facts if fact["section"] in ("skills", "projects")
            and name in matched_skills(fact["text"])
            and not re.search(r"目标|求职|想学|计划|准备学习|正在寻找", fact["source"])
            and re.search(LEARNING + "|" + NEGATIVE, fact["source"], re.I)]
    if weak:
        status = "explicit_gap" if any(re.search(NEGATIVE, fact["source"], re.I) for fact in weak) else "learning"
        evidence = list(dict.fromkeys(evidence + [fact["source"] for fact in weak]))
    return status, evidence


def project_questions(data, resume):
    facts = blocks(resume)
    output = []
    for name in PROJECT_NAMES:
        entries = [item for item in facts if item["project"] == name]
        if not entries:
            continue
        if not any(concrete_project_fact(item) for item in entries):
            continue
        claim = entries[0]["text"].split(",")[0]
        if re.search(r"计划|准备|打算|想做|未完成|尚未完成|未做过|目标", claim):
            continue
        question, answer, followups = PROJECT_Q[name]
        output.append({"type": "project", "question_kind": "project_followup", "project": name, "question": question,
            "reference_answer": answer, "follow_ups": followups,
            "status": "needs_confirmation", "readiness": "真实项目追问；实现细节与本人贡献需核对",
            "resume_evidence": [item["source"] for item in entries],
            "jd_evidence": data["jd_analysis"]["responsibilities"][:2] or data["jd_analysis"]["requirements_original"],
            "knowledge_points": ["架构", "本人贡献", "错误处理", "验证"],
            "learning_advice": "先按真实代码和测试复盘；没有完成或没有记录的部分明确说明。",
            "answer_boundary": "参考思路不是你的经历声明，未提供的细节待补充。"})
    return sorted(output, key=lambda q: -relevance("\n".join(q["resume_evidence"]), data["jd_analysis"]))


def generate_questions(data, resume, knowledge):
    analysis = data["jd_analysis"]
    projects = project_questions(data, resume)
    wanted = []
    groups = {req["skill"]: req for req in analysis["technical_requirements"]}
    # 任选编程语言只选用户有最好证据的分支准备，不改原有组合评分。
    for item in data["matching"]["technical_matches"]:
        if item.get("group_id"):
            names = matched_skills(" ".join(item["jd_evidence"]))
            language = [name for name in names if name in ("Python", "Java", "C++", "JavaScript", "Shell")]
            if language:
                best = min(language, key=lambda name: {"matched": 0, "learning": 1}.get(skill_evidence(resume, name)[0], 2))
                wanted.append(best)
                continue
        wanted.extend(matched_skills(" ".join(item["jd_evidence"])) or [item["skill"]])
    # 岗位职责用于选题，不粘贴到每道题；准备题并不新增岗位必须技能。
    wanted.extend(matched_skills(" ".join(analysis["responsibilities"])))
    wanted.extend(("Python", "AI 应用开发", "Tool Calling", "LLM API", "错误处理", "Session/Context", "评估测试"))
    wanted.extend(BANK)
    core = ["Python", "AI 应用开发", "Tool Calling", "LLM API", "错误处理"]
    topics = list(dict.fromkeys(core + wanted))[:10-len(projects)]
    scene = job_scene(analysis)
    questions = []
    for name in topics:
        question, answer, points = BANK.get(name, knowledge.get(name, (
            f"{name} 在这个岗位中应解决什么问题，如何验证最小实现？",
            f"本地题库暂无 {name} 的专门答案。先核对官方资料，再用最小输入、预期输出和失败样例验证；不要声称已经掌握。",
            ["最小实现", "输入输出", "失败验证"])))
        if name == "Python":
            question += " 请以" + scene + "为例。"
        status, evidence = preparation_evidence(resume, name)
        matching = next((item for item in data["matching"]["technical_matches"] if item["skill"] == name), None)
        req = groups.get(name)
        group = req.get("group_id") if req else None
        level = "组合备选（非全部必须）" if group else "必须项" if req and req["level"] == "required" else "加分项" if req else "岗位相关工程准备"
        advice = "复盘已有实践并准备可运行示例。" if status == "matched" else "先确认现有证据；若未掌握，完成一个最小例子，再练习解释失败路径。"
        questions.append({"type": "technical", "question_kind": "jd_technical" if status == "matched" else "learning",
            "skill": name, "question": question,
            "reference_answer": name + "：" + answer, "knowledge_points": points,
            "status": status, "level": req["level"] if req else "preparation", "requirement_group": group,
            "readiness": {"matched": "已有文本证据，可复盘", "learning": "学习中／部分匹配",
                "not_evidenced": "未找到证据，先确认或学习", "explicit_gap": "明确差距，需要学习",
                "needs_confirmation": "证据待确认，不能视为已掌握"}[status] + "；" + level,
            "resume_evidence": evidence,
            "jd_evidence": matching["jd_evidence"] if matching else [req["evidence"]] if req else ["本岗位的通用工程准备；不额外计入必须技能"],
            "follow_ups": [], "learning_advice": advice,
            "answer_boundary": "以上是技术学习参考，不是你的经历声明；个人答案只能引用当前简历事实。"})
    questions += projects
    # 未识别技术时明确提示，仍提供十道通用开发准备题，不能伪称 JD 的硬性要求。
    if not analysis["technical_requirements"]:
        questions[0]["readiness"] += "；未识别技术要求，需要人工核对 JD"
    for index, question in enumerate(questions, 1):
        question["id"] = index
    return questions
