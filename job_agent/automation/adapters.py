"""投递平台接口；首版仅本机模拟与人工交接，不包含真实 POST 实现。"""

from dataclasses import dataclass
from typing import Protocol

from .policy import digest
from .store import GuardError


class ApplicationRejected(GuardError):
    """平台明确拒收，与无法判断是否已收件的连接异常区分。"""


@dataclass(frozen=True)
class Field:
    name: str
    required: bool = True
    kind: str = "personal"

    def __post_init__(self):
        if not self.name or self.kind not in ("personal", "answer", "consent", "resume") or type(self.required) is not bool:
            raise ValueError("申请字段定义无效。")


@dataclass(frozen=True)
class Receipt:
    attempt_id: str
    packet_hash: str
    receipt_id: str
    simulated: bool = True


class ApplicationAdapter(Protocol):
    name: str
    def fields(self, job): ...
    async def submit(self, packet, attempt_id) -> Receipt: ...
    async def lookup(self, attempt_id) -> Receipt | None: ...


class MockATS:
    """本地SQLite模拟招聘平台；收件回执可跨进程恢复，无HTTP代码。"""
    name = "mock-ats"

    def __init__(self, store, *, extra_fields=(), behavior="success"):
        if behavior not in ("success", "reject", "timeout", "accepted_timeout", "wrong_receipt"):
            raise ValueError("未知模拟平台行为。")
        self.store, self.extra_fields, self.behavior = store, tuple(extra_fields), behavior
        self.submit_calls = 0
        store.db.execute("CREATE TABLE IF NOT EXISTS mock_ats_receipts (attempt_id TEXT PRIMARY KEY, packet_hash TEXT NOT NULL, receipt_id TEXT NOT NULL)")
        store.db.commit()

    def fields(self, job):
        return (Field("name"), Field("email"), Field("resume", kind="resume"), *self.extra_fields)

    async def submit(self, packet, attempt_id):
        self.submit_calls += 1
        if self.behavior == "reject":
            raise ApplicationRejected("模拟平台拒收。")
        if self.behavior == "timeout":
            raise TimeoutError("模拟网络失败；没有可核对回执。")
        packet_hash = digest(packet)
        receipt_id = "mock-" + digest([attempt_id, packet_hash])[:24]
        with self.store.atomic():
            self.store.db.execute("INSERT OR IGNORE INTO mock_ats_receipts VALUES (?,?,?)", (attempt_id, packet_hash, receipt_id))
        if self.behavior == "accepted_timeout":
            raise TimeoutError("模拟服务端已收件但客户端超时。")
        return Receipt(attempt_id, "wrong" if self.behavior == "wrong_receipt" else packet_hash, receipt_id)

    async def lookup(self, attempt_id):
        row = self.store.db.execute("SELECT packet_hash,receipt_id FROM mock_ats_receipts WHERE attempt_id=?", (attempt_id,)).fetchone()
        return Receipt(attempt_id, *row) if row else None


class ManualHandoff:
    """只提供本地材料和原始详情链接，由用户自行访问并提交。"""
    name = "manual-handoff"

    def fields(self, job):
        return (Field("resume", kind="resume"),)

    async def submit(self, packet, attempt_id):
        raise PermissionError("人工交接不会执行提交。")

    async def lookup(self, attempt_id):
        return None
