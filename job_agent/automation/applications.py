"""独立材料、事实来源、模拟提交、未知结果恢复；不改变真实投递状态。"""

import json
import os
from pathlib import Path
from uuid import uuid4

from agent_platform.workspace import contains_credentials
from job_agent.analysis import fingerprint
from .adapters import ApplicationRejected, ManualHandoff, MockATS, Receipt
from .decisions import decide
from .policy import digest
from .store import GuardError
from job_agent.browser.adapter import OfflineBrowserATS
from job_agent.browser.lever import LeverSnapshotATS


class ApplicationService:
    def __init__(self, store, limits, adapter):
        # 不接受从JD/配置加载可执行插件；扩展真实适配器需要独立开发与验收。
        if type(adapter) not in (MockATS, ManualHandoff, OfflineBrowserATS, LeverSnapshotATS):
            raise PermissionError("本版本只开放本地模拟和人工交接，真实平台提交未启用。")
        self.store, self.limits, self.adapter = store, limits, adapter

    async def prepare(self, runtime, job_id, candidate, report, resume, facts):
        self.store.check_running()
        if not isinstance(facts, dict) or any(not isinstance(key, str) for key in facts):
            raise GuardError("申请字段需要人工确认的JSON对象。")
        job = self.store.memory.library.get(job_id)
        if job["jd_hash"] != fingerprint(candidate.analysis_text()):
            raise GuardError("岗位JD已变化，必须重新分析后准备材料。")
        if report is None or report["resume_match"]["matching"].get("resume_fingerprint") != fingerprint(resume):
            raise GuardError("匹配报告与本次简历不一致。")
        decision = decide(candidate, report, job, now=self.store.clock(), resume=resume)
        if decision["action"] == "skip":
            return {"state": "skipped", "decision": decision}
        fields, review = {}, list(decision["review_items"])
        for field in self.adapter.fields(job):
            if field.kind == "resume":
                continue
            fact = facts.get(field.name)
            # 同意条款/开放问题不从简历猜，也不能把模型的建议当用户回答。
            valid = (isinstance(fact, dict) and fact.get("confirmed") is True
                     and isinstance(fact.get("value"), str) and bool(fact["value"].strip()))
            if valid and field.kind == "personal":
                valid = isinstance(fact.get("quote"), str) and bool(fact["quote"].strip()) and fact["quote"] in resume and fact["value"] in fact["quote"]
            elif valid:
                valid = fact.get("source") == "user_answer" and fact.get("job_id") == job_id
            if not valid:
                if field.required:
                    review.append("申请字段待人工确认：" + field.name)
                continue
            if contains_credentials(fact["value"]):
                raise GuardError("申请字段疑似包含凭据。")
            fields[field.name] = fact["value"]
        # 复用既有resume-tailor Skill，使用独立local运行时，不发生隐式付费生成。
        if runtime.llm is not None or runtime.requested_mode != "local":
            raise GuardError("自动材料生成当前只允许独立local运行时；LLM材料需单独授权。")
        prepared = await runtime.prepare_job(job_id, resume, "resume-tailor")
        if not prepared or not prepared.get("files", {}).get("tailored-resume.md"):
            raise GuardError("未获得完整且经证据处理的独立简历文件。")
        packet = {"version": 1, "job_id": job_id, "jd_hash": job["jd_hash"],
                  "resume_hash": fingerprint(resume), "adapter": self.adapter.name,
                  "target": job.get("detail_url") or job.get("source_url"), "fields": fields,
                  "resume": prepared["tailored_resume"],
                  "message": "申请贵公司的该岗位，真实项目与学习状态请见随附简历。",
                  "materials": prepared["files"], "decision": decision,
                  "review_items": list(dict.fromkeys(review)), "simulated": True}
        packet["state"] = "review_required" if review else "ready"
        # 包含个人资料的材料仅落在现有被Git忽略的私有状态目录。
        directory = Path(runtime.workspace.root) / "applications"
        if any(p.is_symlink() for p in (directory, *directory.parents)):
            raise GuardError("申请材料目录不能经过符号链接。")
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / (uuid4().hex + ".json")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(packet, stream, ensure_ascii=False, indent=2)
        self.store.register_packet(digest(packet), job_id, packet["state"])
        return {"state": packet["state"], "packet": packet, "packet_path": str(path), "decision": decision}

    async def submit(self, prepared, candidate, report, resume):
        self.store.check_running()
        packet = prepared.get("packet")
        if not packet or prepared["state"] != "ready" or packet["review_items"]:
            raise GuardError("材料或资格仍需人工确认，禁止提交。")
        job = self.store.memory.library.get(packet["job_id"])
        self.store.check_packet(digest(packet), job["job_id"])
        if (packet["jd_hash"] != job["jd_hash"] or packet["resume_hash"] != fingerprint(resume)
            or fingerprint(candidate.analysis_text()) != packet["jd_hash"]
            or report["resume_match"]["matching"].get("resume_fingerprint") != fingerprint(resume)
            or packet["adapter"] != self.adapter.name
            or packet["target"] != (job.get("detail_url") or job.get("source_url"))):
            raise GuardError("岗位、目标或简历已变化，禁止使用过期材料。")
        if decide(candidate, report, job, now=self.store.clock(), resume=resume)["action"] != "eligible":
            raise GuardError("提交前重新核对资格未通过。")
        if type(self.adapter) is ManualHandoff:
            return {"state": "manual_handoff", "job_id": job["job_id"], "packet_path": prepared["packet_path"], "target": packet["target"]}
        identity = self.store.begin_application(job["job_id"], self.adapter.name, digest(packet), self.limits)
        # 意图落盘后再检查紧急停止；出现中断不会自动重发。
        try:
            self.store.check_running()
            receipt = await self.adapter.submit(packet, identity)
            self._accept(identity, receipt)
        except BaseException as error:
            current = self.store.application(identity)
            if current["state"] == "submitting":
                self.store.application_state(identity, "failed" if isinstance(error, ApplicationRejected) else "unknown", error_kind=type(error).__name__)
            if not isinstance(error, Exception):
                raise
        return self.store.application(identity)

    def _accept(self, identity, receipt):
        attempt = self.store.application(identity)
        if (not isinstance(receipt, Receipt) or receipt.simulated is not True or receipt.attempt_id != identity
            or receipt.packet_hash != attempt["packet_hash"] or not receipt.receipt_id.startswith("mock-")):
            raise GuardError("回执不属于本申请/材料，结果转人工核实。")
        self.store.application_state(identity, "simulated_submitted", receipt=receipt.receipt_id)

    async def reconcile(self, identity):
        self.store.check_running()
        attempt = self.store.application(identity)
        if attempt["adapter"] != self.adapter.name:
            raise GuardError("恢复必须使用原平台。")
        if attempt["state"] not in ("submitting", "unknown"):
            return attempt
        receipt = await self.adapter.lookup(identity)
        if receipt:
            self._accept(identity, receipt)
        elif attempt["state"] == "submitting":
            self.store.application_state(identity, "unknown", error_kind="Interrupted")
        return self.store.application(identity)
