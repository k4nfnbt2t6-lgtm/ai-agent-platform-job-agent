"""v4 岗位库：与分析缓存共用 SQLite，求职状态只接受用户 CLI 操作。"""

import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace

from agent_platform.workspace import contains_credentials
from .analysis import fingerprint, match_scores, validate_text
from .city_policy import city_fit


JOB_STATUSES = {
    "discovered": "已发现", "shortlisted": "准备投递", "applied": "已投递",
    "written_test": "笔试", "interview": "面试", "rejected": "被拒",
    "offer": "Offer", "withdrawn": "主动放弃",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def check_status(status):
    if status not in JOB_STATUSES:
        raise ValueError("无效投递状态；可用：" + ", ".join(JOB_STATUSES))
    return status


class JobLibrary:
    def __init__(self, connection):
        self.db = connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS job_library (
            job_id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT, title TEXT, city TEXT NOT NULL, recruitment_type TEXT NOT NULL,
            jd TEXT NOT NULL, jd_hash TEXT, source TEXT NOT NULL, source_url TEXT,
            detail_url TEXT, jd_kind TEXT NOT NULL,
            overall_match REAL, recommendation TEXT, ranking_json TEXT,
            status TEXT NOT NULL DEFAULT 'discovered' CHECK (status IN
                ('discovered','shortlisted','applied','written_test','interview','rejected','offer','withdrawn')),
            first_discovered_at TEXT NOT NULL, applied_at TEXT,
            status_updated_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
            info_updated_at TEXT NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS job_library_links (
            kind TEXT NOT NULL, external_id TEXT NOT NULL, job_id INTEGER NOT NULL,
            PRIMARY KEY(kind, external_id), FOREIGN KEY(job_id) REFERENCES job_library(job_id)
        )""")
        self.db.execute("CREATE TABLE IF NOT EXISTS job_library_migrations (version INTEGER PRIMARY KEY)")
        self.db.commit()

    def linked(self, kind, external_id):
        row = self.db.execute("SELECT job_id FROM job_library_links WHERE kind=? AND external_id=?",
                              (kind, external_id)).fetchone()
        return row[0] if row else None

    def _link(self, kind, external_id, job_id):
        self.db.execute("INSERT OR IGNORE INTO job_library_links VALUES (?, ?, ?)", (kind, external_id, job_id))

    def get(self, job_id):
        if not str(job_id).isascii() or not str(job_id).isdigit() or len(str(job_id)) > 18 or int(job_id) < 1:
            raise ValueError("岗位编号需要是 /jobs 中显示的正整数。")
        cursor = self.db.execute("SELECT * FROM job_library WHERE job_id=?", (int(job_id),))
        row = cursor.fetchone()
        if row is None:
            raise ValueError("未找到岗位 #" + str(job_id) + "；请先用 /jobs 查看编号。")
        result = dict(zip((field[0] for field in cursor.description), row))
        ranking = result.pop("ranking_json")
        result["ranking"] = json.loads(ranking) if ranking else None
        return result

    def list(self, status=None):
        if status is not None:
            check_status(status)
        ids = self.db.execute("SELECT job_id FROM job_library" + (" WHERE status=?" if status else "")
                              + " ORDER BY job_id", (status,) if status else ())
        return [self.get(row[0]) for row in ids]

    def _upsert(self, data, links=()):
        # JD 哈希连接搜索与手动分析；稳定公司/岗位/城市/类型连接 JD 的不同版本。
        identity = next((found for kind, key in links if (found := self.linked(kind, key))), None)
        if identity is None and data["company"] and data["title"] and data["company"] != "未知公司":
            rows = self.db.execute("""SELECT job_id, source_url, detail_url FROM job_library
                WHERE company=? AND title=? AND city=? AND recruitment_type=?""",
                tuple(data[key] for key in ("company", "title", "city", "recruitment_type"))).fetchall()
            # 不把不同详情链接的同名岗位盲目合并；无链接的用户材料按公司/岗位/城市识别。
            compatible = [row[0] for row in rows if not data.get("detail_url") or not row[2]
                          or row[2] == data["detail_url"]]
            if len(compatible) == 1:
                identity = compatible[0]
        timestamp = now()
        with self.db:
            if identity is None:
                first = data.get("first_discovered_at") or timestamp
                columns = list(data) + ["status_updated_at", "info_updated_at"]
                cursor = self.db.execute("INSERT INTO job_library (" + ",".join(columns) + ") VALUES ("
                    + ",".join("?" for _ in columns) + ")", [*data.values(), first, timestamp])
                identity = cursor.lastrowid
            else:
                previous = self.get(identity)
                # 老报告/重复来源不能改首次发现时间、状态、投递时间和备注。
                fields = {key: value for key, value in data.items() if key != "first_discovered_at"}
                if previous["jd_hash"] != data["jd_hash"]:
                    fields.update(overall_match=None, recommendation=None, ranking_json=None)
                fields["info_updated_at"] = timestamp
                self.db.execute("UPDATE job_library SET " + ",".join(key + "=?" for key in fields)
                                + " WHERE job_id=?", [*fields.values(), identity])
            for kind, key in links:
                self._link(kind, key, identity)
        return identity

    def remember_candidate(self, candidate_id, job):
        location = city_fit(job)["city"]
        data = dict(company=job.company, title=job.title, city=location,
                    recruitment_type=job.recruitment_type, jd=job.jd,
                    jd_hash=fingerprint(job.analysis_text()), source=job.source,
                    source_url=job.source_url or job.url, detail_url=job.detail_url,
                    jd_kind=job.jd_kind, first_discovered_at=job.discovered_at)
        # 相似来源由 v3 的 candidate_id 负责归并，仍保留 v3 的版本化分析缓存。
        return self._upsert(data, (("candidate", candidate_id), ("jd", data["jd_hash"])))

    def remember_analysis(self, jd, analysis, source, report=None, jd_id=None, discovered_at=None):
        jd_id = jd_id or fingerprint(jd)
        identity = self.linked("jd", jd_id)
        if identity is None:
            city = city_fit(SimpleNamespace(jd=jd, city="工作地点待确认"))["city"]
            kind = "实习" if re.search(r"实习|intern", jd, re.I) else "未说明"
            url = source if source.startswith("https://") and not contains_credentials(source) else None
            identity = self._upsert(dict(company=analysis.get("company"), title=analysis.get("title"),
                city=city, recruitment_type=kind, jd=jd, jd_hash=jd_id, source=source,
                source_url=url, detail_url=None, jd_kind="full" if jd else "legacy_missing",
                first_discovered_at=discovered_at or now()), (("jd", jd_id),))
        elif self.get(identity)["jd_hash"] == jd_id and jd and not self.get(identity)["jd"]:
            old = self.get(identity)
            self._upsert(dict(company=analysis.get("company"), title=analysis.get("title"),
                city=city_fit(SimpleNamespace(jd=jd, city=old["city"]))["city"],
                recruitment_type="实习" if re.search(r"实习|intern", jd, re.I) else old["recruitment_type"],
                jd=jd, jd_hash=jd_id, source=old["source"], source_url=old["source_url"],
                detail_url=old["detail_url"], jd_kind="full", first_discovered_at=old["first_discovered_at"]),
                (("jd", jd_id),))
        if report:
            self.record_result(identity, jd_id, report)
        return identity

    def record_result(self, job_id, jd_hash, report, ranking=None):
        # 对旧 JD 的分析只能进缓存，不覆盖此岗位的新版本分数。
        if self.get(job_id)["jd_hash"] != jd_hash:
            return
        matching = report.get("resume_match")
        if not matching:
            return
        score = match_scores(matching["matching"]["technical_matches"])["overall"]["percent"]
        if ranking is None and self.get(job_id)["jd"]:
            from .discovery import rank_job
            job = self.get(job_id)
            ranking = rank_job(SimpleNamespace(**{**job, "valid_through": None, "company": job["company"] or "未知公司",
                                                   "title": job["title"] or "岗位未说明"}), report)
        recommendation = ranking["category"] if ranking else "待排序（单份分析）"
        with self.db:
            self.db.execute("UPDATE job_library SET overall_match=?, recommendation=?, ranking_json=? WHERE job_id=?",
                            (score, recommendation, json.dumps(ranking, ensure_ascii=False) if ranking else None, job_id))

    def set_status(self, job_id, status):
        check_status(status)
        job = self.get(job_id)
        if job["status"] != status:
            timestamp = now()
            applied = job["applied_at"] or (timestamp if status == "applied" else None)
            with self.db:
                self.db.execute("UPDATE job_library SET status=?, applied_at=?, status_updated_at=? WHERE job_id=?",
                                (status, applied, timestamp, job["job_id"]))
        return self.get(job_id)

    def add_note(self, job_id, note):
        job = self.get(job_id)
        validate_text(note, "备注")
        note = note.strip()
        if contains_credentials(note):
            raise ValueError("备注疑似包含凭据，未保存。")
        notes = "\n".join(filter(None, (job["notes"], note)))
        validate_text(notes, "累计备注")
        with self.db:
            self.db.execute("UPDATE job_library SET notes=? WHERE job_id=?", (notes, job["job_id"]))
        return self.get(job_id)

    def funnel(self):
        counts = {status: 0 for status in JOB_STATUSES}
        counts.update(dict(self.db.execute("SELECT status, COUNT(*) FROM job_library GROUP BY status")))
        return {"total": sum(counts.values()), "counts": counts}

    def migrate_legacy(self):
        """只导入一次已有记录；不删除或修改 v1-v3 的缓存和报告。"""
        if self.db.execute("SELECT 1 FROM job_library_migrations WHERE version=4").fetchone():
            return
        from .search import JobCandidate
        from .discovery import rank_job
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with self.db:
            if "discovered_jobs" in tables:
                candidates = self.db.execute("SELECT candidate_id, candidate_json FROM discovered_jobs").fetchall()
                candidates.sort(key=lambda row: json.loads(row[1])["discovered_at"])
                for candidate_id, raw in candidates:
                    job = JobCandidate(**json.loads(raw))
                    self.remember_candidate(candidate_id, job)
            for jd_id, raw, source, timestamp in self.db.execute(
                    "SELECT jd_id, report_json, source_label, analyzed_at FROM jobs ORDER BY analyzed_at").fetchall():
                analysis = json.loads(raw)
                if not self.linked("jd", jd_id):
                    # 旧手动分析没有保存 JD 原文，明确标记缺失，不伪造原文。
                    self.remember_analysis("", analysis, source, jd_id=jd_id, discovered_at=timestamp)
            for jd_id, raw in self.db.execute("SELECT jd_id, report_json FROM job_reports ORDER BY analyzed_at").fetchall():
                identity = self.linked("jd", jd_id)
                if identity:
                    self.record_result(identity, jd_id, json.loads(raw))
            if "discovery_analyses" in tables:
                for candidate_id, raw in self.db.execute(
                        "SELECT candidate_id, report_json FROM discovery_analyses WHERE state='completed' AND report_json IS NOT NULL").fetchall():
                    row = self.db.execute("SELECT candidate_json FROM discovered_jobs WHERE candidate_id=?", (candidate_id,)).fetchone()
                    if row:
                        job = JobCandidate(**json.loads(row[0]))
                        self.record_result(self.linked("candidate", candidate_id), fingerprint(job.analysis_text()),
                                           json.loads(raw), rank_job(job, json.loads(raw)))
            self.db.execute("INSERT INTO job_library_migrations VALUES (4)")
