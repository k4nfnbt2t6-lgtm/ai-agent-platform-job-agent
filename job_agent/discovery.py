"""v3 本地筛选、持久去重和批量复用 v2；搜索层从不调用 LLM。"""

import json
import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from difflib import SequenceMatcher

from .analysis import ANALYSIS_VERSION, clauses, fingerprint, match_scores, matched_skills
from .city_policy import CITY_ORDER, city_fit, select_cities
from .schemas import SCHEMA_VERSION
from .search import AI_DIRECTION, JobCandidate, utc_now


def compact(text):
    # 保留 C++、C#、.NET 等技术名中的符号，不能把不同技术合并。
    return re.sub(r"[^\w+#./-]+", "", unicodedata.normalize("NFKC", text).casefold())


def requirement_signature(jd):
    """近似合并前保留技能、学历/届别/数值/否定与要求正文，防止合并实质变化。"""
    lines = []
    required_section = False
    for line in clauses(jd):
        if re.search(r"要求|资格|条件|requirement|qualification|加分|优先", line, re.I):
            required_section = True
        elif re.search(r"职责|工作内容|responsibilit|福利|benefit", line, re.I):
            required_section = False
        if required_section or matched_skills(line) or re.search(r"本科|硕士|博士|届|\d|不|无|未|must|not|without", line, re.I):
            lines.append(compact(line))
    return sorted(set(lines))


def similar_job(left, right):
    if compact(left.company) != compact(right.company) or compact(left.title) != compact(right.title):
        return False
    if compact(city_fit(left)["city"]) != compact(city_fit(right)["city"]) or left.recruitment_type != right.recruitment_type:
        return False
    a, b = compact(left.jd), compact(right.jd)
    if a == b:
        return True
    if min(len(a), len(b)) < 80 or requirement_signature(left.jd) != requirement_signature(right.jd):
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= .94


def graduation_fit(jd, year=2027):
    contexts = [line for line in clauses(jd) if re.search(r"届|毕业|graduat|class of", line, re.I)]
    if any(re.search(r"(?:不接受|不招|不面向|排除|不含|不包含)[^。\n]{0,12}" + str(year), line) for line in contexts):
        return {"status": "明确其他届别", "points": 0, "evidence": contexts}
    ranges, years = [], set()
    for line in contexts:
        ranges.extend((int(a), int(b)) for a, b in re.findall(r"(20\d{2})\s*[-~—至到]\s*(20\d{2})\s*(?:届|年.*毕业)", line))
        years.update(int(y) for y in re.findall(r"(20\d{2})\s*(?:届|年(?:的)?毕业)", line))
        years.update(int(y) for y in re.findall(r"(?:毕业年份|毕业年|class of|graduating(?: in)?|graduates of)\s*[:：]?\s*(20\d{2})", line, re.I))
        for group in re.findall(r"((?:20\d{2}\s*[/、,，及和或]\s*)+20\d{2})\s*届", line):
            years.update(int(y) for y in re.findall(r"20\d{2}", group))
    if any(a <= year <= b for a, b in ranges) or year in years:
        return {"status": "符合明确届别", "points": 100, "evidence": contexts}
    if years:
        return {"status": "明确其他届别", "points": 0, "evidence": contexts}
    return {"status": "2027届待确认", "points": 0, "evidence": contexts}


def candidate_filter(job):
    reasons = []
    location = city_fit(job)
    if location["priority"] == 4:
        reasons.append("实际工作地点不在广州/上海/深圳或地点待确认，本轮不进入核心候选。")
    if job.company == "未知公司":
        reasons.append("公司身份未提供，需核验后再分析。")
    related = bool(re.search(AI_DIRECTION, job.title, re.I))
    if re.search(r"产品经理|产品运营|商务|销售|市场营销|SEO|运营实习", job.title, re.I) and not re.search(r"研发|开发|工程师", job.title):
        reasons.append("明确产品/运营/商务岗位，不计入本轮AI应用开发实习候选。")
    if not related:
        reasons.append("岗位标题与 AI Agent / AI应用 / LLM / RAG 工程方向不相关。")
    if job.recruitment_type == "社招/全职" or re.search(r"高级|资深|senior|staff|principal", job.title, re.I):
        reasons.append("明确全职或资深岗位，本轮不进入实习分析；全职不等同于社招。")
    graduation = graduation_fit(job.jd)
    if graduation["status"] == "明确其他届别":
        reasons.append("JD 明确限定其他毕业届别，不把 2027 届视为符合。")
    if re.search(r"(?:硕士|博士)(?:及以上|研究生)?(?:学历)?(?:在读|毕业)?(?:学生)?[，,\s]*(?:必须|仅限)|(?:仅限|要求|必须)[^。\n]{0,8}(?:硕士|博士)|(?:硕士|博士)(?:及以上)?(?:学历)?[，,\s]*要求", job.jd):
        reasons.append("JD 明确要求研究生学历，本轮本科实习目标不进入付费分析。")
    if len(job.jd.strip()) < 30 or not re.search(r"要求|职责|资格|熟悉|掌握|技术|require|responsib|qualification", job.jd, re.I):
        reasons.append("仅有标题或过短摘要，缺少可靠 JD，需补充详情。")
    if job.valid_through:
        try:
            deadline = datetime.fromisoformat(job.valid_through.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline < datetime.now(timezone.utc):
                reasons.append("来源明确给出已过期截止日期。")
        except (ValueError, TypeError):
            reasons.append("截止日期格式无法核对，需人工确认。")
    return {"eligible": not reasons, "reasons": reasons, "graduation": graduation,
            "city_preferred": location["priority"] < 4, "city": location,
            "internship": job.recruitment_type == "实习", "related": related}


def job_quality(job, report):
    """只量化 JD 明写的实习内容；不把公司自称知名/融资宣传当质量证据。"""
    analysis = (report.get("jd_analysis") or {}).get("analysis") or {}
    duties = analysis.get("responsibilities", [])
    duty_text = "\n".join(duties)
    dimensions = (
        ("AI工程职责", r"(?:Agent|智能体|RAG|LLM|大模型|AI).*(?:开发|构建|搭建|实现|集成)|(?:开发|构建|搭建|实现).*(?:Agent|智能体|RAG|LLM|大模型|AI)", duty_text, 35),
        ("可交付实践", r"API|工具调用|数据处理|自动化|检索|工作流|系统集成|部署", duty_text, 25),
        ("工程质量", r"测试|评估|监控|优化|代码审查|code review", duty_text, 20),
        ("带教与成长", r"导师|带教|指导|技术分享|培训|mentorship|mentor", "\n".join(clauses(job.jd)), 20),
    )
    evidence, score = [], 0
    for label, pattern, text, points in dimensions:
        quotes = [line for line in text.splitlines() if re.search(pattern, line, re.I)
                  and not re.search(r"不(?:提供|安排|支持|参与)|没有|无导师|无带教|不涉及", line)]
        if quotes:
            score += points
            evidence.append({"dimension": label, "points": points, "evidence": quotes[:3]})
    title = job.title
    if re.search(r"AI\s*Agent|智能体", title, re.I):
        relevance = 100
    elif re.search(r"LLM|RAG|大模型", title, re.I):
        relevance = 90
    else:
        relevance = 70
    return {"score": score, "label": "JD内容较强" if score >= 75 else "JD内容一般或证据不足",
            "evidence": evidence, "company_quality": {"status": "待核验", "score": None,
            "reason": "没有独立核验依据；不从公司名、广告或岗位所在地推断公司质量，不计额外分。"},
            "direction_relevance": relevance, "direction_evidence": title,
            "note": "岗位质量分表示JD中职责和带教证据的完整度，不能保证实际团队质量。"}


def rank_job(job, report):
    matching = report["resume_match"]["matching"]
    scores = match_scores(matching["technical_matches"])
    required = scores["required"]
    overall = scores["overall"]["percent"]
    fit = candidate_filter(job)
    quality = job_quality(job, report)
    full = round(100 * required["matched"] / required["total"], 1) if required["total"] else None
    qualification_gaps = [x["requirement"] for x in [*matching["education"], *matching["experience"]] if x["status"] == "explicit_gap" and not x.get("group_id")]
    strong = (overall is not None and overall >= 80 and full is not None and full >= 70
              and required["unmatched"] == 0 and fit["internship"] and job.jd_kind == "full"
              and quality["score"] >= 75 and quality["direction_relevance"] >= 90
              and fit["graduation"]["points"] == 100 and not qualification_gaps)
    if qualification_gaps or overall is not None and (overall < 40 or required["total"] and required["points"] == 0):
        category = "当前差距较大"
    elif (overall is not None and overall >= 70 and full is not None and full >= 60
          and required["unmatched"] == 0 and fit["city_preferred"] and fit["internship"]
          and fit["graduation"]["points"] == 100 and job.jd_kind == "full"
          and (fit["city"]["priority"] == 1 or strong)):
        category = "高优先级"
    else:
        category = "可以尝试"
    components = {"综合匹配度": (overall or 0, .30), "必须技能完全覆盖率": (full or 0, .10),
                  "城市偏好": (fit["city"]["points"], .25), "岗位质量": (quality["score"], .20),
                  "AI方向相关度": (quality["direction_relevance"], .10),
                  "2027届明确适配": (fit["graduation"]["points"], .05)}
    contributions = {name: {"value": value, "weight": weight, "contribution": round(value * weight, 3)}
                     for name, (value, weight) in components.items()}
    override = fit["city"]["priority"] in (2, 3) and strong
    return {"category": category, "rank_score": round(sum(value * weight for value, weight in components.values()), 1),
            "overall_match": overall, "required_full_coverage": full, "required_counts": required,
            "city": fit["city"], "quality": quality, "city_override": {"eligible": override, "overtaken": [],
            "reason": "具备越级资格：综合≥80%、必须技能完全覆盖≥70%、无未匹配必须技能、岗位质量≥75、AI方向相关度≥90、明确2027届实习，且无明确资格差距；实际越过哪个广州岗位还要比较匹配度和排序分。" if override else
                      "维持城市偏好：尚未同时满足高度匹配、职责/成长证据、明确2027届等越级条件；不凭公司名或宣传加分。"},
            "graduation": fit["graduation"], "components": contributions,
            "reasons": [f"综合匹配度 {overall if overall is not None else '不适用'}%；必须项完全/部分/未匹配 {required['matched']}/{required['partial']}/{required['unmatched']}。",
                        "工作城市：" + fit["city"]["city"] + "；" + fit["city"]["label"] + "。" + fit["city"]["reason"],
                        f"岗位质量 {quality['score']}/100；AI方向相关度 {quality['direction_relevance']}/100；公司质量待核验。",
                        "招聘类型：" + job.recruitment_type + "；" + fit["graduation"]["status"] + "。",
                        "JD " + ("为原文。" if job.jd_kind == "full" else "为摘要，建议先核对原文。")]
                       + ["资格存在明确差距：" + text for text in qualification_gaps],
            "note": "排序只提供投递建议；未知条件不视为符合，分数不代表录用概率。"}


def sort_ranked(rows):
    """默认广州→上海→深圳；只允许有证据且明显更适配的低城市越过广州。"""
    order = {"高优先级": 0, "可以尝试": 1, "当前差距较大": 2}
    rows.sort(key=lambda row: (row["ranking"]["city"]["priority"], order[row["ranking"]["category"]],
                              -row["ranking"]["rank_score"], row["candidate"]["company"]))
    for row in list(rows):
        rank = row["ranking"]
        if not rank["city_override"]["eligible"]:
            continue
        current = rows.index(row)
        targets = [other for other in rows[:current] if other["ranking"]["city"]["priority"] < rank["city"]["priority"]
                   and (rank["overall_match"] or 0) - (other["ranking"]["overall_match"] or 0) >= 20
                   and rank["rank_score"] > other["ranking"]["rank_score"]]
        if targets:
            # 不越过更靠前、没有满足明显优势条件的岗位。
            insertion = max([rows.index(other) + 1 for other in rows[:current] if other not in targets] or [0])
            rows.remove(row)
            rows.insert(insertion, row)
            actual = [other for other in targets if rows.index(row) < rows.index(other)]
            rank["city_override"]["overtaken"] = [other["candidate_id"] for other in actual]
            if actual:
                rank["city_override"]["reason"] += " 已越过：" + "、".join(other["candidate"]["company"] + " / " + other["candidate"]["title"] for other in actual) + "；综合匹配度至少高20个百分点，含城市权重的排序分也更高。"
        if not rank["city_override"]["overtaken"]:
            rank["city_override"]["reason"] += " 本轮没有越过更高城市岗位：未形成至少20个百分点的匹配优势及更高排序分，或无可比较岗位。"
    return rows


class DiscoveryStore:
    """复用现有 JobMemory SQLite；新表不改原岗位、报告与 Skill 表结构。"""
    def __init__(self, memory):
        self.memory = memory
        self.db = memory.connection
        self.db.execute("CREATE TABLE IF NOT EXISTS discovered_jobs (candidate_id TEXT PRIMARY KEY, candidate_json TEXT NOT NULL, sources_json TEXT NOT NULL)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS discovery_analyses (
            candidate_id TEXT NOT NULL, context_key TEXT NOT NULL, state TEXT NOT NULL,
            report_json TEXT, PRIMARY KEY(candidate_id, context_key))""")
        self.db.commit()

    def remember(self, job):
        for candidate_id, data, sources in self.db.execute("SELECT candidate_id, candidate_json, sources_json FROM discovered_jobs"):
            original = JobCandidate(**json.loads(data))
            if similar_job(original, job):
                original = replace(original, valid_through=job.valid_through or original.valid_through,
                                   source_url=job.source_url or original.source_url, detail_url=job.detail_url or original.detail_url,
                                   parsing_evidence=job.parsing_evidence or original.parsing_evidence)
                aliases = json.loads(sources)
                alias = {"source": job.source, "url": job.url, "discovered_at": job.discovered_at,
                         "source_url": job.source_url, "detail_url": job.detail_url, "parsing_evidence": job.parsing_evidence}
                if not any(x["url"] == job.url for x in aliases):
                    aliases.append(alias)
                else:
                    for existing in aliases:
                        if existing["url"] == job.url:
                            existing["last_seen_at"] = job.discovered_at
                            for field in ("source_url", "detail_url", "parsing_evidence"):
                                if getattr(job, field) is not None:
                                    existing[field] = getattr(job, field)
                with self.db:
                    self.db.execute("UPDATE discovered_jobs SET sources_json=?, candidate_json=? WHERE candidate_id=?", (json.dumps(aliases, ensure_ascii=False), json.dumps(original.to_dict(), ensure_ascii=False), candidate_id))
                self.memory.library.remember_candidate(candidate_id, original)
                return candidate_id, original, True
        candidate_id = fingerprint(job.company + "\n" + job.title + "\n" + job.city + "\n" + job.recruitment_type + "\n" + job.jd)
        aliases = [{"source": job.source, "url": job.url, "discovered_at": job.discovered_at,
                         "source_url": job.source_url, "detail_url": job.detail_url, "parsing_evidence": job.parsing_evidence}]
        with self.db:
            self.db.execute("INSERT INTO discovered_jobs VALUES (?, ?, ?)",
                            (candidate_id, json.dumps(job.to_dict(), ensure_ascii=False), json.dumps(aliases, ensure_ascii=False)))
        self.memory.library.remember_candidate(candidate_id, job)
        return candidate_id, job, False

    def sources(self, candidate_id):
        return json.loads(self.db.execute("SELECT sources_json FROM discovered_jobs WHERE candidate_id=?", (candidate_id,)).fetchone()[0])

    def context_key(self, runtime, resume):
        return fingerprint(json.dumps({"resume": fingerprint(resume), "mode": runtime.requested_mode,
            "model": runtime.llm.config.model if runtime.llm else None, "schema": SCHEMA_VERSION,
            "local_rules": ANALYSIS_VERSION, "score_version": 2}, sort_keys=True))

    def existing(self, candidate_id, context_key):
        row = self.db.execute("SELECT state, report_json FROM discovery_analyses WHERE candidate_id=? AND context_key=?", (candidate_id, context_key)).fetchone()
        return (row[0], json.loads(row[1]) if row[1] else None) if row else None

    def claim(self, candidate_id, context_key):
        # 即使进程在付费请求中断，也不在下一次搜索静默重复付费。
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO discovery_analyses VALUES (?, ?, 'started', NULL)", (candidate_id, context_key))
        return cursor.rowcount == 1

    def finish(self, candidate_id, context_key, report, state="completed"):
        with self.db:
            self.db.execute("UPDATE discovery_analyses SET state=?, report_json=? WHERE candidate_id=? AND context_key=?",
                            (state, json.dumps(report, ensure_ascii=False) if report else None, candidate_id, context_key))


async def analyze_candidates(runtime, candidates, resume, max_analyses=2, search_only=False, city_min=2, guangzhou_only=False):
    if not 1 <= max_analyses <= 5:
        raise ValueError("本轮新分析数量限制为 1 到 5。")
    if not search_only and not resume:
        raise ValueError("自动分析需要 --resume；只搜索可用 --search-only。")
    store = DiscoveryStore(runtime.workspace.memory)
    context = store.context_key(runtime, resume) if resume else None
    unique, rejected, duplicates = {}, [], 0
    for job in candidates:
        identity, canonical, duplicate = store.remember(job)
        duplicates += int(duplicate)
        # 同一次搜索的多个来源只评估一次；保留全部来源。
        unique[identity] = canonical
    eligible = []
    for identity, job in unique.items():
        fit = candidate_filter(job)
        if not fit["eligible"]:
            rejected.append({"candidate": job.to_dict(), "reasons": fit["reasons"]})
        else:
            eligible.append((identity, job, fit))
    selection = select_cities([job for _, job, _ in eligible], city_min, guangzhou_only, limit=20)
    chosen = {id(job) for job in selection["selected"]}
    deferred = [{"candidate": job.to_dict(), "reason": "广州-only 已关闭上海/深圳扩展。" if guangzhou_only else "前序城市有效候选已足够，本轮不补充该城市。"}
                for job in selection["deferred"]]
    eligible = [row for row in eligible if id(row[1]) in chosen]
    eligible.sort(key=lambda x: (x[2]["city"]["priority"], not x[2]["internship"], -x[2]["graduation"]["points"], x[1].title))
    ranked, pending, attempted = [], [], 0
    for identity, job, fit in eligible:
        report, reused = None, False
        if search_only:
            pending.append({"candidate": job.to_dict(), "reason": "仅搜索，未调用分析或发送简历。"})
            continue
        cached = store.existing(identity, context)
        if cached:
            state, report = cached
            if state != "completed":
                pending.append({"candidate": job.to_dict(), "reason": "此前分析未完成或中断，本次不静默重试付费；可用 v2 单份入口人工核对。"})
                continue
            reused = True
        else:
            # 与 v2 同 JD、同简历、同模式的成功报告也可以直接复用。
            mode = "llm" if runtime.requested_mode == "llm" else "local_rules"
            model = runtime.llm.config.model if runtime.llm else None
            for previous in runtime.workspace.memory.list_reports(job.analysis_text()):
                match = previous.get("resume_match") or {}
                if (previous.get("mode") == mode and previous.get("model") == model
                    and previous.get("schema_version") == SCHEMA_VERSION and previous.get("skill_gaps") is not None
                    and (previous.get("mode") == "llm" or previous.get("jd_analysis", {}).get("analysis", {}).get("analysis_version") == ANALYSIS_VERSION)
                    and match.get("matching", {}).get("resume_fingerprint") == fingerprint(resume)):
                    report, reused = previous, True
                    break
            if report is None and attempted >= max_analyses:
                pending.append({"candidate": job.to_dict(), "reason": "达到本轮新分析数量上限，尚未评分。"})
                continue
            if not store.claim(identity, context):
                pending.append({"candidate": job.to_dict(), "reason": "该岗位已有分析执行记录，跳过并发重复。"})
                continue
            if report is None:
                attempted += 1
                report = await runtime.analyze(job.analysis_text(), resume, "all", job.source)
            if report is None:
                store.finish(identity, context, None, "incomplete")
                pending.append({"candidate": job.to_dict(), "reason": "分析未完成或未确认；没有生成匹配分数，未自动重试。"})
                continue
            runtime.workspace.memory.save_report(job.analysis_text(), resume, "all", report)
            store.finish(identity, context, report)
        ranking = rank_job(job, report)
        job_id = runtime.workspace.memory.library.linked("candidate", identity)
        ranked.append({"candidate_id": identity, "job_id": job_id, "candidate": job.to_dict(), "sources": store.sources(identity),
                       "reused_analysis": reused, "report": report, "ranking": ranking})
    sort_ranked(ranked)
    for row in ranked:
        runtime.workspace.memory.library.record_result(row["job_id"], fingerprint(JobCandidate(**row["candidate"]).analysis_text()),
                                                      row["report"], row["ranking"])
    return {"version": 3, "created_at": utc_now(), "ranked": ranked, "pending": pending, "rejected": rejected,
            "deferred": deferred, "city_policy": selection["policy"],
            "duplicates": duplicates, "new_analyses": attempted, "unique_candidates": len(unique)}


def print_priority_list(result):
    print("\nJob Agent v3 优先投递列表（未自动投递）")
    print(f"候选去重后 {result['unique_candidates']}；重复来源/岗位 {result['duplicates']}；本轮新分析 {result['new_analyses']}。")
    policy = result.get("city_policy", {})
    print("城市策略：广州 >>> 上海 > 深圳；扩展阈值 " + str(policy.get("enough", 2)) + " 个有效候选；阶段 " + " → ".join(policy.get("search_stages", policy.get("stages", []))))
    print("推荐等级：高优先级 / 可以尝试 / 当前差距较大；以下按最终顺序展示。")
    if not result["ranked"]:
        print("  暂无已分析推荐。")
    for index, row in enumerate(result["ranked"], 1):
        job, rank = row["candidate"], row["ranking"]
        print(f"  {index}. {job['company']} / {job['title']} / {rank['city']['city']} / {job['recruitment_type']} / {rank['category']}")
        if row.get("job_id"):
            print(f"     岗位库编号 #{row['job_id']}；用 /job {row['job_id']} 查看、/status {row['job_id']} shortlisted 登记准备投递。")
        print(f"     综合匹配度 {rank['overall_match'] if rank['overall_match'] is not None else '不适用'}%；排序分 {rank['rank_score']}；{'复用分析' if row['reused_analysis'] else '本轮分析'}。")
        for reason in rank["reasons"]:
            print("     " + reason)
        print("     城市越级判断：" + rank["city_override"]["reason"])
        for evidence in rank["quality"]["evidence"]:
            print("     岗位质量证据：" + evidence["dimension"] + " / " + "；".join(evidence["evidence"]))
        print("     来源：" + job["source"] + "；发现时间：" + job["discovered_at"])
        print("     原始链接：" + job["url"])
        if job.get("source_url"):
            print("     来源页面：" + job["source_url"])
        if job.get("detail_url"):
            print("     岗位详情：" + job["detail_url"])
        if job.get("parsing_evidence"):
            print("     解析依据：" + json.dumps(job["parsing_evidence"], ensure_ascii=False))
        for source in row["sources"][1:]:
            print("     其他来源：" + source["source"] + " " + source["url"])
        for item in row["report"]["resume_match"]["matching"]["technical_matches"]:
            print("     技能证据：" + item["skill"] + " / " + item["status"] + " / " + ("；".join(item["resume_evidence"]) or "未找到"))
            if item.get("reason"):
                print("       " + item["reason"])
    for key, label in (("pending", "尚未分析"), ("rejected", "本地筛除"), ("deferred", "未启用城市/暂不补充")):
        print("\n" + label + "：")
        for row in result.get(key, []):
            print("  - " + row["candidate"]["title"] + "：" + (row.get("reason") or "；".join(row["reasons"])))
    for issue in result.get("search_issues", []):
        print("来源提示：" + issue["source"] + "：" + issue["reason"])
    print("列表只提供投递建议；2027届、学历和实习要求仍需核对原文；没有自动投递。")
