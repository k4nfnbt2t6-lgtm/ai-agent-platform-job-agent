"""与 jobs.sqlite 共用连接；预算、请求意图和投递意图先落盘再执行。"""

import json
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_CEILING
from uuid import uuid4


class GuardError(ValueError):
    pass


class BudgetExhausted(GuardError):
    pass


class UnresolvedCost(GuardError):
    pass


class EmergencyStopped(GuardError):
    pass


def micro(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise GuardError("费用必须为非负有限数值。")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


class AutomationStore:
    def __init__(self, memory, clock=None):
        self.memory, self.db = memory, memory.connection
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS auto_control (id INTEGER PRIMARY KEY CHECK(id=1), stopped INTEGER NOT NULL);
        INSERT OR IGNORE INTO auto_control VALUES (1,0);
        CREATE TABLE IF NOT EXISTS auto_plans (
            id TEXT PRIMARY KEY, day TEXT NOT NULL, work_key TEXT UNIQUE NOT NULL,
            state TEXT NOT NULL, reserved_micro INTEGER NOT NULL, slots INTEGER NOT NULL,
            spent_micro INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS auto_requests (
            id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, state TEXT NOT NULL,
            input_tokens INTEGER, output_tokens INTEGER, cost_micro INTEGER,
            error_kind TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS auto_applications (
            id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, mode TEXT NOT NULL, adapter TEXT NOT NULL,
            packet_hash TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT, error_kind TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, day TEXT NOT NULL,
            UNIQUE(job_id, mode));
        CREATE TABLE IF NOT EXISTS auto_runs (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, summary_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS auto_packets (
            packet_hash TEXT PRIMARY KEY, job_id INTEGER NOT NULL, state TEXT NOT NULL);
        """)
        self.db.commit()

    def stamp(self):
        now = self.clock()
        if now.tzinfo is None:
            raise GuardError("自动化时钟需要时区。")
        return now.isoformat()

    def day(self):
        return self.clock().astimezone(timezone(timedelta(hours=8))).date().isoformat()

    @contextmanager
    def atomic(self):
        if self.db.in_transaction:
            raise GuardError("不能嵌套自动化事务。")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def stopped(self):
        return bool(self.db.execute("SELECT stopped FROM auto_control WHERE id=1").fetchone()[0])

    def check_running(self):
        if self.stopped():
            raise EmergencyStopped("紧急停止已生效；不会发出新的模型或申请请求。")

    def stop(self):
        with self.atomic():
            self.db.execute("UPDATE auto_control SET stopped=1 WHERE id=1")

    def resume(self, *, confirmed=False):
        if confirmed is not True:
            raise GuardError("恢复自动化需要明确确认。")
        with self.atomic():
            self.db.execute("UPDATE auto_control SET stopped=0 WHERE id=1")

    def reserve_plan(self, work_key, estimate_rmb, calls, limits):
        amount = micro(estimate_rmb)
        if type(calls) is not int or calls < 1:
            raise GuardError("请求预留次数无效。")
        with self.atomic():
            self.check_running()
            # 费用未知跨日也不自行解除；需要核对账单后人工处理。
            if (self.db.execute("SELECT 1 FROM auto_plans WHERE state='unknown'").fetchone()
                or self.db.execute("SELECT 1 FROM auto_requests WHERE state IN ('started','unknown')").fetchone()):
                raise UnresolvedCost("存在费用不明请求，暂停新的自动付费计划。")
            total, slots = self.db.execute("SELECT COALESCE(SUM(reserved_micro),0), COALESCE(SUM(slots),0) FROM auto_plans WHERE day=?", (self.day(),)).fetchone()
            if total + amount > micro(limits.daily_rmb) or slots + calls > limits.daily_requests:
                raise BudgetExhausted("每日额度不足以完成完整分析；不降低输出质量或超额调用。")
            identity = uuid4().hex
            try:
                self.db.execute("INSERT INTO auto_plans (id,day,work_key,state,reserved_micro,slots,created_at) VALUES (?,?,?,'reserved',?,?,?)",
                                (identity, self.day(), work_key, amount, calls, self.stamp()))
            except Exception as error:
                import sqlite3
                if isinstance(error, sqlite3.IntegrityError):
                    raise GuardError("此分析已有持久化计划，不自动重复请求。") from None
                raise
        return identity

    def begin_request(self, plan_id, estimate_rmb, limits):
        with self.atomic():
            self.check_running()
            row = self.db.execute("SELECT day,state,reserved_micro,slots,spent_micro,calls FROM auto_plans WHERE id=?", (plan_id,)).fetchone()
            if not row or row[0] != self.day() or row[1] != "reserved" or row[5] >= row[3]:
                raise GuardError("分析计划不可用、已跨日或请求次数耗尽。")
            if self.db.execute("SELECT 1 FROM auto_requests WHERE state IN ('started','unknown')").fetchone():
                raise UnresolvedCost("存在执行中或中断请求，先人工核对，不并发付费。")
            amount = max(row[2], row[4] + micro(estimate_rmb))
            total = self.db.execute("SELECT COALESCE(SUM(reserved_micro),0) FROM auto_plans WHERE day=? AND id<>?", (self.day(), plan_id)).fetchone()[0]
            if total + amount > micro(limits.daily_rmb):
                raise BudgetExhausted("完整上下文的实际预估超过预留预算，未发送。")
            identity = uuid4().hex
            self.db.execute("UPDATE auto_plans SET reserved_micro=?, calls=calls+1 WHERE id=?", (amount, plan_id))
            self.db.execute("INSERT INTO auto_requests (id,plan_id,state,created_at) VALUES (?,?,'started',?)", (identity, plan_id, self.stamp()))
        return identity

    def settle_request(self, request_id, *, input_tokens, output_tokens, cost, error_kind=None):
        with self.atomic():
            row = self.db.execute("SELECT plan_id,state FROM auto_requests WHERE id=?", (request_id,)).fetchone()
            if not row or row[1] != "started":
                raise GuardError("请求已结算或不存在。")
            unknown = cost is None
            self.db.execute("UPDATE auto_requests SET state=?, input_tokens=?, output_tokens=?, cost_micro=?, error_kind=? WHERE id=?",
                            ("unknown" if unknown else "settled", input_tokens, output_tokens,
                             None if unknown else micro(cost), error_kind, request_id))
            self.db.execute("UPDATE auto_plans SET spent_micro=spent_micro+?,state=CASE WHEN ? THEN 'unknown' ELSE state END WHERE id=?",
                            (0 if unknown else micro(cost), unknown, row[0]))
            # 服务商可能超出客户端估算；留痕并立即停止，不能声称可控制其账单。
            plan = self.db.execute("SELECT spent_micro,reserved_micro FROM auto_plans WHERE id=?", (row[0],)).fetchone()
            if plan[0] > plan[1]:
                self.db.execute("UPDATE auto_plans SET reserved_micro=spent_micro WHERE id=?", (row[0],))
                self.db.execute("UPDATE auto_control SET stopped=1 WHERE id=1")

    def finish_plan(self, identity, success):
        with self.atomic():
            if self.db.execute("SELECT 1 FROM auto_requests WHERE plan_id=? AND state='started'", (identity,)).fetchone():
                self.db.execute("UPDATE auto_plans SET state='unknown' WHERE id=?", (identity,))
            self.db.execute("UPDATE auto_plans SET state=?, reserved_micro=spent_micro,slots=calls WHERE id=? AND state='reserved'",
                            ("completed" if success else "failed", identity))

    def begin_application(self, job_id, adapter, packet_hash, limits):
        with self.atomic():
            self.check_running()
            mode = "simulation"  # 本版本绝不发出真实投递。
            status = self.memory.library.get(job_id)["status"]
            if status not in ("discovered", "shortlisted"):
                raise GuardError("岗位已有投递或终止状态，不再提交。")
            count = self.db.execute("SELECT COUNT(*) FROM auto_applications WHERE day=? AND mode=?", (self.day(), mode)).fetchone()[0]
            if count >= limits.daily_applications:
                raise GuardError("达到每日模拟投递次数上限。")
            identity = uuid4().hex
            try:
                self.db.execute("INSERT INTO auto_applications VALUES (?,?,?,?,?,'submitting',NULL,NULL,?,?,?)",
                                (identity, job_id, mode, adapter, packet_hash, self.stamp(), self.stamp(), self.day()))
            except Exception as error:
                import sqlite3
                if isinstance(error, sqlite3.IntegrityError):
                    raise GuardError("岗位已有申请意图，不重复投递（JD变化也不例外）。") from None
                raise
        return identity

    def register_packet(self, packet_hash, job_id, state):
        with self.atomic():
            self.db.execute("INSERT OR IGNORE INTO auto_packets VALUES (?,?,?)", (packet_hash, job_id, state))

    def check_packet(self, packet_hash, job_id):
        if not self.db.execute("SELECT 1 FROM auto_packets WHERE packet_hash=? AND job_id=? AND state='ready'", (packet_hash, job_id)).fetchone():
            raise GuardError("申请材料未核验或已被改动，禁止提交。")

    def application(self, identity):
        cursor = self.db.execute("SELECT * FROM auto_applications WHERE id=?", (identity,))
        row = cursor.fetchone()
        if row is None:
            raise GuardError("申请记录不存在。")
        return dict(zip((x[0] for x in cursor.description), row))

    def application_state(self, identity, state, receipt=None, error_kind=None):
        if state not in ("simulated_submitted", "unknown", "failed"):
            raise GuardError("无效模拟申请状态。")
        with self.atomic():
            row = self.application(identity)
            if row["state"] not in ("submitting", "unknown"):
                raise GuardError("已终结的申请不能改写。")
            self.db.execute("UPDATE auto_applications SET state=?,receipt=?,error_kind=?,updated_at=? WHERE id=?",
                            (state, receipt, error_kind, self.stamp(), identity))

    def save_summary(self, summary):
        identity = uuid4().hex
        with self.atomic():
            self.db.execute("INSERT INTO auto_runs VALUES (?,?,?)", (identity, self.stamp(), json.dumps(summary, ensure_ascii=False)))
        return identity

    def status(self):
        latest = self.db.execute("SELECT created_at,summary_json FROM auto_runs ORDER BY rowid DESC LIMIT 1").fetchone()
        sums = self.db.execute("SELECT COALESCE(SUM(reserved_micro),0), COALESCE(SUM(slots),0) FROM auto_plans WHERE day=?", (self.day(),)).fetchone()
        return {"stopped": self.stopped(), "day": self.day(), "reserved_or_spent_rmb": sums[0] / 1_000_000,
                "reserved_or_used_requests": sums[1],
                "unresolved_requests": self.db.execute("SELECT COUNT(*) FROM auto_requests WHERE state IN ('started','unknown')").fetchone()[0],
                "applications": [dict(zip(("id", "job_id", "state"), row)) for row in self.db.execute("SELECT id,job_id,state FROM auto_applications ORDER BY rowid")],
                "last_run": {"at": latest[0], "summary": json.loads(latest[1])} if latest else None}
