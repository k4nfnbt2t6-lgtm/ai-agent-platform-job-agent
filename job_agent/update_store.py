"""每日计划与检查摘要：复用 JobMemory 连接，不保存简历全文。"""

import json
import re
from datetime import datetime, timedelta, timezone

CHINA = timezone(timedelta(hours=8))


def utc_clock():
    return datetime.now(timezone.utc)


def stamp(moment):
    return moment.astimezone(timezone.utc).isoformat()


def daily_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("每日时间需要 HH:MM，例如 09:00；按北京时间。")
    return value


def next_daily(moment, at):
    hour, minute = map(int, daily_time(at).split(":"))
    local = moment.astimezone(CHINA)
    result = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if result <= local:
        result += timedelta(days=1)
    return result.astimezone(timezone.utc)


class UpdateStore:
    def __init__(self, memory):
        self.memory, self.db = memory, memory.connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS job_daily_schedule (
            id INTEGER PRIMARY KEY CHECK(id=1), daily_at TEXT, next_run_at TEXT,
            last_found_at TEXT
        )""")
        self.db.execute("INSERT OR IGNORE INTO job_daily_schedule (id) VALUES (1)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS job_update_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            summary_json TEXT
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS job_update_items (
            run_id INTEGER NOT NULL, job_id INTEGER NOT NULL, kind TEXT NOT NULL,
            found_at TEXT NOT NULL,
            PRIMARY KEY(run_id, job_id)
        )""")
        if "found_at" not in {row[1] for row in self.db.execute("PRAGMA table_info(job_update_items)")}:
            self.db.execute("ALTER TABLE job_update_items ADD COLUMN found_at TEXT")
            self.db.execute("""UPDATE job_update_items SET found_at=(SELECT started_at
                FROM job_update_runs WHERE job_update_runs.run_id=job_update_items.run_id) WHERE found_at IS NULL""")
        self.db.commit()

    def schedule(self):
        row = self.db.execute("SELECT daily_at, next_run_at, last_found_at FROM job_daily_schedule WHERE id=1").fetchone()
        return dict(zip(("daily_at", "next_run_at", "last_found_at"), row))

    def configure(self, at, moment):
        daily_time(at)
        previous = self.schedule()
        # 同一时间重启保留到期点；只有用户改变时间才重新计算。
        if previous["daily_at"] != at or previous["next_run_at"] is None:
            with self.db:
                self.db.execute("UPDATE job_daily_schedule SET daily_at=?, next_run_at=? WHERE id=1",
                                (at, next_daily(moment, at).isoformat()))
        return self.schedule()

    def claim_due(self, moment):
        saved = self.schedule()
        due = saved["next_run_at"]
        if not due or datetime.fromisoformat(due) > moment:
            return False
        with self.db:
            cursor = self.db.execute("UPDATE job_daily_schedule SET next_run_at=? WHERE id=1 AND next_run_at=?",
                                    (next_daily(moment, saved["daily_at"]).isoformat(), due))
        return cursor.rowcount == 1

    def begin(self, origin, moment):
        with self.db:
            cursor = self.db.execute("INSERT INTO job_update_runs (origin, started_at, status) VALUES (?, ?, 'running')",
                                     (origin, stamp(moment)))
        return cursor.lastrowid

    def add_item(self, run_id, job_id, kind, moment):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO job_update_items (run_id, job_id, kind, found_at) VALUES (?, ?, ?, ?)",
                            (run_id, job_id, kind, stamp(moment)))
            if kind == "new":
                self.db.execute("UPDATE job_daily_schedule SET last_found_at=? WHERE id=1", (stamp(moment),))

    def rows(self, run_id):
        return [self.job_row(identity, kind) for identity, kind in self.db.execute(
            "SELECT job_id, kind FROM job_update_items WHERE run_id=? ORDER BY job_id", (run_id,))]

    def job_row(self, identity, kind):
        job = self.memory.library.get(identity)
        return {"job_id": identity, "kind": kind, "company": job["company"], "title": job["title"],
                "city": job["city"], "overall_match": job["overall_match"], "recommendation": job["recommendation"],
                "url": job["detail_url"] or job["source_url"], "status": job["status"],
                "analyzed": job["ranking"] is not None or job["overall_match"] is not None}

    def finish(self, run_id, moment, summary, status):
        with self.db:
            self.db.execute("UPDATE job_update_runs SET finished_at=?, status=?, summary_json=? WHERE run_id=?",
                            (stamp(moment), status, json.dumps(summary, ensure_ascii=False), run_id))

    def last_check(self):
        row = self.db.execute("SELECT run_id, origin, started_at, finished_at, status, summary_json FROM job_update_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        if not row:
            return None
        result = dict(zip(("run_id", "origin", "started_at", "finished_at", "status", "summary_json"), row))
        raw = result.pop("summary_json")
        result["summary"] = json.loads(raw) if raw else None
        return result

    def today_jobs(self, moment):
        local = moment.astimezone(CHINA)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        ids = self.db.execute("""SELECT DISTINCT job_id FROM job_update_items
            WHERE kind='new' AND found_at>=? AND found_at<? ORDER BY job_id""",
            (start.isoformat(), end.isoformat()))
        return [self.job_row(row[0], "new") for row in ids]

    def recover_interrupted(self, moment):
        # 调用者已持有状态目录的排他锁，此时旧 running 不可能是另一进程正在检查。
        for (run_id,) in self.db.execute("SELECT run_id FROM job_update_runs WHERE status='running'").fetchall():
            summary = make_summary(self.rows(run_id), issues=[{"reason": "上次检查被中断；已保留岗位和进度，没有连续重试。"}])
            self.finish(run_id, moment, summary, "interrupted")


def make_summary(rows, *, analyzed=0, unchanged=0, issues=(), http_requests=0, city_policy=None):
    counts = {label: sum(row["recommendation"] == label for row in rows)
              for label in ("高优先级", "可以尝试", "当前差距较大")}
    return {"mode": "local", "estimated_rmb": 0, "new_count": sum(row["kind"] == "new" for row in rows),
            "updated_count": sum(row["kind"] == "updated" for row in rows),
            "analyzed_count": analyzed, "unchanged_count": unchanged,
            "pending_count": sum(not row["analyzed"] for row in rows),
            "recommendations": counts, "jobs": rows, "issues": list(issues),
            "http_requests": http_requests, "city_policy": city_policy}


def print_update(summary):
    print(f"本次新增岗位：{summary['new_count']}；JD 更新：{summary['updated_count']}；新分析：{summary['analyzed_count']}；未变化跳过：{summary['unchanged_count']}。")
    print("推荐统计（本次新增/更新/补齐分析的岗位）：")
    for label, count in summary["recommendations"].items():
        print(f"  {label}：{count}")
    if summary["pending_count"]:
        print(f"  待分析：{summary['pending_count']}（未评分不当作低匹配，后续检查在额度内补齐）。")
    for row in summary["jobs"]:
        percent = f"{row['overall_match']:.1f}%" if row["overall_match"] is not None else "不适用（未识别技术要求）" if row["analyzed"] else "待分析"
        print(f"  #{row['job_id']} | {row['company'] or '公司未说明'} | {row['title'] or '岗位未说明'} | {row['city']} | {percent} | {row['recommendation'] or '尚未推荐'}")
        print("    原始链接：" + (row["url"] or "未提供"))
    for issue in summary["issues"]:
        print("  来源/检查提示：" + issue["reason"])
    print("分析模式 local；模型请求 0；模型费用 0 元。")
