"""一次性的 v6 人工资料 API 验收；默认仅预览，--run 才可能收费。"""

from dataclasses import replace
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from agent_platform.workspace import contains_credentials
from job_agent.analysis import analyze_jd, validate_text
from job_agent.llm import LLMAnalyzer, LLMConfig, LLMError
from job_agent.preparation_llm import PROMPT, plan, preflight_payload
from job_agent.runtime import JobRuntime

PROJECT = Path(__file__).resolve().parent
DIRECTORY = PROJECT / ".agent-platform/job-v6-first-api-acceptance"
SKILLS = ("resume-tailor", "interview-prep")
BUDGET = 0.35
REQUEST_LIMIT = 2
MODEL = "gpt-6-sol"
RECORDED_ENDPOINT = "https://api.likeai520.cc/v1"
FIXTURES = {
    "preparation-jd.example.md": "24403d1f82afb458aa48d2b989712b18bf60ea94b6325c7158111e9ecae91131",
    "preparation-resume.example.md": "369033d2118a68fa09580b40de4926473e0ac4314778cfb002f94f6a41cf8d7d",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def acceptance_config(config):
    """历史双Skill验收的明确范围，不作为业务默认参数。"""
    return replace(config, max_output_tokens=4096, reasoning_effort="none", preparation_passes=1,
                   max_requests=REQUEST_LIMIT, budget_rmb=BUDGET)


def artificial_materials(project=PROJECT):
    values = []
    for filename, expected in FIXTURES.items():
        path = project / "job_agent/examples" / filename
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("人工样例不存在、经过链接或已变化；拒绝替换为任何真实个人资料。")
        values.append(validate_text(path.read_text(), "人工验收材料"))
    return tuple(values)


def prepared_materials(jd, resume):
    job = {"job_id": 1, "company": "人工准备科技", "title": "AI Agent应用开发实习生", "city": "广州",
           "jd": jd, "source_url": None, "detail_url": None}
    prepared = {skill: plan(job, resume, skill) for skill in SKILLS}
    for value in prepared.values():
        preflight_payload(value)
    return prepared


def scope_hash(prepared):
    return digest(json.dumps(prepared["payload"], ensure_ascii=False, sort_keys=True))


def estimates(backend, prepared):
    return {skill: backend.estimate_payload(skill, value["payload"], value["schema"], prompt=PROMPT)
            for skill, value in prepared.items()}


def checked_total(backend, prepared):
    values = estimates(backend, prepared)
    total = backend.usage()["known_estimated_rmb"] + sum(
        backend.cost(value["estimated_input_tokens"], value["max_output_tokens"]) for value in values.values())
    if total > BUDGET or backend.config.input_price <= 0 or backend.config.output_price <= 0:
        raise LLMError("两项完整流程的保守估算超过0.35元，或价格无效；没有发起请求。")
    return values, total


def show_scope(config, prepared, values, total, *, preview=False):
    print("仅预览，不发起请求。" if preview else "第一次真实验收：只发送以下固定人工资料。")
    print(f"接收端：{config.base_url}；模型：{MODEL}；最多2次，总预估预算0.35元。")
    print(f"完整两项流程保守估算：{total:.6f}元；输入${config.input_price}/百万、输出${config.output_price}/百万，$1=10元。")
    print("价格来自项目配置；真实账单需向中转站核对，客户端不能保证服务商计费硬上限。")
    for skill, value in prepared.items():
        estimate = values[skill]
        print(f"\n{skill}：最多1次，保守输入{estimate['estimated_input_tokens']}，最多输出{estimate['max_output_tokens']} token。")
        print(json.dumps(value["payload"], ensure_ascii=False, indent=2))
    print("不发送真实简历、原文件、Key、路径、源码或历史会话；store=False，无自动重试。")


def safe_directory(directory):
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError("验收目录不能经过符号链接。")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not directory.is_dir():
        raise ValueError("验收目录必须是文件夹。")


def safe_file(path):
    if path.is_symlink() or path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("验收产物必须是独立普通文件。")


def write_private(path, content):
    safe_file(path)
    if contains_credentials(content):
        raise ValueError("验收输出疑似含凭据，已阻止写入。")
    # 先落盘临时文件再原子替换，避免中断后把收费记录截断为可重新运行的空文件。
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def exclusive(directory):
    safe_directory(directory)
    path = directory / ".lock"
    safe_file(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("已有验收进程运行；未启动第二个收费进程。") from None
        yield
    finally:
        os.close(fd)


class Journal:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / "acceptance.json"
        self.data = None

    def read(self):
        safe_file(self.path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        return self.data

    def start(self, config, prepared):
        if self.path.exists():
            raise ValueError("已有本轮验收记录，不允许重置收费次数。")
        self.data = {"status": "running", "started_at": now(), "model": MODEL, "base_url": config.base_url,
                     "budget_rmb": BUDGET, "max_requests": REQUEST_LIMIT, "real_api_requests": 0,
                     "pricing": {"input_usd_per_million": config.input_price, "output_usd_per_million": config.output_price, "usd_to_rmb": 10},
                     "material_hashes": {skill: scope_hash(value) for skill, value in prepared.items()},
                     "attempts": [], "results": [], "usage": None, "billed_rmb": None,
                     "quality_conclusion": "未验收；自由语言生成质量需逐条人工复核"}
        self.save()

    def save(self):
        write_private(self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")


class AcceptanceLLM(LLMAnalyzer):
    """只收紧验收进程边界；复用现有客户端/schema/计费，不修改业务源码。"""
    def __init__(self, config, journal):
        super().__init__(config)
        self.journal = journal

    def preflight(self, estimate):
        super().preflight(estimate)
        if (self.unknown_usage or self.requests + estimate["calls"] > REQUEST_LIMIT
                or estimate["projected_rmb"] > BUDGET):
            raise LLMError("达到本轮2次/0.35元边界，或存在未知费用；停止且不重试。")

    async def ask(self, skill, payload, schema, *, prompt=PROMPT):
        self.preflight(self.estimate_payload(skill, payload, schema, prompt=prompt))
        attempts = self.journal.data["attempts"]
        if skill not in SKILLS or len(attempts) >= REQUEST_LIMIT or any(item["skill"] == skill for item in attempts):
            raise LLMError("该 Skill 已尝试，或达到两次上限；禁止重复收费请求。")
        if digest(json.dumps(payload, ensure_ascii=False, sort_keys=True)) != self.journal.data["material_hashes"][skill]:
            raise LLMError("待发送材料与确认的人工材料不一致，未请求。")
        entry = {"skill": skill, "status": "in_flight", "started_at": now(), "schema_validation": "未确认"}
        attempts.append(entry)
        self.journal.save()  # 请求前持久化；即使被强制结束，也不会自动重新收费。
        before = self.usage()
        try:
            output = await super().ask(skill, payload, schema, prompt=prompt)
            if self.usage()["known_estimated_rmb"] > BUDGET:
                raise LLMError("服务商返回用量按配置估算超过0.35元，已停止；请核对账单，不继续或重试。")
            entry.update(status="response_received", schema_validation="通过")
            artifact = self.journal.directory / f"{skill}.schema-output.json"
            write_private(artifact, output.model_dump_json(indent=2) + "\n")
            entry["schema_output"] = str(artifact)
            return output
        except LLMError as error:
            self.failed = True
            self.last_error = str(error)
            entry.update(status="failed", error=str(error))
            raise
        finally:
            usage = self.usage()
            entry.update(finished_at=now(), requests=self.requests - before["llm_requests"],
                         input_tokens=usage["input_tokens"] - before["input_tokens"],
                         output_tokens=usage["output_tokens"] - before["output_tokens"],
                         estimated_rmb=None if self.unknown_usage else
                         usage["known_estimated_rmb"] - before["known_estimated_rmb"])
            self.journal.data.update(usage=usage, real_api_requests=self.requests)
            self.journal.save()


def quality_signals(skill, data):
    """客观检查只作信号；不把关键词检查或合法 JSON 冒充生成质量通过。"""
    if skill == "resume-tailor":
        text = data["tailored_resume"]
        return {"complete_sections": all("## " + label in text for label in ("教育背景", "技术技能", "项目经历")),
                "three_projects_present": all(name in text for name in ("AI Agent Platform", "Job Agent", "Jieqi Agent")),
                "python_basics_retained": "Python基础" in text,
                "learning_context_retained": "正在学习或尚未熟练" in text,
                "api_basics_retained": "API基础" in text,
                "changes_with_jd_evidence": all(change["jd_evidence"] for change in data["changes"]),
                "semantic_quality": "待人工复核：是否更贴合JD、是否改变原意或遗漏贡献限定"}
    questions = data["questions"]
    return {"question_count": len(questions),
            "three_project_topics": {q.get("project") for q in questions if q["type"] == "project"}
            == {"AI Agent Platform", "Job Agent", "Jieqi Agent"},
            "python_not_promoted": all(q["status"] != "matched" for q in questions if q.get("skill") == "Python"),
            "llm_api_not_promoted": all(q["status"] != "matched" for q in questions if q.get("skill") == "LLM API"),
            "project_followups": all(len(q["follow_ups"]) >= 2 for q in questions if q["type"] == "project"),
            "semantic_quality": "待人工复核：每题具体性、技术正确性、答案解释价值与项目事实边界"}


def summary_markdown(data):
    lines = ["# Job Agent v6 第一次真实 API 验收", f"运行状态：{data['status']}",
             "只使用固定人工简历/JD。schema通过、原文规则通过和人工生成质量评审是不同结论。",
             "|Skill|真实请求数|schema|证据检查|输入 token|输出 token|按配置估算费用|",
             "|---|---:|---|---|---:|---:|---:|"]
    for skill in SKILLS:
        attempt = next((item for item in data["attempts"] if item["skill"] == skill), {})
        result = next((item for item in data["results"] if item["skill"] == skill), {})
        fee = attempt.get("estimated_rmb")
        label = "未知" if attempt and fee is None else f"{fee or 0:.6f}元"
        lines.append(f"|{skill}|{attempt.get('requests', '未知' if attempt else 0)}|{attempt.get('schema_validation', '未调用')}|{result.get('evidence_validation', '未检查')}|{attempt.get('input_tokens', '未知' if attempt else 0)}|{attempt.get('output_tokens', '未知' if attempt else 0)}|{label}|")
        if result.get("files"):
            lines += [f"\n## {skill} 产物"] + [f"- {name}：{path}" for name, path in result["files"].items()]
            lines += ["客观检查：" + json.dumps(result["quality_signals"], ensure_ascii=False)]
        if result.get("reason") or attempt.get("error"):
            lines.append(f"\n{skill} 停止原因：" + (result.get("reason") or attempt["error"]))
    usage = data.get("usage") or {}
    fee = usage.get("estimated_rmb")
    total = "未知，请核对服务商账单" if data["attempts"] and fee is None else f"{fee or 0:.6f}元"
    lines += ["\n按配置估算总费用：" + total, "实际扣费：未从API取得账单，请本人核对中转站账单。",
              "生成质量结论：" + data["quality_conclusion"],
              "不会自动重试；重复运行此脚本只查看本轮记录，不继续收费。"]
    return "\n\n".join(lines) + "\n"


async def execute(config, prepared, jd, resume, journal, project=PROJECT):
    journal.start(config, prepared)
    backend = AcceptanceLLM(config, journal)
    async def approved(workspace, estimate, actual_config):
        current = workspace.preparation_plan
        if actual_config != config or scope_hash(current) != journal.data["material_hashes"][workspace.selected]:
            raise ValueError("材料/接收端发生变化，取消本次调用。")
        backend.preflight(estimate)
        request_cost = backend.cost(estimate["estimated_input_tokens"], estimate["max_output_tokens"])
        print(f"即将调用 {workspace.selected}：本次保守估算{request_cost:.6f}元，累计保守估算{estimate['projected_rmb']:.6f}元。", flush=True)
        return True  # 仅延续 --run 后针对固定人工材料/接收端/2次预算的整轮确认。

    runtime = None
    try:
        runtime = JobRuntime(project, demo=True, mode="llm", model=MODEL, fallback="none", approve_llm=approved,
                             state_dir=journal.directory / "runtime")
        runtime.llm = backend
        record = runtime.workspace.memory.remember(jd, analyze_jd(jd), "固定人工验收资料")
        identity = runtime.workspace.memory.library.linked("jd", record["jd_id"])
        before_job = runtime.workspace.memory.library.get(identity)
        fixture_before = artificial_materials(project)
        for skill in SKILLS:
            data = await runtime.prepare_job(identity, resume, skill)
            if data is None:
                journal.data["results"].append({"skill": skill, "evidence_validation": "未通过/未完成",
                                               "reason": backend.last_error or "准备工作流未完成，未将模型文字当作成功。"})
                journal.data["status"] = "failed"
                break
            signals = quality_signals(skill, data)
            valid = data["mode"] == "llm" and data["model_requests"] == 1 and data.get("evidence_checked")
            unchanged = before_job == runtime.workspace.memory.library.get(identity) and fixture_before == artificial_materials(project)
            journal.data["results"].append({"skill": skill, "evidence_validation": "通过" if valid else "未通过",
                                           "files": data["files"], "quality_signals": signals,
                                           "original_materials_and_job_unchanged": unchanged})
            if not valid or not unchanged or any(value is False for value in signals.values()):
                journal.data["status"] = "failed"
                break
            journal.save()
        else:
            journal.data["status"] = "automated_checks_passed"
            journal.data["quality_conclusion"] = "结构、原文证据规则及客观检查通过；生成质量与实际账单仍待人工核对"
    except (ValueError, OSError):
        journal.data["status"] = "failed"
        journal.data["error"] = "验收中断或校验/文件操作失败，停止且不重试；详见每次请求的安全记录。"
    finally:
        if runtime:
            await runtime.close()
        journal.data.update(finished_at=now(), usage=backend.usage(), real_api_requests=backend.requests)
        journal.save()
        write_private(journal.directory / "summary.md", summary_markdown(journal.data))
    return 0 if journal.data["status"] == "automated_checks_passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="固定人工资料的v6验收：默认预览，--run真实请求，最多2次/0.35元，不重试。")
    parser.add_argument("--run", action="store_true", help="检查现有环境变量，展示材料并确认后执行；重复运行只查看记录")
    args = parser.parse_args(argv)
    try:
        if args.run:
            with exclusive(DIRECTORY):
                journal = Journal(DIRECTORY)
                if journal.read() is not None:
                    print(summary_markdown(journal.data))
                    print("已有记录，不再次调用API。记录：" + str(journal.path))
                    return 0
                jd, resume = artificial_materials()
                prepared = prepared_materials(jd, resume)
                config = acceptance_config(LLMConfig.from_env(MODEL))
                if config.base_url != RECORDED_ENDPOINT:
                    raise ValueError("API接收端与项目记录的现有中转站不同；未请求，请先核对地址并确认。")
                values, total = checked_total(LLMAnalyzer(config), prepared)
                show_scope(config, prepared, values, total)
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise ValueError("真实验收必须在Mac Terminal交互运行，不能通过管道静默收费。")
                if input("确认上述人工材料、接收端和2次/0.35元预算，输入“确认人工验收”（其他输入取消）：").strip() != "确认人工验收":
                    print("已取消，未请求API。")
                    return 0
                code = asyncio.run(execute(config, prepared, jd, resume, journal))
                print(summary_markdown(journal.data))
                print("结果目录：" + str(DIRECTORY))
                return code
        jd, resume = artificial_materials()
        prepared = prepared_materials(jd, resume)
        try:
            config = acceptance_config(LLMConfig.from_env(MODEL, require_key=False))
        except LLMError:
            config = acceptance_config(LLMConfig("", RECORDED_ENDPOINT, MODEL, .30, 1.50))
            print("当前API配置不可用；以下仅按项目记录的接收端与价格预览，不会自动配置或调用。")
        values, total = checked_total(LLMAnalyzer(config), prepared)
        show_scope(config, prepared, values, total, preview=True)
        print("环境就绪后执行：.venv/bin/python -B accept_job_v6_api.py --run")
        return 0
    except LLMError as error:
        print("验收未完成：" + str(error), file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print("验收未完成：配置缺失/无效、材料或状态目录检查失败。请在自己的Terminal设置现有OPENAI_API_KEY、OPENAI_BASE_URL；不要发到聊天。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("验收已停止；若已发送请求可能计费，不会自动重试。")
        return 130
    except Exception:
        print("验收异常停止；不打印可能含凭据的异常正文。不要重新收费，检查本地验收记录。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
