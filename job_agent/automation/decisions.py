"""资格先于分数；不把规则推断当作已获招聘方确认的事实。"""

import re
from datetime import datetime, timezone

from job_agent.analysis import analyze_jd, match_resume, match_scores
from job_agent.city_policy import city_fit
from job_agent.discovery import candidate_filter, graduation_fit


def decide(job, report, library_job, *, now=None, resume=None):
    now = now or datetime.now(timezone.utc)
    reasons, reviews = [], []
    fit = candidate_filter(job)
    reasons.extend(fit["reasons"])
    if library_job["status"] not in ("discovered", "shortlisted"):
        reasons.append("已有投递/终止状态：" + library_job["status"])
    if job.valid_through:
        try:
            expiry = datetime.fromisoformat(job.valid_through.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                reviews.append("截止时间没有时区，需要核对。")
            elif expiry <= now:
                reasons.append("岗位已超过截止时间。")
        except ValueError:
            reviews.append("截止时间无法可靠解析。")
    else:
        reviews.append("未给出可核对的截止日期，需要确认岗位仍在招聘。")
    if job.recruitment_type != "实习":
        (reviews if job.recruitment_type == "未知" else reasons).append("招聘身份不是已确认实习。")
    graduation = graduation_fit(job.jd)
    if graduation["status"] == "明确其他届别":
        reasons.append("不接受目标2027届。")
    elif graduation["status"] != "符合明确届别":
        reviews.append("2027届资格待确认。")
    if resume is not None:
        applicant = graduation_fit(resume)
        if applicant["status"] == "明确其他届别":
            reasons.append("候选人的毕业届别不符合2027届目标。")
        elif applicant["status"] != "符合明确届别":
            reviews.append("个人毕业届别未提供明确证据。")
        # 模型引用了真实文本也不等于正确理解学历/专业/年限；保留独立硬资格检查。
        local_jd = analyze_jd(job.analysis_text())
        local_match = match_resume(local_jd, resume)
        group_status = {r.get("group_id"): r["status"] for r in local_match["technical_matches"] if r.get("group_id")}
        for row in [*local_match["education"], *local_match["experience"]]:
            requirement = row["requirement"]
            if local_jd.get("qualification_levels", {}).get(requirement) == "preferred":
                continue
            group = local_jd.get("qualification_groups", {}).get(requirement)
            if group and group_status.get(group) == "matched":
                continue
            if row["status"] == "explicit_gap" and not group:
                reasons.append("原文硬资格不满足：" + requirement)
            elif row["status"] not in ("matched", "not_required"):
                reviews.append("原文资格仍需核对：" + requirement)
    if job.jd_kind != "full":
        reviews.append("只有JD摘要，资格信息可能不完整。")
    if len(city_fit(job)["cities"]) != 1:
        reviews.append("实际团队工作城市待确认。")
    if report is None:
        reviews.append("尚无完整分析结果。")
        matching, analysis = {}, {}
    else:
        matching = report["resume_match"]["matching"]
        analysis = report["jd_analysis"]["analysis"]
        if report.get("quality_degraded"):
            reviews.append("LLM失败后降级为local，不能据此自动投递。")
        groups = {r.get("group_id"): r["status"] for r in matching.get("technical_matches", []) if r.get("group_id")}
        for row in [*matching.get("education", []), *matching.get("experience", [])]:
            if row.get("level") == "preferred" or analysis.get("qualification_levels", {}).get(row["requirement"]) == "preferred":
                continue
            group = analysis.get("qualification_groups", {}).get(row["requirement"])
            if group and groups.get(group) == "matched":
                continue
            if row["status"] == "explicit_gap" and not group:
                reasons.append("硬资格不满足：" + row["requirement"])
            elif row["status"] not in ("matched", "not_required"):
                reviews.append("硬资格待核对：" + row["requirement"])
    # 没有学历资格的分析不能自动假定本科可投；必要条件未知转人工。
    if not re.search(r"本科|bachelor", job.jd, re.I):
        reviews.append("本科生是否可投待确认。")
    unclassified = analysis.get("unclassified", [])
    if any(re.search(r"每周|到岗|持续.{0,8}月|至少.{0,8}个月|仅限|必须.{0,12}专业|全日制|工作许可|国籍", line)
           for line in unclassified):
        reviews.append("存在尚未结构化确认的申请条件。")
    if re.search(r"每周.{0,8}[3-7三四五六七].{0,3}天|实习(?:时长|至少|不少于).{0,8}月|到岗时间|工作许可|国籍", job.jd):
        reviews.append("到岗周期/许可等额外资格需要本人逐项确认。")
    score = match_scores(matching["technical_matches"])["overall"]["percent"] if matching else None
    if matching:
        required = match_scores(matching["technical_matches"])["required"]
        if not required["total"]:
            reviews.append("未识别到明确必须技术要求，需要核对开发内容。")
        if required["unmatched"]:
            reviews.append("存在未匹配的必须技术，需要人工判断是否投递。")
    else:
        required = None
    return {"action": "skip" if reasons else "review" if reviews else "eligible",
            "reasons": list(dict.fromkeys(reasons)), "review_items": list(dict.fromkeys(reviews)),
            "overall_match": score, "required": required, "city": city_fit(job),
            "graduation": graduation, "qualification_overrides_score": bool(reasons)}
