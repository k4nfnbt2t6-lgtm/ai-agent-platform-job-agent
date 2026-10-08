"""Scheduled Tasks 课程：模型安排任务，调度器到期执行，按实际时间验收。"""

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

from agents import Agent, MaxTurnsExceeded, ModelSettings, OpenAIResponsesModel, RunConfig, Runner
from agents.items import ToolCallOutputItem
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from openai.types.shared import Reasoning

from agent import MAX_OUTPUT_TOKENS, LoopRecorder, LoopStopped, print_usage
from scheduled_tools import ScheduledTasks, create_scheduled_tools
from tools import PROJECT_DIR


MAX_MODEL_REQUESTS, MAX_TOOL_CALLS = 4, 3
REQUEST_TIMEOUT_SECONDS, TOTAL_TIMEOUT_SECONDS = 120.0, 540
INSTRUCTIONS = (
    "你是 Scheduled Tasks 课程助手。先用 schedule_task 按本次约定延迟安排人工整数汇总，"
    "再用实际返回的任务编号查询一次状态，最后取得结果。调度器会自动执行，"
    "状态查询和取结果不会提前启动任务。每轮一个工具，只调度一次，不重复轮询或重试。"
    "工具失败停止。模型响应期间可能到期，如实采用实际状态和时间。"
    "最终只输出 JSON：task_id、status、sum、count、receipt、not_early。"
    "前五项来自结果工具；not_early 根据实际 started_at >= run_at 判断，不能凭猜测。"
)


class ScheduledRecorder(LoopRecorder):
    def __init__(self):
        super().__init__(max_model_requests=MAX_MODEL_REQUESTS, max_tool_calls=MAX_TOOL_CALLS)

    async def on_tool_end(self, context, agent, tool, result):
        await super().on_tool_end(context, agent, tool, result)
        print("工具实际返回：", json.dumps(result, ensure_ascii=False), flush=True)
        if not isinstance(result, dict) or not result.get("ok"):
            raise LoopStopped("调度工具失败或被拒绝，已停止，没有自动重试。")


def verify_run(result, recorder, manager):
    records = recorder.tool_calls
    if len(manager.jobs) != 1 or len(records) != 3 or [r["name"] for r in records] != [
            "schedule_task", "get_task_status", "get_task_result"]:
        return {"模型安排、查询、取结果三个工具均真实调用": False}
    job = next(iter(manager.jobs.values()))
    actual = records[2]["output"].get("task", {})
    try:
        final = json.loads(result.final_output)
    except (TypeError, ValueError):
        final = None
    not_early = job.started_at is not None and job.started_at >= job.run_at
    expected = {"task_id": job.task_id, "status": "completed", "sum": sum(manager.numbers),
                "count": len(manager.numbers), "receipt": (job.result or {}).get("receipt"), "not_early": True}
    outputs = {item.call_id: item.raw_item["output"] for item in result.new_items if isinstance(item, ToolCallOutputItem)}
    before = [e for e in manager.observations if e["at"] < job.run_at]
    return {
        "模型安排、查询、取结果三个工具均真实调用": [r["turn"] for r in records] == [1, 2, 3],
        "提交立即返回 scheduled，执行尚未开始": (
            records[0]["output"]["task"]["status"] == "scheduled"
            and records[0]["output"]["task"]["started_at"] is None
            and records[0]["output"]["task"]["result"] is None),
        "计划时间严格来自提交时间加约定延迟": (
            job.run_at == job.created_at + manager.delay_seconds
            and records[0]["arguments"] == {"numbers": manager.numbers, "delay_seconds": manager.delay_seconds}),
        "到期前的实际观察均未开始，且没有 worker 结果": (
            bool(before) and all(e["task"]["status"] == "scheduled" and e["task"]["started_at"] is None
                                and e["task"]["result"] is None for e in before)),
        "调度器实际开始时间不早于计划时间": not_early,
        "真实状态经过 scheduled → running → completed": [h["status"] for h in job.history] == ["scheduled", "running", "completed"],
        "查询和取结果使用同一真实编号，结果及回执一致": (
            all(r["arguments"] == {"task_id": job.task_id} and r["output"]["task"]["task_id"] == job.task_id for r in records[1:])
            and records[2]["output"].get("ready") is True and actual.get("result") == job.result
            and job.result is not None and job.result["sum"] == expected["sum"]),
        "模型最终报告与实际结果和时间证据一致": (
            final == expected and isinstance(final, dict) and final["not_early"] is True
            and type(final["sum"]) is int and type(final["count"]) is int),
        "工具实际返回值均带入后续请求": (
            len(outputs) == 3 and len(recorder.model_inputs) == 4 and all(r["call_id"] in outputs
                and recorder.model_inputs[i].get(r["call_id"]) == outputs[r["call_id"]] for i, r in enumerate(records, start=1))),
        "四次模型请求、三次工具处理，没有额外调用": len(recorder.responses) == len(result.raw_responses) == 4,
    }


def save_record(manager, recorder=None, verified=False):
    payload = manager.snapshot() | {"verified": verified}
    if recorder:
        payload["usage"] = {"requests": len(recorder.model_inputs), "tool_calls": len(recorder.tool_calls),
            "input_tokens": sum(r.usage.input_tokens for r in recorder.responses),
            "output_tokens": sum(r.usage.output_tokens for r in recorder.responses), "warnings": recorder.usage_warnings}
    descriptor, name = tempfile.mkstemp(prefix="agent_scheduled_state_", suffix=".json", dir=PROJECT_DIR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("本节调度记录：", name, flush=True)
    return Path(name)


async def local_demo(repeat=False):
    print("本地演示：真实计时、自动执行及计算，没有模型或 API。", flush=True)
    manager = ScheduledTasks([137, 245, 18], delay_seconds=1.0 if repeat else 3.0, max_jobs=2 if repeat else 1)
    results = []
    try:
        ids = []
        # 有限次数，不用持续循环：重复概念只预先安排两次。
        for occurrence in range(1, 3 if repeat else 2):
            started = manager.schedule(manager.numbers, manager.delay_seconds * occurrence)
            ids.append(started["task"]["task_id"])
            print("已安排：", started["task"]["task_id"], "；延迟秒：", started["task"]["delay_seconds"], flush=True)
        await asyncio.sleep(0.2)
        for task_id in ids:
            observed = manager.get_status(task_id)
            print("到期前检查：", observed["task"]["status"], "；started_at：", observed["task"]["started_at"], flush=True)
        for task_id in ids:
            result = await manager.get_result(task_id, wait=True)
            results.append(result)
            job = manager.jobs[task_id]
            print("结果：", json.dumps(result, ensure_ascii=False), flush=True)
            print("实际启动距离提交秒：", round(job.started_at - job.created_at, 4), flush=True)
        passed = all(r["ready"] and r["task"]["result"]["sum"] == 400 and
                     r["task"]["started_at"] >= r["task"]["run_at"] for r in results)
        passed = passed and all(e["task"]["status"] == "scheduled" and e["task"]["started_at"] is None
                                for e in manager.observations if e["at"] < e["task"]["run_at"])
    finally:
        await manager.close()
    save_record(manager, verified=passed)
    print("本地定时流程验证：", "通过" if passed else "未通过", flush=True)
    if repeat:
        print("重复概念仅运行两次，没有继续安排下一次。", flush=True)
    return 0 if passed else 1


async def main(demo=False, repeat=False):
    if demo:
        return await local_demo(repeat=repeat)
    api_key, base_url = (os.environ.get(n, "").strip() for n in ("OPENAI_API_KEY", "OPENAI_BASE_URL"))
    missing = [n for n, v in (("OPENAI_API_KEY", api_key), ("OPENAI_BASE_URL", base_url)) if not v]
    if missing:
        print("配置错误：请在已设置环境变量的 Terminal 中运行；缺少 " + "、".join(missing), file=sys.stderr)
        return 1
    if base_url.rstrip("/") != "https://api.likeai520.cc/v1":
        print("配置错误：本节只允许已授权的中转站地址。", file=sys.stderr)
        return 1
    manager = ScheduledTasks([secrets.randbelow(400) + 100 for _ in range(3)])
    recorder = ScheduledRecorder()
    verified = False
    task = f"请将人工整数 {json.dumps(manager.numbers)} 的汇总安排在提交 {manager.delay_seconds} 秒后执行，查询一次状态，再取得结果并报告是否提前执行。"
    print("Scheduled Tasks 验证：安排未来执行 → 查询 → 自动到期执行 → 取结果 → 报告。", flush=True)
    print("最多四次模型请求、三次工具处理，无自动重试；只安排一次本地短任务。", flush=True)
    print("人工任务：", task, flush=True)
    try:
        async with AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0) as client:
            learning_agent = Agent(name="Scheduled Learning Agent", instructions=INSTRUCTIONS,
                model=OpenAIResponsesModel(model="gpt-6-sol", openai_client=client),
                model_settings=ModelSettings(max_tokens=MAX_OUTPUT_TOKENS, reasoning=Reasoning(effort="none"),
                    store=False, tool_choice="auto", parallel_tool_calls=False),
                tools=list(create_scheduled_tools(manager)), tool_use_behavior="run_llm_again")
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                result = await Runner.run(learning_agent, task, max_turns=MAX_MODEL_REQUESTS,
                    hooks=recorder, run_config=RunConfig(tracing_disabled=True))
        print("Agent 最终报告：", result.final_output, flush=True)
        checks = verify_run(result, recorder, manager)
        for name, passed in checks.items():
            print(f"{name}：{'通过' if passed else '未通过'}", flush=True)
        verified = bool(checks) and all(checks.values())
    except (LoopStopped, MaxTurnsExceeded) as error:
        print(f"本节已停止：{error}", file=sys.stderr)
    except TimeoutError:
        print("流程总时限已到，没有自动继续。", file=sys.stderr)
    except APITimeoutError:
        print("请求等待超时；请核对平台账单，没有自动重试。", file=sys.stderr)
    except APIStatusError as error:
        detail = re.sub(r"sk-[A-Za-z0-9_-]+", "[已隐藏]", str(error.message).replace(api_key, "[已隐藏]"))
        print(f"中转站返回 HTTP {error.status_code}：{detail}", file=sys.stderr)
    except APIConnectionError as error:
        print(f"连接异常：{type(error).__name__}。", file=sys.stderr)
    except Exception as error:
        print(f"本地或接口异常：{type(error).__name__}。", file=sys.stderr)
    finally:
        await manager.close()
    try:
        save_record(manager, recorder, verified)
    except OSError as error:
        verified = False
        print(f"记录保存失败：{type(error).__name__}。", file=sys.stderr)
    print("Scheduled Tasks 定时任务验证：", "通过" if verified else "未通过", flush=True)
    if not verified:
        print("没有自动重试、追加请求或遗留计时器。")
    print_usage(recorder)
    return 0 if verified else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scheduled Tasks 课程，默认进行已授权的真实模型验证。")
    parser.add_argument("--demo", action="store_true", help="本地无模型演示。")
    parser.add_argument("--repeat-demo", action="store_true", help="仅配合 --demo 演示两次有限重复。")
    args = parser.parse_args()
    if args.repeat_demo and not args.demo:
        parser.error("--repeat-demo 只能与 --demo 一起使用，不调用模型。")
    raise SystemExit(asyncio.run(main(demo=args.demo, repeat=args.repeat_demo)))
