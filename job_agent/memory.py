"""Job Agent 的业务记忆：本地岗位结论和内容去重，不保存完整简历。"""

import json
import sqlite3
from datetime import datetime, timezone

from .analysis import ANALYSIS_VERSION, fingerprint
from .schemas import SCHEMA_VERSION
from .job_library import JobLibrary


class JobMemory:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
            jd_id TEXT PRIMARY KEY, company TEXT, title TEXT,
            report_json TEXT NOT NULL, source_label TEXT NOT NULL,
            analyzed_at TEXT NOT NULL, analysis_version INTEGER NOT NULL DEFAULT 1
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS llm_jd_analyses (
            jd_id TEXT NOT NULL, model TEXT NOT NULL, schema_version INTEGER NOT NULL,
            report_json TEXT NOT NULL, PRIMARY KEY (jd_id, model, schema_version)
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS job_reports (
            jd_id TEXT NOT NULL, resume_hash TEXT NOT NULL, mode TEXT NOT NULL,
            model TEXT NOT NULL, workflow TEXT NOT NULL, schema_version INTEGER NOT NULL,
            report_json TEXT NOT NULL, analyzed_at TEXT NOT NULL,
            PRIMARY KEY (jd_id, resume_hash, mode, model, workflow, schema_version)
        )""")
        self.connection.commit()
        self.library = JobLibrary(self.connection)
        self.library.migrate_legacy()

    def get(self, jd_text):
        jd_id = fingerprint(jd_text)
        row = self.connection.execute("SELECT report_json FROM jobs WHERE jd_id = ? AND analysis_version = ?", (jd_id, ANALYSIS_VERSION)).fetchone()
        return {"jd_id": jd_id, "analysis": json.loads(row[0])} if row else None

    def remember(self, jd_text, report, source_label):
        jd_id = fingerprint(jd_text)
        with self.connection:
            existing = self.connection.execute("SELECT analysis_version FROM jobs WHERE jd_id=?", (jd_id,)).fetchone()
            self.connection.execute("""INSERT INTO jobs
                (jd_id, company, title, report_json, source_label, analyzed_at, analysis_version)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(jd_id) DO UPDATE SET company=excluded.company, title=excluded.title,
                    report_json=excluded.report_json, analysis_version=excluded.analysis_version
                WHERE jobs.analysis_version != excluded.analysis_version""", (jd_id, report["company"], report["title"],
                    json.dumps(report, ensure_ascii=False), source_label, datetime.now(timezone.utc).isoformat(), ANALYSIS_VERSION))
        self.library.remember_analysis(jd_text, report, source_label)
        return {"jd_id": jd_id, "new_record": existing is None,
                "refreshed": existing is not None and existing[0] != ANALYSIS_VERSION}

    def list_jobs(self):
        return [{"jd_id": row[0], "company": row[1], "title": row[2], "source": row[3], "analyzed_at": row[4]}
                for row in self.connection.execute("SELECT jd_id, company, title, source_label, analyzed_at FROM jobs ORDER BY analyzed_at DESC")]

    def get_llm(self, jd_text, model):
        jd_id = fingerprint(jd_text)
        row = self.connection.execute("SELECT report_json FROM llm_jd_analyses WHERE jd_id=? AND model=? AND schema_version=?",
                                      (jd_id, model, SCHEMA_VERSION)).fetchone()
        return {"jd_id": jd_id, "analysis": json.loads(row[0])} if row else None

    def remember_llm(self, jd_text, report, source_label, model):
        jd_id = fingerprint(jd_text)
        with self.connection:
            exists = self.connection.execute("SELECT 1 FROM jobs WHERE jd_id=?", (jd_id,)).fetchone()
            # 0 表示此行没有本地规则结果，local 的 get() 不会错用 LLM 缓存。
            self.connection.execute("""INSERT OR IGNORE INTO jobs
                (jd_id, company, title, report_json, source_label, analyzed_at, analysis_version)
                VALUES (?, ?, ?, ?, ?, ?, 0)""", (jd_id, report["company"], report["title"],
                    json.dumps(report, ensure_ascii=False), source_label, datetime.now(timezone.utc).isoformat()))
            self.connection.execute("INSERT OR REPLACE INTO llm_jd_analyses VALUES (?, ?, ?, ?)",
                                    (jd_id, model, SCHEMA_VERSION, json.dumps(report, ensure_ascii=False)))
        self.library.remember_analysis(jd_text, report, source_label)
        return {"jd_id": jd_id, "new_record": exists is None, "refreshed": False}

    def save_report(self, jd_text, resume, workflow, report):
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO job_reports VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fingerprint(jd_text), fingerprint(resume) if resume else "", report["mode"], report.get("model") or "",
                 workflow, SCHEMA_VERSION, json.dumps(report, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))

    def list_reports(self, jd_text):
        return [json.loads(row[0]) for row in self.connection.execute(
            "SELECT report_json FROM job_reports WHERE jd_id=? ORDER BY analyzed_at", (fingerprint(jd_text),))]

    def close(self):
        self.connection.close()
