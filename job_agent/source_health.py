"""公开来源的核验结果，复用现有岗位 SQLite 连接；页面可读不等于接入成功。"""

import json


def source_check(source, jobs, pages, failures, checked_at):
    from .discovery import candidate_filter, similar_job
    from .city_policy import city_fit

    unique = []
    for job in jobs:
        if not any(similar_job(job, old) for old in unique):
            unique.append(job)
    eligible = [job for job in unique if candidate_filter(job)["eligible"]]
    expired_notice = any("来源明确标记信息已过期" in reason for reason in failures)
    status = "verified" if eligible else "parsed_no_eligible" if unique else "no_open_jobs" if expired_notice else "readable_parse_failed" if pages else "fetch_failed"
    return {"name": source["name"], "url": source["url"], "search_city": source.get("search_city", "广州"),
            "supported_parsers": source.get("supported_parsers", [source["kind"]]),
            "actual_parsers": sorted({(job.parsing_evidence or {}).get("method", "unknown") for job in unique}),
            "status": status, "verified_real_search": bool(eligible), "checked_at": checked_at,
            "last_success_at": checked_at if eligible else None, "readable_pages": list(pages),
            "parsed_count": len(unique), "eligible_count": len(eligible),
            "eligible_cities": {city: sum(city_fit(job)["city"] == city for job in eligible) for city in ("广州", "上海", "深圳")},
            "failure_reasons": list(dict.fromkeys(failures)),
            "excluded": [{"title": job.title, "url": job.url, "reasons": candidate_filter(job)["reasons"]}
                         for job in unique if not candidate_filter(job)["eligible"]],
            "job_evidence": [{"title": job.title, "url": job.url, "source_url": job.source_url,
                              "city": city_fit(job), "recruitment_type": job.recruitment_type,
                              "valid_through": job.valid_through, "parsing_evidence": job.parsing_evidence}
                             for job in eligible],
            "availability_note": "成功仅表示自动取得可筛选的JD；未标截止日期/届别/实习类型不等于已确认，投递前复核招聘状态。"}


class SourceHealthStore:
    def __init__(self, memory):
        self.db = memory.connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS job_source_checks (
            source_url TEXT PRIMARY KEY, last_success_at TEXT, check_json TEXT NOT NULL
        )""")
        self.db.commit()

    def record(self, checks):
        with self.db:
            for check in checks:
                if check["status"] == "not_checked":
                    continue
                previous = self.db.execute("SELECT last_success_at FROM job_source_checks WHERE source_url=?", (check["url"],)).fetchone()
                saved = {**check, "last_success_at": check["last_success_at"] or (previous[0] if previous else None)}
                self.db.execute("""INSERT INTO job_source_checks VALUES (?, ?, ?)
                    ON CONFLICT(source_url) DO UPDATE SET last_success_at=excluded.last_success_at,
                    check_json=excluded.check_json""", (check["url"], saved["last_success_at"], json.dumps(saved, ensure_ascii=False)))

    def list(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT check_json FROM job_source_checks ORDER BY source_url")]


def print_source_checks(checks):
    labels = {"verified": "自动搜索通过", "parsed_no_eligible": "已解析，但无合格候选",
              "no_open_jobs": "来源明确显示岗位已过期", "readable_parse_failed": "来源可读但解析失败",
              "fetch_failed": "访问失败", "not_checked": "本轮未访问（城市/数量策略）"}
    for check in checks:
        print(f"来源核验：{check['name']} | {labels[check['status']]} | 可筛选岗位 {check.get('eligible_count', 0)}")
        if check.get("availability_note"):
            print("  " + check["availability_note"])
