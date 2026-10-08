"""将已提供事实编排为中文简历；只调整结构、顺序和排版，不生成个人事实。"""

import re

from .analysis import LEARNING, matched_skills

PROJECT_NAMES = ("AI Agent Platform", "Job Agent", "Jieqi Agent")
HEADINGS = {
    "教育背景": "education", "教育经历": "education", "学历": "education",
    "项目经历": "projects", "项目经验": "projects", "个人项目": "projects",
    "技能与学习方向": "skills", "专业技能": "skills", "技术技能": "skills", "技能": "skills",
    "工作经历": "work", "实习经历": "work", "工作与实习经历": "work",
    "获奖经历": "other", "荣誉奖项": "other", "其他经历": "other", "补充信息": "other",
}
TOPICS = (r"工具调用|Tool Calling", r"重连|恢复|localStorage", r"权限|边界|受限",
          r"测试|回归|验收", r"简历|岗位|JD|去重", r"会话|Context|Session", r"API|接口")


def project_name(text):
    return next((name for name in PROJECT_NAMES if re.search(re.escape(name).replace(r"\ ", r"[ _-]+"), text, re.I)), None)


def relevance(text, analysis):
    score = sum(3 if req["level"] == "required" else 1 for req in analysis["technical_requirements"]
                if req["skill"] in matched_skills(text))
    duties = " ".join(analysis["responsibilities"])
    score += sum(3 for pattern in TOPICS if re.search(pattern, duties, re.I) and re.search(pattern, text, re.I))
    return score


def blocks(resume):
    """保留来源引文；只拼接 PDF 明确的项目项目符号续行。"""
    result, section, current_project = [], "profile", None
    physical = resume.splitlines()
    logical, index = [], 0
    while index < len(physical):
        original = physical[index]
        index += 1
        # 学习章节与其后续技能保持为整体，重排时不能把 MCP 等词移出弱证据语境。
        if re.match(r"\s*(?:#{1,6}\s*)?(?:正在学习|学习中|尚未熟练|了解中|想学习|准备学习|计划学习|打算学习)", original):
            while index < len(physical):
                following = physical[index].strip()
                if (following.strip("#* ：:") in HEADINGS
                        or re.match(r"已掌握|已有能力|学历|项目|工作经历|实习经历|目标|求职", following.lstrip("# "))):
                    break
                original += "\n" + physical[index]
                index += 1
        logical.append(original)
    for original in logical:
        text = re.sub(r"^#{1,6}\s+", "", original.strip())
        if not text:
            continue
        plain = text.strip("#* ：:")
        if plain in HEADINGS:
            section, current_project = HEADINGS[plain], None
            continue
        inline = re.match(r"^([^：:]+)[：:]\s*(.+)$", plain)
        if inline and inline[1] in HEADINGS and HEADINGS[inline[1]] != "projects":
            section, current_project = HEADINGS[inline[1]], None
            result.append({"section": section, "project": None, "text": inline[2],
                           "source": original, "bullet": False})
            continue
        if re.match(r"项目(?:经历|经验)?\s*[:：]", text):
            section = "projects"
            current_project = project_name(text) or text
        elif (project_name(text) and not re.match(r"[•●\-*]", text)
              and (section == "projects" or re.search(r"完成|开发|实现|构建|搭建|built|implemented", text, re.I))
              and not re.search(r"目标|计划|准备|正在学习|尚未熟练|未完成|未做过", text)):
            section = "projects"
            current_project = project_name(text)
        elif re.match(r"(?:目标|职业目标|求职方向|求职意向|求职目标)[:： ]", text):
            section, current_project = "profile", None
        elif re.match(r"(?:正在学习|学习中|尚未熟练|开发基础|Agent 实践|技术栈|已掌握|Python基础|API基础)", text, re.I):
            section, current_project = "skills", None
        elif section == "profile" and re.search(r"本科|硕士|博士|学院|大学", text) and not re.search(r"电话|邮箱|期望城市", text):
            result.append({"section": "education", "project": None, "text": text, "source": original})
            continue
        bullet = bool(re.match(r"[•●\-*]\s*", text))
        clean = re.sub(r"^[•●\-*]\s*", "", text)
        if "\n" in clean:
            clean = "；".join(part.strip() for part in clean.splitlines() if part.strip())
        # 长项目说明的下一物理行不是新事实。保留 source 的原始换行供审计。
        if (section == "projects" and result and result[-1]["section"] == "projects"
                and result[-1].get("bullet") and not bullet and not project_name(text)
                and not re.search(r"项目|·|开发基础|学习中", text)):
            result[-1]["text"] += clean
            result[-1]["source"] += "\n" + original
        else:
            result.append({"section": section, "project": current_project, "text": clean,
                           "source": original, "bullet": bullet})
    return result


def build_resume(job, resume, analysis, *, facts=None, project_order=None, skill_order=None):
    facts = blocks(resume) if facts is None else facts
    sections = {name: [item for item in facts if item["section"] == name]
                for name in ("profile", "education", "projects", "skills", "work", "other")}
    # 有真实姓名时用原姓名作标题；缺少姓名时只使用中性文档标题。
    profile = sections["profile"]
    has_name = bool(profile and re.fullmatch(r"[\u4e00-\u9fff]{2,5}", profile[0]["text"]))
    lines = ["# " + (profile.pop(0)["text"] if has_name else "技术实习简历")]
    lines += [f"求职意向：{job['title'] or '技术开发实习'} ｜ {job['city']}"]
    lines += [item["text"] for item in profile]

    def render(title, items):
        if items:
            lines.append("\n## " + title)
            lines.extend("- " + item["text"] for item in items)

    render("教育背景", sections["education"])
    # 技能只重排原技能事实，不将 JD 中缺失技能添加到简历。
    skills = sorted(sections["skills"], key=lambda item: (
        bool(re.search(LEARNING, item["text"])) and not re.search(r"基础", item["text"]),
        -relevance(item["text"], analysis)))
    if skill_order is not None:
        skills.sort(key=lambda item: skill_order.index(item["id"]))
    render("技术技能", skills)
    projects, intro = {}, []
    for item in sections["projects"]:
        if item["project"]:
            projects.setdefault(item["project"], []).append(item)
        else:
            intro.append(item)
    ordered = sorted(projects, key=lambda name: (
        -relevance("\n".join(item["text"] for item in projects[name]), analysis),
        PROJECT_NAMES.index(name) if name in PROJECT_NAMES else len(PROJECT_NAMES)))
    if project_order is not None:
        ordered = project_order
    if intro or projects:
        lines.append("\n## 项目经历")
        lines.extend(item["text"] for item in intro)
        for name in ordered:
            items = projects[name]
            title = items[0]["text"]
            lines.append("\n### " + re.sub(r"^项目(?:经历|经验)?\s*[:：]\s*", "", title))
            details = items[1:]
            # 保留原项目副标题；只重排实际项目要点，版本号和成果数字保持原样。
            subtitles = [item for item in details if not item.get("bullet")]
            bullets = sorted([item for item in details if item.get("bullet")], key=lambda item: -relevance(item["text"], analysis))
            lines.extend(item["text"] for item in subtitles)
            lines.extend("- " + item["text"] for item in bullets)
    render("工作与实习经历", sections["work"])
    render("补充信息", sections["other"])
    rendered = "\n\n".join(lines) + "\n"
    changes = [{"section": item["section"], "before": item["source"],
                "after": re.sub(r"^项目(?:经历|经验)?\s*[:：]\s*", "", item["text"]) if item["project"] else item["text"],
                "jd_evidence": analysis["responsibilities"][:2] or analysis["requirements_original"],
                "reason": "保留原事实，清理项目符号和 PDF 断行；按岗位相关性编排到对应章节。"} for item in facts]
    return rendered, {"changes": changes, "project_order": ordered,
                      "skill_order": [item["text"] for item in skills], "source_facts": facts}
