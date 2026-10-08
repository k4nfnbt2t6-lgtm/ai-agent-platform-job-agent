"""v6 本地求职准备：摘录真实证据，不生成新的个人事实。"""

from .analysis import PRACTICE, analyze_jd, fingerprint, match_resume, skill_gap_analysis

from .preparation_resume import build_resume
from .preparation_questions import generate_questions, job_scene, preparation_evidence


STATUS_LABELS = {
    "matched": "已有项目或技能文本证据／可重点复盘（不代表熟练或精通）",
    "learning": "学习中／部分匹配，需补强",
    "not_evidenced": "未找到证据，先确认再学习",
    "needs_confirmation": "证据待确认，不能视为已掌握",
    "explicit_gap": "明确差距，需要学习",
}

# 只修正面试准备材料；不改变岗位评分与其他 Skill 的练习配置。
PREPARATION_PRACTICE = {
    "SQL": (
        "先核对 SQLite 项目中的真实查询、去重与测试实现；未提供的细节待确认。"
        "建议另建最小样例练习参数化查询、唯一约束和事务，不把建议写成现有功能。",
        "能区分实际项目证据与新练习；用样例验证参数绑定、重复写入和失败回滚，不能仅凭 SQLite 经历宣称 SQL 熟练。"),
    "LLM API": (
        "建议使用模拟 HTTP 练习 API 请求与响应、JSON 解析、HTTP 错误分类：401、429 和超时；"
        "说明停止、限流退避与重试边界，并记录 Token 与费用估算。真实付费请求须另行确认，不自动重试。",
        "覆盖成功、401、429、超时和无效响应；401 不重试，429 不盲目重发，超时标记结果及费用可能未知；"
        "解释输入/输出 Token 计费、预算和请求次数边界，日志不泄露密钥。"),
}


def preparation_checklist(data, resume, questions):
    """与题目共享准备证据；组合仍只占一项，不使用岗位得分证明熟练度。"""
    technical = {q["skill"]: q for q in questions if q["type"] == "technical"}
    rank = {"explicit_gap": 0, "not_evidenced": 1, "needs_confirmation": 2,
            "learning": 3, "matched": 4}

    def evaluate(node):
        if "children" not in node:
            name = node.get("skill") or node.get("qualification") or "未登记条件"
            if name in technical:
                question = technical[name]
                status, evidence = question["status"], question["resume_evidence"]
            elif "qualification" in node:
                # 资格证据不改写成技能掌握；仍保留原组合内的资格边界。
                status, evidence = node["status"], node["resume_evidence"]
            else:
                status, evidence = preparation_evidence(resume, name)
            return status, [{"skill": name, "status": status, "resume_evidence": evidence}]
        children = [evaluate(child) for child in node["children"]]
        choose = max if node["operator"] == "any" else min
        selected = choose(children, key=lambda value: rank.get(value[0], 2))
        targets = selected[1] if node["operator"] == "any" else [t for _, ts in children for t in ts]
        return selected[0], targets

    checklist = []
    for item in data["matching"]["technical_matches"]:
        status, targets = evaluate(item.get("expression_matches", item))
        tasks, checks = [], []
        for target in targets:
            name = target["skill"]
            task, check = PREPARATION_PRACTICE.get(name, PRACTICE.get(name, (
                "针对当前要求做最小例子；未知实现先核对，不作为已有能力。",
                "能说明输入、输出、失败路径和实际验证结果。")))
            tasks.append(f"建议准备 {name}：" + task)
            checks.append(f"{name}：" + check)
        choice = "组合仍只计一项；按所选分支准备，不要求同时掌握全部备选。" if item.get("group_id") else ""
        checklist.append({"skill": item["skill"], "priority": "P1" if item["level"] == "required" else "P2",
            "status": status, "action": STATUS_LABELS.get(status, "条件待核对"),
            "task": " ".join(tasks) + " 练习场景：" + job_scene(data["jd_analysis"]) + " " + choice,
            "acceptance": " ".join(checks), "jd_evidence": item["jd_evidence"],
            "resume_evidence": list(dict.fromkeys(e for target in targets for e in target["resume_evidence"])),
            "preparation_topics": targets, "group_id": item.get("group_id")})
    return checklist

# 固定知识点不涉及调用服务；参考答案与个人经历声明严格分开。
KNOWLEDGE = {
    "Python": ("如何处理工具函数的无效输入与异常，并验证结果？", "先校验类型与边界，再处理预期异常并返回明确错误；正常、边界、失败路径分别测试。", ["输入校验", "异常处理", "单元测试"]),
    "RAG": ("RAG 的检索、生成和证据引用如何配合？", "先切分并索引资料，根据问题检索相关片段，再携带来源生成回答；区分检索失败与生成失败，无证据时不能补造答案。", ["切分", "检索", "来源引用", "检索与回答评估"]),
    "MCP": ("MCP 接入工具与实际操作授权有什么区别？", "MCP 提供工具发现和调用约定；具体允许读写什么仍由宿主和工具权限控制，连接成功不代表获得全部操作权限。", ["工具发现", "参数校验", "权限边界"]),
    "Tool Calling": ("工具调用中模型和宿主分别负责什么？", "模型提出工具名称与参数，宿主校验权限并执行，将结果回传；要处理未知工具、无效参数、执行失败及有界停止。", ["模型提议", "宿主执行", "失败处理", "停止条件"]),
    "LLM API": ("如何处理模型 API 超时、限流和费用？", "设置超时、请求和输出上限；区分可重试错误与鉴权等失败，限制重试次数并记录用量，日志不能包含密钥。模拟调用不等于真实接口实战。", ["超时", "有界重试", "用量", "敏感信息"]),
    "SQL": ("如何防止岗位记录重复并安全查询？", "使用稳定标识或唯一约束识别重复，参数化查询避免拼接用户输入，事务保证相关写入一致；重启后验证记录仍存在。", ["唯一约束", "参数化查询", "事务"]),
    "Git": ("一次修改如何做到可以审查和回退？", "先查看状态与差异，隔离修改并提交有明确目的的变更；区分撤销工作区、撤销提交与改写历史，上传前检查敏感资料。", ["差异", "分支", "提交", "回退"]),
    "Agents SDK": ("如何组织 Agent、工具和会话而不复制运行底座？", "把业务指令和工具配置与运行循环分开，复用会话存储和运行器；业务工具负责校验输入与权限，会话资料不应扩大授权。", ["配置与运行分离", "工具", "会话隔离"]),
    "AI 应用开发": ("怎样证明一个 Agent 应用能稳定完成业务任务？", "定义输入、工具、状态和停止条件，保留可核对证据；用固定样例验证成功、失败和权限边界，区分模型输出与实际执行结果。", ["工作流", "证据", "可重复评估"]),
    "评估测试": ("如何为 Agent 建立可复现的回归评估？", "固定输入和预期行为，覆盖误匹配、无证据、工具失败与越权；离线 mock 验证逻辑，真实接口验收单独记录，不混淆两类结果。", ["固定样例", "边界", "离线与真实验收"]),
    "Docker": ("镜像与容器有什么区别？", "镜像是运行环境与应用的模板，容器是基于镜像启动的实例；持久数据需单独安排，不能依赖容器可写层永久保存。", ["镜像", "容器", "持久化"]),
    "FastAPI": ("API 参数校验失败与业务执行失败如何区分？", "入口先校验请求结构和类型，再执行业务；为无效请求与业务异常返回明确状态和错误，并分别测试。", ["请求校验", "业务错误", "接口测试"]),
    "向量检索": ("向量召回如何验证确实找到相关资料？", "建立固定查询与相关片段标注，检查召回结果及排名；向量相似度不是事实正确性的保证，仍需要引用和后续回答评估。", ["嵌入", "召回", "相关性评估"]),
}


def context(job, resume):
    """当前简历始终重算；不复用其他简历或旧 JD 的个人能力结论。"""
    analysis = analyze_jd(job["jd"])
    matching = match_resume(analysis, resume)
    return {"schema_version": 1, "mode": "local", "job_id": job["job_id"],
            "company": job["company"], "title": job["title"], "city": job["city"],
            "source_url": job["detail_url"] or job["source_url"],
            "jd_fingerprint": fingerprint(job["jd"]), "resume_fingerprint": fingerprint(resume),
            "jd_analysis": analysis, "matching": matching,
            "gaps": skill_gap_analysis(matching), "model_requests": 0, "estimated_rmb": 0}


def resume_tailor(job, resume):
    data = context(job, resume)
    draft, layout = build_resume(job, resume, data["jd_analysis"])
    data.update(skill="resume-tailor", tailored_resume=draft, **layout,
                note="正式简历与修改说明分开保存；只重组已有事实，保留学习与辅助开发限定，不添加经历或成果。")
    return data


def interview_prep(job, resume):
    data = context(job, resume)
    questions = generate_questions(data, resume, KNOWLEDGE)
    checklist = preparation_checklist(data, resume, questions)
    data.update(skill="interview-prep", questions=questions, checklist=checklist,
                note="固定 10 道岗位技术与真实项目题；答案用于学习，不证明已经掌握。项目细节按实际经历核实。")
    return data
