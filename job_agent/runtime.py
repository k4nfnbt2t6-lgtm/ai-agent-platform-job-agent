"""业务配置：继承 PlatformRuntime，只替换工作区、模型替身与 Agent 定义。"""

import asyncio
import json
from pathlib import Path

from agents import Agent

from agent_platform.demo import DemoModel, answer, current_turn, tool
from agent_platform.runtime import PlatformRuntime
from agent_platform.workspace import contains_credentials
from skill_tools import SkillCatalog

from .analysis import validate_text
from .instructions import INSTRUCTIONS
from .memory import JobMemory
from .permissions import SKILL_ORDER, PREPARATION_SKILLS, REGISTERED_SKILLS, require_skill
from .preparation_files import save_preparation
from .tools import create_tools, report
from .llm import LLMAnalyzer, LLMConfig, LLMError, LLMBudgetError
from .schemas import SCHEMA_VERSION


class JobWorkspace:
    def __init__(self, project, state_dir=None):
        self.project = Path(project).resolve(strict=True)
        directory = Path(state_dir) if state_dir else self.project / ".agent-platform" / "job-agent"
        if not directory.is_absolute():
            directory = self.project / directory
        if any(p.is_symlink() for p in (directory, *directory.parents)) or not directory.resolve().is_relative_to(self.project):
            raise ValueError("Job Agent 状态目录必须在项目内，不能经过链接。")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = directory.resolve(strict=True)
        for name in ("jobs.sqlite", "sessions.sqlite"):
            path = self.root / name
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                    raise ValueError("Job Agent 数据库必须是独立普通文件。")
        self.skills = SkillCatalog(self.project, "job_agent/skills")
        self.memory = JobMemory(self.root / "jobs.sqlite")
        self.pending = None  # 没有文件写入工具，兼容共同 Runtime 的权限入口。
        self.jd = self.resume = None
        self.analysis_result = self.match_result = None
        self.gap_result = None
        self.preparation_job = self.preparation_result = None
        self.preparation_plan = None
        self.preparation_approved = False
        self.llm = None
        self.source_label = "用户粘贴"
        self.selected = "all"
        self.loaded = set()

    def prepare(self, jd, resume=None, selected="all", source_label="用户粘贴"):
        if selected not in ("all", *REGISTERED_SKILLS):
            raise ValueError("未知 Job Skill。")
        jd = validate_text(jd, "JD")
        resume = validate_text(resume, "简历/能力文本") if resume is not None else None
        if selected != "analyze-jd" and resume is None:
            raise ValueError("此 Skill 需要当前简历/能力文本；仅提取 JD 可使用 analyze-jd。")
        source_label = validate_text(source_label, "材料来源")
        if contains_credentials(jd) or resume and contains_credentials(resume) or contains_credentials(source_label):
            raise ValueError("JD 或简历中疑似含凭据，已阻止分析与保存。")
        self.jd, self.resume, self.selected, self.source_label = jd, resume, selected, source_label
        self.analysis_result = self.match_result = None
        self.gap_result = None
        self.preparation_job = self.preparation_result = None
        self.preparation_plan = None
        self.preparation_approved = False
        self.loaded.clear()

    def require_skill(self, name):
        require_skill(self.selected, self.loaded, name)

    async def close(self):
        self.memory.close()


class JobModel(DemoModel):
    """本地模型替身仅选择工具顺序；业务结果由真实文本分析工具计算。"""
    async def get_response(self, system_instructions, input, *args, **kwargs):
        task, outputs = current_turn(input)
        selected = json.loads(task)["workflow"]
        skills = SKILL_ORDER if selected == "all" else (selected,)
        index = len(outputs)
        if index == 0:
            return tool("load_skill", {"names": list(skills)})
        if index <= len(skills):
            return tool(skills[index - 1].replace("-", "_"), {})
        return answer("所选 Job Skill 已执行，报告由宿主汇总真实工具结果。")


class JobRuntime(PlatformRuntime):
    session_namespace = "job:"

    def __init__(self, *args, mode="local", model=None, fallback="local", approve_llm=None, **kwargs):
        if mode not in ("local", "llm") or fallback not in ("local", "none"):
            raise ValueError("分析模式或 fallback 配置无效。")
        self.requested_mode, self.fallback, self.approve_llm = mode, fallback, approve_llm
        self.analysis_lock = asyncio.Lock()
        self.llm = LLMAnalyzer(LLMConfig.from_env(model)) if mode == "llm" else None
        # SDK 决策循环仍由统一底座执行；付费调用只在获准的业务工具内发生。
        if mode == "llm":
            kwargs["demo"] = True
        super().__init__(*args, **kwargs)

    def task_timeout_seconds(self):
        return max(400, self.llm.config.timeout_seconds * max(3, self.llm.config.preparation_passes) + 30) if self.llm else 400

    def usage(self):
        data = super().usage()
        if self.llm:
            data.update(self.llm.usage(), mode="LLM 业务分析；SDK 决策为本地替身")
        return data

    def make_workspace(self, project, state_dir):
        return JobWorkspace(project, state_dir)

    def demo_model(self):
        return JobModel()

    def build_agent(self, model, task):
        return Agent(name="Job Agent", instructions=INSTRUCTIONS + "\n可用 Skills：" + json.dumps(self.workspace.skills.metadata(), ensure_ascii=False),
                     model=model, tools=create_tools(self.workspace), model_settings=self.model_settings())

    async def analyze(self, jd, resume=None, selected="all", source_label="用户粘贴"):
        if selected in PREPARATION_SKILLS:
            raise ValueError("求职准备请从岗位库使用 /tailor 编号 或 /interview-prep 编号。")
        if self.analysis_lock.locked():
            raise ValueError("已有分析或材料确认运行，请等待。")
        async with self.analysis_lock:
            return await self._analyze(jd, resume, selected, source_label)

    async def prepare_job(self, job_id, resume, selected):
        """复用 SDK/Skill 工作流；每项准备最多一次获准请求，失败只回退本地。"""
        if selected not in PREPARATION_SKILLS:
            raise ValueError("未知准备 Skill；支持 resume-tailor / interview-prep。")
        if self.analysis_lock.locked() or self.busy:
            raise ValueError("已有分析或准备运行，请等待。")
        if not self.demo:
            raise ValueError("求职准备只支持本地决策循环，不允许网络模型执行。")
        async with self.analysis_lock:
            job = self.workspace.memory.library.get(job_id)
            if not job["jd"] or job["jd_kind"] == "legacy_missing":
                raise ValueError("该岗位缺少 JD 原文，请先重新导入岗位 JD。")
            self.workspace.prepare(job["jd"], resume, selected, "岗位库")
            self.workspace.preparation_job = job
            self.workspace.llm = None
            before = self.llm.usage() if self.llm else None
            estimate = None
            fallback_reason = None
            if self.llm:
                from .preparation_llm import PROMPT, plan, preflight_payload
                prepared = plan(job, self.workspace.resume, selected)
                self.workspace.preparation_plan = prepared
                payload_before = json.dumps(prepared["payload"], ensure_ascii=False, sort_keys=True)
                scope_before = (self.workspace.jd, self.workspace.resume, self.workspace.selected)
                backend_before, config_before = self.llm, self.llm.config
                job_before = json.dumps(job, ensure_ascii=False, sort_keys=True)
                try:
                    preflight_payload(prepared)
                    estimate = self.llm.estimate_preparation(prepared, PROMPT)
                    self.llm.preflight(estimate)
                    if self.approve_llm is None or not await self.approve_llm(self.workspace, estimate, self.llm.config):
                        self.emit("status", "未确认本次准备材料及费用，没有发送 API 请求，也未生成新文件。")
                        return None
                    if (self.llm is not backend_before or self.llm.config != config_before
                            or scope_before != (self.workspace.jd, self.workspace.resume, self.workspace.selected)
                            or self.workspace.preparation_plan is not prepared
                            or job_before != json.dumps(self.workspace.preparation_job, ensure_ascii=False, sort_keys=True)
                            or payload_before != json.dumps(prepared["payload"], ensure_ascii=False, sort_keys=True)):
                        raise ValueError("确认期间材料发生变化，已取消；请重新确认新的数据范围。")
                    self.workspace.llm = self.llm
                    self.workspace.preparation_approved = True
                except LLMError as error:
                    if isinstance(error, LLMBudgetError):
                        self.emit("error", str(error) + " 未自动改用 local；请先确认预算与次数。")
                        return None
                    fallback_reason = str(error)
                    if self.fallback == "none":
                        self.emit("error", fallback_reason)
                        return None
                    self.emit("error", fallback_reason + " 已回退 local；没有新增付费请求。")
            result = await self.run_task(json.dumps({"workflow": selected}, ensure_ascii=False))
            completed = any(item["name"] == selected.replace("-", "_")
                            and isinstance(item["output"], dict) and item["output"].get("ok")
                            for item in self.recorder.tool_calls)
            if result is None or not completed or self.workspace.preparation_result is None:
                if self.workspace.llm is None:
                    self.emit("error", "准备 Skill 未实际完成，没有生成报告文件。")
                    return None
                self.llm.failed = True
                if self.llm.last_error_kind == "budget":
                    self.emit("error", self.llm.last_error + " 未自动改用 local；请先确认预算与次数。")
                    return None
                fallback_reason = self.llm.last_error or "LLM 准备未通过请求、结构或证据校验。"
                if self.fallback == "none":
                    self.emit("error", fallback_reason + " 未生成准备文件。")
                    return None
                self.emit("error", fallback_reason + " 已停止付费请求；仅回退 local，不重试 API。")
                self.workspace.prepare(job["jd"], resume, selected, "岗位库")
                self.workspace.preparation_job = job
                self.workspace.llm = None
                result = await self.run_task(json.dumps({"workflow": selected}, ensure_ascii=False))
                if result is None or self.workspace.preparation_result is None:
                    return None
            data = self.workspace.preparation_result
            if fallback_reason:
                self.emit("error", "质量降级：本次仅为 local 规则结果，未完成 LLM 语义生成/验收，请人工复核。")
            data.update(quality_degraded=bool(fallback_reason), semantic_complete=self.workspace.llm is not None,
                        quality_warning="质量降级：local 规则不能替代 LLM 语义验收，请人工复核。" if fallback_reason else None,
                        requested_mode=self.requested_mode, fallback_reason=fallback_reason,
                        model=self.llm.config.model if self.workspace.llm else None)
            if self.llm:
                after = self.llm.usage()
                requests = after["llm_requests"] - before["llm_requests"]
                data.update(model_requests=requests,
                            input_tokens=after["input_tokens"] - before["input_tokens"],
                            output_tokens=after["output_tokens"] - before["output_tokens"],
                            estimated_rmb=None if requests and after["unknown_usage"] else
                            after["known_estimated_rmb"] - before["known_estimated_rmb"], llm_usage=after)
                data["llm_estimate"] = estimate
            self.workspace.preparation_approved = False
            return save_preparation(self.workspace.root, self.workspace.preparation_result)

    async def _analyze(self, jd, resume=None, selected="all", source_label="用户粘贴"):
        if self.busy:
            raise ValueError("已有分析运行，请等待。")
        self.workspace.prepare(jd, resume, selected, source_label)
        self.workspace.llm = self.llm
        fallback_reason = None
        if self.llm:
            try:
                cached = self.workspace.memory.get_llm(self.workspace.jd, self.llm.config.model)
                calls = {"analyze-jd": 1, "match-resume": 2, "skill-gap-analysis": 3, "all": 3}[selected] - bool(cached)
                estimate = self.llm.estimate(self.workspace.jd, self.workspace.resume, calls)
                self.llm.preflight(estimate)
                if calls and (self.approve_llm is None or not await self.approve_llm(self.workspace, estimate, self.llm.config)):
                    self.emit("status", "没有得到本次材料与费用确认，未发起 LLM 请求。")
                    return None
            except LLMError as error:
                if isinstance(error, LLMBudgetError):
                    self.emit("error", str(error) + " 未自动改用 local；请先确认预算与次数。")
                    return None
                fallback_reason = str(error)
                if self.fallback == "none":
                    self.emit("error", fallback_reason)
                    return None
                self.workspace.llm = None
                self.emit("error", fallback_reason + " 已回退 local。")
        start = 0 if self.demo else len(self.recorder.tool_calls)
        result = await self.run_task(json.dumps({"workflow": selected}, ensure_ascii=False))
        if result is None:
            if not self.llm or self.workspace.llm is None:
                return None
            self.llm.failed = True
            if self.llm.last_error_kind == "budget":
                self.emit("error", self.llm.last_error + " 未自动改用 local；请先确认预算与次数。")
                return None
            if self.fallback == "none":
                self.emit("error", "LLM 分析失败，未启用 local fallback；本次结果未完成。")
                return None
            self.emit("error", "LLM 分析失败，停止付费请求；重新用 local 完成本次分析。")
            fallback_reason = self.llm.last_error or "LLM 请求、结构或证据校验失败；未自动重试。"
            self.workspace.prepare(jd, resume, selected, source_label)
            self.workspace.llm = None
            start = 0
            result = await self.run_task(json.dumps({"workflow": selected}, ensure_ascii=False))
            if result is None:
                return None
        expected = SKILL_ORDER if selected == "all" else (selected,)
        completed = {item["name"] for item in self.recorder.tool_calls[start:]
                     if isinstance(item["output"], dict) and item["output"].get("ok")}
        if not all(name.replace("-", "_") in completed for name in expected):
            self.emit("error", "所选 Job Skill 未全部实际执行，不能把模型文字当作分析完成。")
            return None
        data = report(self.workspace)
        if fallback_reason:
            self.emit("error", "质量降级：本次仅为 local 规则结果，未完成 LLM 语义分析/验收，请人工复核。")
        data.update(quality_degraded=bool(fallback_reason), semantic_complete=self.workspace.llm is not None,
                    quality_warning="质量降级：local 规则不能替代 LLM 语义验收，请人工复核。" if fallback_reason else None)
        data.update(requested_mode=self.requested_mode, model=self.llm.config.model if self.workspace.llm else None,
                    schema_version=SCHEMA_VERSION, fallback_reason=fallback_reason)
        if self.llm:
            data["llm_usage"] = self.llm.usage()
            self.workspace.memory.save_report(self.workspace.jd, self.workspace.resume, selected, data)
        data["job_id"] = self.workspace.memory.library.remember_analysis(
            self.workspace.jd, data["jd_analysis"]["analysis"], self.workspace.source_label, data)
        return data
