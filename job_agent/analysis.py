"""离线、可检查的 JD 分析；不把词命中当作精通或录用概率。"""

import hashlib
import re
import unicodedata

from .permissions import MAX_MATERIAL_BYTES

ANALYSIS_VERSION = 7

SKILLS = {
    "Python": r"\bpython\b",
    "Java": r"\bjava\b",
    "C++": r"(?<![a-z0-9_])c\+\+(?![a-z0-9_+])",
    "Shell": r"\bshell\b|\bbash\b",
    "JavaScript": r"\bjavascript\b|\bjs\b",
    "TypeScript": r"\btypescript\b",
    "SQL": r"\bsql\b|\bsqlite\b|\bmysql\b|\bpostgresql\b",
    "Git": r"\bgit\b|版本控制",
    "Linux": r"\blinux\b",
    "Docker": r"\bdocker\b|容器化",
    "FastAPI": r"\bfastapi\b",
    "Flask": r"\bflask\b",
    "PyTorch": r"\bpytorch\b",
    "RAG": r"\brag\b|检索增强(?:生成)?",
    "MCP": r"\bmcp\b|model context protocol",
    "Tool Calling": r"tool\s*calling|function\s*calling|工具调用|函数调用",
    "Agents SDK": r"\bagents?\s+sdk\b",
    "LLM API": r"\bllm\s*api\b|openai\s*api|大模型\s*(?:api|接口)|模型\s*api",
    "AI 应用开发": r"ai\s*应用|llm\s*应用|大模型应用|ai\s*agent(?:\s*开发)?|智能体应用",
    "LangChain": r"\blangchain\b",
    "LangGraph": r"\blanggraph\b",
    "向量检索": r"向量检索|向量数据库|\bfaiss\b|\bmilvus\b|\bchroma\b",
    "评估测试": r"\bevals?\b|单元测试|自动测试|评估(?:集|指标|测试)",
}
# 英文技术词常与中文连写；ASCII 边界既能匹配“熟悉Python”，也能排除 Pythonista。
SKILLS = {name: re.sub(r"\\b(.+?)\\b", lambda match: r"(?<![a-z0-9_])" + match[1] + r"(?![a-z0-9_])", pattern)
          for name, pattern in SKILLS.items()}
PREFERRED = r"加分|优先|更佳|非必需|非必须|nice[- ]to[- ]have|preferred|bonus|a plus"
REQUIRED = r"熟悉|掌握|熟练|具备|了解|能够|要求|需要|必须|经验|proficien|experience|knowledge|familiar|must|required"
EDUCATION = r"学历|本科|硕士|博士|研究生|学士|bachelor|master|ph\.?d|degree|在读|专业"
EXPERIENCE = r"经验|项目经历|实习经历|工作年限|experience|\d+\s*(?:年|个月|years?|months?)"
NEGATIVE = r"不会|未掌握|未用过|没用过|未做过|没有.*(?:经验|实践)|不熟悉|尚未(?:掌握|使用|用过|接触)|暂无|不了解|不懂|无.*(?:经验|经历)|no experience|not familiar|never used|do not know"
LEARNING = r"正在学习|学习中|初学|入门|计划|准备|想学|打算|接下来学|尚未熟练|还未熟练|还不熟练|未熟练|不太熟练|不够熟练|不熟练|了解中|正在了解|初步了解|仅了解|只了解|基础|learning|plan to learn|beginner|basic"
PROJECT_NAMES = r"(?<![a-z0-9_])(?:ai[ _-]+agent[ _-]+platform|job[ _-]+agent)(?![a-z0-9_])"
AI_PROJECT = r"ai\s*应用|llm\s*应用|大模型应用|智能体|ai\s*agent|" + PROJECT_NAMES
PROJECT_ACTION = r"完成|做过|开发|实现|构建|搭建|built|implemented|developed|created"
PROJECT_NEGATIVE = r"未完成|没(?:有)?完成|尚未完成|未做过|没(?:有)?做过|没有.*项目|无.*项目(?:经验|经历)"
PROJECT_PLANNED = r"计划|准备|打算|想做|正在开发|开发中|正在做|正在学习|学习中|了解中"
INTENT_HEADERS = {"目标", "职业目标", "求职目标", "求职方向", "职业方向", "职业规划", "目标岗位", "寻找方向"}
PROJECT_HEADERS = {"项目", "项目经历", "项目经验", "个人项目", "个人项目经历"}
NEUTRAL_HEADERS = {"已掌握", "掌握", "熟悉", "熟练", "技能", "技术栈", "当前技能", "已有能力", "学历", "工作经历", "实习经历"} | PROJECT_HEADERS
INTENT_TEXT = r"^(?:我(?:的)?\s*)?(?:目标(?:是|为|岗位)?|职业目标|求职目标|求职方向|职业方向|职业规划|(?:正在)?寻找|想找|希望(?:从事|成为|进入)|想(?:从事|成为)|期望(?:从事|进入))"


def normalized(text):
    # 保留行边界：标题换行会改变职责/要求归属，不能与同一行混为一份 JD。
    lines = unicodedata.normalize("NFKC", text).casefold().splitlines()
    return "\n".join(" ".join(line.split()) for line in lines if line.strip())


def fingerprint(text):
    return hashlib.sha256(normalized(text).encode("utf-8")).hexdigest()


def validate_text(text, label):
    if not isinstance(text, str) or not text.strip():
        raise ValueError(label + "不能为空。")
    if len(text.encode("utf-8")) > MAX_MATERIAL_BYTES:
        raise ValueError(label + "超过本版单文件 64 KB 上限。")
    return unicodedata.normalize("NFKC", text).strip()


def clauses(text):
    result = []
    for line in re.split(r"[\n；;。]+", text):
        line = re.sub(r"^[\s#>*\-]+", "", line).strip()
        line = re.sub(r"^\d+[.)、]\s*", "", line)
        if line:
            result.append(line)
    return result


def matched_skills(text):
    return [name for name, pattern in SKILLS.items() if re.search(pattern, text, re.I)]


def jd_heading(header):
    """编号、Markdown、括号不改变章节含义，避免加分标题漏识别。"""
    header = header.strip().strip("*_# 【】[]()（）")
    header = re.sub(r"^(?:[一二三四五六七八九十]+|\d+)[、.)]\s*", "", header).strip().strip("【】[]()（）")
    if re.fullmatch(r"岗位职责|工作职责|职位描述|岗位描述|工作内容(?:描述)?|职责|你将负责|responsibilities|what you(?: will|'ll) do", header, re.I):
        return "responsibilities"
    if re.fullmatch(r"岗位要求|职位要求|任职要求|任职资格|招聘要求|技术要求|资格要求|基本要求|我们期待你|requirements|qualifications", header, re.I):
        return "requirements"
    if re.fullmatch(r"加分项|加分条件|优先条件|优先项|nice[- ]to[- ]have|preferred|bonus", header, re.I):
        return "preferred"
    if re.fullmatch(r"学历(?:要求)?|专业(?:要求)?|学历\s*[/、]\s*专业要求|教育(?:要求|背景)|education requirements", header, re.I):
        return "education"
    if re.fullmatch(r"经验要求|工作经验|项目经验|experience requirements", header, re.I):
        return "experience"
    if re.fullmatch(r"你将获得|福利(?:待遇)?|薪资(?:待遇)?|工作地点|企业简介|公司简介|其他信息|申请方式|投递方式|申请材料|联系方式|benefits|about us", header, re.I):
        return "other"
    return None


# 组合条件只计一个评分单位；树结构保留“或”和“且”的区别。
CHOICE = r"至少(?:掌握|熟悉|会|满足)?(?:以下)?(?:一门|一种|一项|一个)|任一|任选|任何一(?:门|种|项)|至少一(?:门|种|项)"
INTEREST = r"感兴趣|兴趣|热情|意愿|愿意(?:学习|持续学习|动手)|interest(?:ed)? in"
APPLICATION = r"(?:附|提供|提交|发送|投递).*(?:Git(?:Hub)?\s*链接|作品(?:集)?\s*链接|项目(?:材料|链接)|作品集|简历)|(?:Git(?:Hub)?|作品|项目)\s*链接"


def split_education_preference(line):
    """只切开明确的门槛+偏好，不改写原文、不把专业例子变成技能。"""
    parts = re.split(r"[,，]", line, maxsplit=1)
    if (len(parts) == 2 and degree_level(parts[0]) is not None
            and re.search(PREFERRED, parts[1], re.I)
            and re.search(r"专业|计算机|软件|信管|自动化|大数据|硕士|博士", parts[1])):
        return [part.strip() for part in parts]
    return [line]


def choice_expression(text):
    # “Python和Git 或 Java和Docker”：每个或分支内部仍需全部满足。
    parts, begin, depth = [], 0, 0
    for match in re.finditer(r"[()]|或者|或是|或|(?<![a-z])or(?![a-z])", text, re.I):
        token = match[0]
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth-1)
        elif not depth:
            parts.append(text[begin:match.start()]); begin = match.end()
    parts.append(text[begin:])
    if (len(parts) > 1 and any(matched_skills(part) or re.search(EDUCATION + "|" + EXPERIENCE, part, re.I) for part in parts)
            and all(not re.fullmatch(r"\s*(?:了解|尝试|尝试过|掌握|熟悉|使用|会|understand|use)\s*", part, re.I) for part in parts)):
        children = []
        for part in parts:
            children.append(choice_expression(part.strip()))
        return {"operator": "any", "children": children, "evidence": text}
    names = matched_skills(text)
    if re.search(CHOICE, text) and len(names) > 1:
        return {"operator": "any", "children": [{"skill": name, "evidence": text} for name in names], "evidence": text}
    # 括号里的或条件不能越过括号，把外面的必需技能变成任选。
    opening = text.find("(")
    if opening >= 0:
        depth = 0
        for index in range(opening, len(text)):
            depth += (text[index] == "(") - (text[index] == ")")
            if depth == 0:
                inner = text[opening+1:index]
                if matched_skills(inner):
                    children = [{"skill": name, "evidence": text[:opening]} for name in matched_skills(text[:opening])]
                    children.append(choice_expression(inner))
                    children.extend({"skill": name, "evidence": text[index+1:]} for name in matched_skills(text[index+1:]))
                    return children[0] if len(children) == 1 else {"operator": "all", "children": children, "evidence": text}
                break
    leaves = [{"skill": name, "evidence": text} for name in names]
    if not leaves:
        if re.search(EDUCATION, text, re.I):
            return {"qualification": text.strip(), "kind": "education", "evidence": text}
        if re.search(EXPERIENCE, text, re.I):
            return {"qualification": text.strip(), "kind": "experience", "evidence": text}
        # 未登记条件保留待核对，绝不把一个空分支当作已经满足。
        return {"skill": None, "evidence": text}
    return leaves[0] if len(leaves) == 1 else {"operator": "all", "children": leaves, "evidence": text}



def expression_satisfied(node, predicate):
    """项目经验也使用同一组合语义，不能要求任选语言全部出现。"""
    if "skill" in node:
        return node["skill"] is not None and predicate(node["skill"])
    if "qualification" in node:
        return False  # 项目技能文本不替代资格证明。
    checks = [expression_satisfied(child, predicate) for child in node["children"]]
    return bool(checks) and (any(checks) if node["operator"] == "any" else all(checks))


def expression_qualifications(node):
    if "qualification" in node:
        yield node
    for child in node.get("children", []):
        yield from expression_qualifications(child)

def analyze_jd(text):
    text = validate_text(text, "JD")
    report = {"company": None, "title": None, "responsibilities": [], "technical_requirements": [],
              "education_requirements": [], "experience_requirements": [], "keywords": [],
              "requirements_original": [], "qualification_levels": {}, "unclassified": [],
              "interests": [], "application_materials": [], "requirement_groups": [], "qualification_groups": {}, "method": "local_rules_v3",
              "analysis_version": ANALYSIS_VERSION}
    section, section_level, active_group = None, "required", None
    seen = set()
    for original in clauses(text):
        pieces = re.split(r"[:：]", original, maxsplit=1)
        header = pieces[0].strip()
        content = pieces[1].strip() if len(pieces) == 2 else ""
        if re.fullmatch(r"公司(?:名称)?|company", header, re.I) and content:
            report["company"] = content
            continue
        if re.fullmatch(r"岗位(?:名称)?|职位(?:名称)?|job title|title", header, re.I) and content:
            report["title"] = content
            continue
        kind = jd_heading(header)
        if kind:
            active_group = None
            if kind in ("requirements", "preferred", "responsibilities", "other"):
                section_level = "preferred" if kind == "preferred" else "required"
            section = kind
            if not content:
                continue
            original = content
        report["keywords"] = list(dict.fromkeys([*report["keywords"], *matched_skills(original)]))
        if section == "other":
            report["unclassified"].append(original)
            if re.search(APPLICATION, original, re.I):
                report["application_materials"].append(original)
            continue
        if section == "responsibilities" or section is None and re.match(r"负责|参与|协助|搭建|维护|开发|设计|实现|优化", original):
            report["responsibilities"].append(original)
            continue
        # 逗号后的明确能力仍保留；兴趣/材料这一分句不进入技术评分。
        if re.search(INTEREST + "|" + APPLICATION, original, re.I):
            pieces = re.split(r"[,，]", original)
            remaining = []
            for part in pieces:
                if re.search(APPLICATION, part, re.I):
                    report["application_materials"].append(part)
                elif re.search(INTEREST, part, re.I):
                    report["interests"].append(part)
                elif re.search(REQUIRED, part, re.I) and matched_skills(part):
                    remaining.append(part)
            if not remaining:
                report["unclassified"].append(original)
                continue
            original = ",".join(remaining)
        if re.fullmatch(r"(?:至少满足|满足)(?:以下|下列)?(?:任一|一项)(?:条件)?[:：]?|以下任一(?:条件)?[:：]?", original):
            active_group = {"id": f"G{len(report['requirement_groups'])+1}", "level": section_level,
                            "evidence": [original], "expression": {"operator": "any", "children": [], "evidence": original}}
            report["requirement_groups"].append(active_group)
            report["requirements_original"].append(original)
            continue
        lines = [part for education_part in split_education_preference(original)
                 for part in re.split(r"[,，]\s*(?=(?:且|并|同时)?(?:熟悉|掌握|熟练|会写|具备|了解|需要|必须|能够))", education_part)]
        for line in lines:
            names = matched_skills(line)
            report["keywords"] = list(dict.fromkeys([*report["keywords"], *names]))
            if not (section in ("requirements", "preferred", "education", "experience") or re.search(REQUIRED + "|" + EDUCATION + "|" + EXPERIENCE, line, re.I)):
                report["unclassified"].append(line)
                continue
            report["requirements_original"].append(line)
            level = "preferred" if section_level == "preferred" or re.search(PREFERRED, line, re.I) else "required"
            educational = section == "education" or bool(re.search(EDUCATION, line, re.I))
            if educational:
                report["education_requirements"].append(line)
            if section == "experience" or re.search(EXPERIENCE, line, re.I):
                if original not in report["experience_requirements"]:
                    report["experience_requirements"].append(original)
            report["qualification_levels"][line] = level
            usable = []
            for name in names:
                fragments = [part for part in re.split(r"[,，]", line) if name in matched_skills(part)]
                if educational and not any(re.search(r"熟悉|掌握|熟练|了解|会写|具备|proficien|familiar", part, re.I) for part in fragments):
                    continue
                skill_level = "preferred" if section_level == "preferred" or all(re.search(PREFERRED, part, re.I) for part in fragments) else "required"
                usable.append((name, skill_level))
            # 组合块到普通软条件/资格条件为止，未知技术分支也保留。
            branch = bool(usable) or educational or bool(re.search(EXPERIENCE, line, re.I)) or bool(re.match(r"会|熟悉|掌握|了解|具备.*(?:模型|工具|技术)", line))
            if active_group and not branch:
                active_group = None
            group = active_group
            if group:
                expression = choice_expression(line)
                qualification = None
                if educational:
                    qualification = {"qualification": line, "kind": "education", "evidence": line}
                elif re.search(EXPERIENCE, line, re.I):
                    qualification = {"qualification": original, "kind": "experience", "evidence": original}
                if qualification and not list(expression_qualifications(expression)):
                    report["qualification_groups"][qualification["qualification"]] = group["id"]
                    expression = {"operator": "all", "children": [expression, qualification], "evidence": line} if usable else qualification
                group["expression"]["children"].append(expression)
                group["evidence"].append(line)
            elif (usable or educational or re.search(EXPERIENCE, line, re.I)) and (re.search(CHOICE, line) or re.search(r"或|(?<![a-z])or(?![a-z])", line, re.I)):
                expression = choice_expression(line)
                def has_choice(node):
                    return node.get("operator") == "any" or any(has_choice(c) for c in node.get("children", []))
                if has_choice(expression):
                    group = {"id": f"G{len(report['requirement_groups'])+1}", "level": level, "evidence": [line], "expression": expression}
                    if re.search(EXPERIENCE, line, re.I) and not list(expression_qualifications(expression)):
                        group["expression"] = {"operator": "all", "children": [expression,
                            {"qualification": original, "kind": "experience", "evidence": original}], "evidence": line}
                    report["requirement_groups"].append(group)
            for name, skill_level in usable:
                key = (name, skill_level, line)
                if key not in seen:
                    row = {"skill": name, "level": skill_level, "evidence": line}
                    if group:
                        row["group_id"] = group["id"]
                    report["technical_requirements"].append(row)
                    seen.add(key)
    report["requirement_groups"] = [g for g in report["requirement_groups"] if g["expression"].get("children")]
    for group in report["requirement_groups"]:
        qualifications = list(expression_qualifications(group["expression"]))
        if not qualifications:
            continue
        keys = {node["qualification"] for node in qualifications}
        for field in ("education_requirements", "experience_requirements"):
            report[field] = [q for q in report[field] if q not in group["evidence"] or q in keys]
        for node in qualifications:
            key = node["qualification"]
            report["qualification_groups"][key] = group["id"]
            report["qualification_levels"][key] = group["level"]
            field = node["kind"] + "_requirements"
            if key not in report[field]:
                report[field].append(key)
    report["limitations"] = ["本地词典与章节规则提取；未登记技术分支保留待核对，不视为已满足。",
                             "组合要求按一个条件评分，任选分支取最佳、同一分支的全部条件取最低证据分。",
                             "公司或岗位名未使用标签时可能无法自动识别；未说明的学历/经验不推断。"]
    return report


def evidence_header_context(header):
    """两个证据读取器共享章节判断，复合标题不依赖逐字白名单。"""
    header = header.strip().strip("*_ ")
    if header in INTENT_HEADERS:
        return "intent"
    if header in NEUTRAL_HEADERS:
        return "neutral"
    cleaned = re.sub(r"(?:的)?(?:技能|技术|能力)$", "", header).strip()
    remainder = re.sub(r"(?:想|计划|准备|打算|待)学习|" + LEARNING, "", cleaned)
    if remainder != cleaned and re.fullmatch(r"(?:\s|[/、,]|或|和|及|以及|部分匹配)*", remainder):
        return "learning"
    return None


def resume_segments(resume):
    """保留学习标题作用域和原文依据；逗号/转折后的技能不丢掉标题语境。"""
    context, heading = None, ""
    for original in clauses(unicodedata.normalize("NFKC", resume)):
        header, sep, body = original.partition(":")
        header = header.strip().strip("*_ ")
        inherited, label = context, heading
        kind = evidence_header_context(header)
        if kind is not None:
            inherited = kind if kind in ("learning", "intent") else None
            if kind == "neutral" and context == "learning" and header in ("技能", "技术栈", "当前技能"):
                inherited = context
            label = header if kind in ("learning", "intent") else heading if inherited else ""
            # 章节即使第一行有正文，也持续到明确的新章节；保留标题用于证据回溯。
            context, heading = inherited, label
            if not sep or not body.strip():
                continue
            text = body
        else:
            text = original
            if re.match(INTENT_TEXT, text, re.I):
                inherited, label = "intent", ""
            elif context == "intent":
                pass  # 职业目标章节中的“开发/完成”仍是目标，不能变成已做过的项目。
            elif re.match(r"正在学习|尚未熟练|了解中|初步了解|仅了解|只了解|(?:想|准备|计划|打算)学习", text):
                inherited, label = "learning", ""
            elif re.match(r"已掌握", text) or context is None and re.match(PROJECT_ACTION + r"|掌握|熟练使用|熟练掌握", text, re.I):
                inherited = None
                context, heading = None, ""
        evidence = original if not label or label in original else label + "：\n" + original
        for fragment in re.split(r"[,，]+|但是|但", text):
            fragment = fragment.strip()
            if fragment:
                scope = inherited
                if context is None and inherited != "intent" and re.match(r"已掌握|掌握|熟练使用|熟练掌握", fragment):
                    scope = None
                if re.match(INTENT_TEXT, fragment, re.I):
                    scope = "intent"
                yield fragment, evidence, scope


def project_records(resume):
    """项目名只证明相关领域；具体技术需来自项目自己的描述。"""
    records, current = [], None
    in_projects, project_heading = False, ""
    context, context_heading = None, ""
    for line in clauses(unicodedata.normalize("NFKC", resume)):
        header, sep, body = line.partition(":")
        header = header.strip().strip("*_ ")
        kind = evidence_header_context(header)
        if kind in ("learning", "intent"):
            context, in_projects, current = kind, False, None
            context_heading = header + "："
            if not sep or not body.strip():
                continue
            content, declared = body.strip(), False
        elif header.strip() in PROJECT_HEADERS:
            context, context_heading = None, ""
            in_projects, current = True, None
            project_heading = header.strip() + "："
            if not body.strip():
                continue
            content, declared = body.strip(), True
        else:
            content, declared = line, in_projects
            if kind == "neutral" and not (context == "learning" and header.strip() in ("技能", "技术栈", "当前技能")):
                context, context_heading = None, ""
        if context == "intent" or re.match(INTENT_TEXT, content, re.I):
            current = None
            continue
        named = bool(re.search(PROJECT_NAMES, content, re.I))
        candidate = named or bool(re.search(r"项目|project", content, re.I) and re.search(PROJECT_ACTION, content, re.I))
        if candidate:
            declared = declared or bool(re.search(r"项目(?:经历|经验)", content))
            # 学习/计划只影响项目声明本身，不让同一行随后“学习 MCP”抹掉已完成项目。
            claim = re.split(r"[,，]", content, maxsplit=1)[0]
            if re.search(PROJECT_NEGATIVE, claim, re.I):
                status = "explicit_gap"
            elif context == "learning" or re.search(PROJECT_PLANNED, claim, re.I):
                status = "learning"
            elif declared or re.search(PROJECT_ACTION, claim, re.I):
                status = "matched"
            else:
                status = "needs_confirmation"
            quote = project_heading + "\n" + line if in_projects and header.strip() not in PROJECT_HEADERS else line
            if context == "learning" and context_heading.rstrip("：") not in line:
                quote = context_heading + "\n" + quote
            current = {"text": quote, "status": status, "ai_application": bool(re.search(AI_PROJECT, content, re.I))}
            records.append(current)
        elif current and re.match(r"技术栈\s*:|使用|实现|接入|基于|负责|工具|功能", content):
            current["text"] += "\n" + line
        else:
            current = None
            if sep or re.search(LEARNING + "|学历|工作经历|实习经历", line, re.I):
                in_projects = False
    return records


def skill_evidence(resume, skill):
    positives, studying, negatives = [], [], []
    for line, evidence, context in resume_segments(resume):
        if context == "intent":
            continue
        if not re.search(SKILLS[skill], line, re.I):
            continue
        if re.search(NEGATIVE, line, re.I):
            negatives.append(evidence)
        elif context == "learning" or re.search(LEARNING, line, re.I):
            studying.append(evidence)
        else:
            positives.append(evidence)
    if skill == "AI 应用开发":
        for project in project_records(resume):
            if project["ai_application"]:
                if project["status"] == "matched":
                    positives.append(project["text"])
                elif project["status"] == "learning":
                    studying.append(project["text"])
                elif project["status"] == "explicit_gap":
                    negatives.append(project["text"])
    positives, studying, negatives = [list(dict.fromkeys(items)) for items in (positives, studying, negatives)]
    if positives and negatives:
        return "needs_confirmation", positives + negatives
    if negatives:
        return "explicit_gap", negatives
    # 当前明确限制熟练度时，项目中使用过也只能算部分匹配。
    if studying:
        return "learning", studying + positives
    if positives:
        return "matched", positives
    if skill == "LLM API":
        basic = [evidence for line, evidence, context in resume_segments(resume)
                 if context != "intent" and re.search(r"(?<![a-z0-9_])api(?![a-z0-9_])|接口基础|http\s*接口", line, re.I)]
        if basic:
            return "needs_confirmation", list(dict.fromkeys(basic))
    return "not_evidenced", []


def degree_level(text):
    if re.search(r"博士|ph\.?d|doctorate", text, re.I):
        return 3
    if re.search(r"硕士|研究生|master", text, re.I):
        return 2
    if re.search(r"本科|学士|bachelor", text, re.I):
        return 1
    if re.search(r"大专|专科|associate", text, re.I):
        return 0
    return None


def education_match(requirements, resume, levels=None):
    evidence = [x for x in clauses(resume) if re.search(EDUCATION, x, re.I)]
    confirmed = [x for x in evidence if not re.search(NEGATIVE + "|" + LEARNING + r"|计划|准备|目标|打算", x, re.I)]
    actual = degree_level(" ".join(confirmed))
    items = []
    for requirement in requirements:
        preferred = (levels or {}).get(requirement) == "preferred" or bool(re.search(PREFERRED, requirement, re.I))
        expected = degree_level(requirement)
        if re.search(r"本科|bachelor", requirement, re.I):
            # 本科/硕士均可或硕士优先，不能误判成最低必须硕士。
            expected = 1
        status = "not_evidenced"
        reason = "简历或 JD 未明确学历/专业，需确认。"
        if expected is not None and actual is not None:
            status = "matched" if actual >= expected else "explicit_gap"
            reason = "已匹配文本中的学历层次；在读/毕业状态及专业限制仍需核对。" if status == "matched" else "文本中的学历层次低于 JD。"
            if status == "explicit_gap" and preferred:
                status, reason = "needs_confirmation", "学历低于优先条件；需确认其是否必需，不直接判定不符合。"
        if not re.search(r"专业不限|不限专业|any major", requirement, re.I) and re.search(r"计算机|软件工程|computer science", requirement, re.I) and re.search(r"能源与动力|火电", resume):
            status = "needs_confirmation" if preferred else "explicit_gap"
            reason = "简历为能源与动力工程，JD 提及计算机/软件专业；需核对专业限制是否硬性。"
        items.append({"requirement": requirement, "status": status, "resume_evidence": evidence, "reason": reason})
    return items


def experience_match(requirements, resume):
    evidence = [x for x in clauses(resume) if re.search(EXPERIENCE + r"|项目|project", x, re.I)]
    projects = project_records(resume)
    items = []
    for requirement in requirements:
        status, reason = "needs_confirmation", "需要核对具体年限和经历；不能用项目代替工作/实习经验。"
        item_evidence = evidence
        target = requirement
        if re.search(r"经验不限|无需.*经验|不要求.*经验|no experience required", requirement, re.I):
            status, reason = "not_required", "JD 明确不要求经验。"
        elif re.search(r"工作|实习|professional|internship", requirement, re.I) and re.search(r"(?:无|没有).*(?:工作|实习).*(?:经验|经历)", resume):
            status, reason = "explicit_gap", "简历明确没有 JD 所提工作/实习经验。"
        elif re.search(r"项目(?:经验|经历)|project experience", requirement, re.I):
            # 将“有 AI 应用项目经验，掌握 MCP”分开；后一句不是项目经历的证明条件。
            parts = re.split(r"[,，]", requirement)
            contexts = []
            for index, part in enumerate(parts):
                if re.search(r"项目(?:经验|经历)|project experience", part, re.I):
                    if index and not matched_skills(part) and not re.search(AI_PROJECT, part, re.I):
                        contexts.append(parts[index - 1])
                    contexts.append(part)
            target = ",".join(contexts)
            technologies = matched_skills(target)
            needs_ai_application = bool(re.search(AI_PROJECT, target, re.I))
            # 未登记的具体项目领域（如“前后端项目经验”）不能用任意项目代替。
            # 纯“有项目经验”仍可由真实项目证明；更具体的未知条件转语义/人工核对。
            generic_project = bool(re.fullmatch(r"(?:有|具有|具备|拥有)?(?:一定的?|相关|实际|真实)?项目(?:经验|经历)", target.strip()))
            domain_known = bool(technologies or needs_ai_application or generic_project)
            relevant = [project for project in projects if project["status"] == "matched"
                        and domain_known
                        and (not needs_ai_application or project["ai_application"] or "AI 应用开发" in technologies)
                        and (not technologies or expression_satisfied(choice_expression(target),
                             lambda name: skill_evidence(project["text"], name)[0] == "matched"))]
            def explicitly_missing(node):
                if "skill" in node:
                    return node["skill"] is not None and skill_evidence(resume, node["skill"])[0] == "explicit_gap"
                if "qualification" in node:
                    return False
                values = [explicitly_missing(child) for child in node["children"]]
                return bool(values) and (all(values) if node["operator"] == "any" else any(values))
            if technologies and explicitly_missing(choice_expression(target)):
                status, reason = "explicit_gap", "简历明确未实践 JD 所要求的项目技术，不能用其他项目代替。"
            elif relevant:
                status, reason = "matched", "找到相关项目实践证据；具体质量仍需进一步核对。"
                item_evidence = [project["text"] for project in relevant]
            else:
                reason = "未找到该项相关项目实践证据；不能用其他项目或学习计划代替。"
        items.append({"requirement": target, "jd_evidence": requirement, "status": status,
                      "resume_evidence": item_evidence, "reason": reason})
    return items


STATUS_POINTS = {"matched": 1.0, "learning": 0.5}
REQUIREMENT_WEIGHTS = {"required": 2.0, "preferred": 1.0}
MATCH_REASONS = {
    "matched": "找到相关能力或实践的文本证据；不代表专家水平。",
    "learning": "原文保留学习、基础或尚未熟练语境，只能作为部分匹配。",
    "explicit_gap": "原文明确说明未掌握或未实践，不能计入匹配。",
    "not_evidenced": "未找到该项能力证据；职业目标和项目名称不能证明具体技术。",
    "needs_confirmation": "证据不足、矛盾或仅涉及一般基础，尚不能确认岗位所需能力。",
}


def match_scores(technical_matches):
    """只给校验后的技术要求评分；资格条件仍单独核对，不交给模型算分。"""
    scores = {"version": 2, "status_points": {**STATUS_POINTS, "unmatched": 0.0},
              "requirement_weights": dict(REQUIREMENT_WEIGHTS)}
    for level in REQUIREMENT_WEIGHTS:
        rows = [row for row in technical_matches if row["level"] == level]
        matched = sum(row["status"] == "matched" for row in rows)
        partial = sum(row["status"] == "learning" for row in rows)
        points = sum(STATUS_POINTS.get(row["status"], 0.0) for row in rows)
        scores[level] = {"matched": matched, "partial": partial,
                         "unmatched": len(rows) - matched - partial, "total": len(rows),
                         "points": points, "percent": round(100 * points / len(rows), 1) if rows else None}
    # 使用未四舍五入的原始分值与项目数，避免分组百分比再平均导致偏差。
    earned = sum(scores[level]["points"] * weight for level, weight in REQUIREMENT_WEIGHTS.items())
    possible = sum(scores[level]["total"] * weight for level, weight in REQUIREMENT_WEIGHTS.items())
    scores["overall"] = {"weighted_points": earned, "weighted_total": possible,
                         "percent": round(100 * earned / possible, 1) if possible else None}
    return scores


def grouped_matches(jd_report, individual, qualifications=()):
    """local/LLM共用组合计分，不让模型把或条件变成多项必须。"""
    by_skill = {row["skill"]: row for row in individual}
    by_qualification = {row["requirement"]: row for row in qualifications}
    for row in qualifications:
        group_id = jd_report.get("qualification_groups", {}).get(row["requirement"])
        if group_id:
            row["group_id"] = group_id
    groups = jd_report.get("requirement_groups", [])
    if not groups:
        return individual

    def evaluate(node):
        if "qualification" in node:
            row = by_qualification.get(node["qualification"])
            return {"qualification": node["qualification"], "jd_evidence": node["evidence"],
                    "status": row["status"] if row else "not_evidenced",
                    "resume_evidence": row["resume_evidence"] if row else [],
                    "reason": row["reason"] if row else "资格条件证据待确认。"}
        if "skill" in node:
            row = by_skill.get(node["skill"])
            return {"skill": node["skill"], "jd_evidence": node["evidence"],
                    "status": row["status"] if row else "not_evidenced",
                    "resume_evidence": row["resume_evidence"] if row else [],
                    "reason": row["reason"] if row else "未登记条件，需核对真实证据。"}
        children = [evaluate(child) for child in node["children"]]
        choose = max if node["operator"] == "any" else min
        tie = {"explicit_gap": 0, "not_evidenced": 1, "needs_confirmation": 2, "learning": 3, "matched": 4}
        best = choose(range(len(children)), key=lambda i: (STATUS_POINTS.get(children[i]["status"], 0.0), tie.get(children[i]["status"], 0)))
        used = [children[best]] if node["operator"] == "any" else children
        return {"operator": node["operator"], "status": children[best]["status"], "selected_branch": best if node["operator"] == "any" else None,
                "children": children, "resume_evidence": list(dict.fromkeys(x for c in used for x in c["resume_evidence"])),
                "reason": "任选条件取有最强证据的一项。" if node["operator"] == "any" else "同一分支的条件须全部满足。"}

    ordinary = {}
    for req in jd_report["technical_requirements"]:
        if req.get("group_id") or req["skill"] not in by_skill:
            continue
        row = ordinary.setdefault(req["skill"], {**by_skill[req["skill"]], "level": req["level"], "jd_evidence": []})
        row["jd_evidence"].append(req["evidence"])
        if req["level"] == "required":
            row["level"] = "required"
    result = list(ordinary.values())
    for group in groups:
        tree = evaluate(group["expression"])
        label = "任一条件：" + "；或 ".join(group["evidence"][1:] if len(group["evidence"]) > 1 else group["evidence"])
        result.append({"skill": label, "level": group["level"], "group_id": group["id"],
                       "jd_evidence": group["evidence"], "status": tree["status"], "resume_evidence": tree["resume_evidence"],
                       "reason": tree["reason"] + "组合只计一项；无需同时掌握所有可选技能。",
                       "expression_matches": tree})
    return result


def match_resume(jd_report, resume):
    resume = validate_text(resume, "简历/能力文本")
    items = []
    grouped = {}
    for item in jd_report["technical_requirements"]:
        key = item["skill"]
        grouped.setdefault(key, {"skill": key, "level": item["level"], "jd_evidence": []})
        grouped[key]["jd_evidence"].append(item["evidence"])
        if item["level"] == "required":
            grouped[key]["level"] = "required"
    for item in grouped.values():
        status, evidence = skill_evidence(resume, item["skill"])
        items.append({**item, "status": status, "resume_evidence": evidence, "reason": MATCH_REASONS[status]})
    education = education_match(jd_report["education_requirements"], resume, jd_report.get("qualification_levels"))
    experience = experience_match(jd_report["experience_requirements"], resume)
    items = grouped_matches(jd_report, items, [*education, *experience])
    required = [x for x in items if x["level"] == "required"]
    matched = sum(x["status"] == "matched" for x in required)
    return {"technical_matches": items, "required_coverage": {
                "matched": matched, "total": len(required),
                "percent": round(100 * matched / len(required), 1) if required else None},
            "match_scores": match_scores(items),
            "education": education,
            "experience": experience,
            "resume_fingerprint": fingerprint(resume),
            "limitations": ["技能匹配仅表示找到文本证据，不能证明熟练程度。",
                             "覆盖率仅针对词典识别的必需技术；不是整体适合度或录用概率。"]}


PRACTICE = {
    "RAG": ("用人工文档实现本地检索和来源引用；先复用 Platform 的文件与工具接口。", "固定问题能找到来源，无资料支持时回答不知道。"),
    "MCP": ("给现有工具做一个本地只读 MCP 适配样例；不连接外部账号。", "列出工具和参数，拒绝无效请求，并说明 MCP 与权限的区别。"),
    "Python": ("为现有 Platform 的一个工具补充输入校验和失败处理。", "能独立解释代码，正常、无效输入和异常三类测试通过。"),
    "Git": ("在本地练习查看差异、分支和提交；不自动 push。", "能解释一次具体修改和回退方法，提交不含密钥。"),
    "SQL": ("阅读并演示现有 SQLite 岗位记录的参数化查询和去重。", "同一岗位只保存一条记录，关闭重开仍能查询。"),
    "Tool Calling": ("演示模型提出请求、宿主校验执行、结果回传的完整流程。", "展示一次有效调用、越权拒绝和有界停止。"),
    "Agents SDK": ("复用现有 Runtime 配置业务 Agent，并解释 tools/session/hooks。", "新业务配置不复制 Runner，工具与会话相互隔离。"),
    "LLM API": ("使用模拟 HTTP 验证请求、超时和错误；真实调用另行确认预算。", "鉴权失败不重试，日志不显示密钥。"),
    "评估测试": ("为三个 Job Skill 建立固定 JD/简历评估样例。", "能指出误匹配、缺失要求和不应编造的经验。"),
}


def skill_gap_analysis(match_report):
    gaps, verify, strengths = [], [], []
    for item in match_report["technical_matches"]:
        if item["status"] == "matched":
            strengths.append({key: item[key] for key in ("skill", "level", "jd_evidence", "resume_evidence")})
            continue
        task, check = PRACTICE.get(item["skill"],
            (f'围绕 JD 的 {item["skill"]} 要求做一个最小示例，保留真实代码和说明。',
             "展示实际结果和失败路径，能解释与当前岗位职责的关系。"))
        if item.get("group_id"):
            task += " 可任选一条路径补强，不要求同时学习全部备选技术。"
        entry = {"skill": item["skill"], "priority": "P1" if item["level"] == "required" else "P2",
                 "status": item["status"], "jd_evidence": item["jd_evidence"], "preparation": task, "acceptance": check}
        if item["status"] in ("not_evidenced", "needs_confirmation"):
            entry["first_action"] = "先确认是否已掌握；如果有，补充真实项目证据，不直接判定不会。"
            verify.append(entry)
        else:
            entry["first_action"] = "针对 JD 补齐并验证这项技能，学习中不当作已掌握。"
            gaps.append(entry)
    key = lambda item: (item["priority"], {"explicit_gap": 0, "learning": 1}.get(item["status"], 2), item["skill"])
    return {"strengths": strengths,
            "learning_gaps": sorted(gaps, key=key), "evidence_to_confirm": sorted(verify, key=key),
            "priority_actions": sorted(gaps + verify, key=key)[:3],
            "qualification_checks": [x for x in [*match_report["education"], *match_report["experience"]]
                                     if x["status"] not in ("matched", "not_required") and not x.get("group_id")],
            "note": "先处理必需要求，再考虑加分项；学习技能不能自动解决学历/专业或经验限制。"}
