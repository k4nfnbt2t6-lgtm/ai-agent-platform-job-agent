"""后台任务课：真实本地 worker + 主 Agent 继续决策 + 状态与结果核对。"""

import argparse
import asyncio
import copy
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
from background_tools import BackgroundTasks, create_background_tools
from tools import PROJECT_DIR


MAX_MODEL_REQUESTS = 5
MAX_TOOL_CALLS = 4
REQUEST_TIMEOUT_SECONDS = 120.0
TOTAL_TIMEOUT_SECONDS = 660
INSTRUCTIONS = (
    "你是 Background Tasks 课程助手。先启动任务指定的后台汇总，拿到任务编号；"
    "不要等待结果，继续调用 add 完成独立加法；然后查询一次任务状态，再取得后台结果。"
    "每轮一个工具；只启动一次、查询一次、取结果一次，不重复轮询或重试。"
    "每次使用工具实际返回的 task_id。工具失败就停止，不编造状态或结果。"
    "最终只输出 JSON：task_id、status、background_sum、count、receipt、foreground_sum。"
    "receipt 必须来自后台结果工具，其他字段同样来自实际结果；状态应为 completed。"
    "后台可能在等待模型回答时完成，如实使用工具记录，不声称前台加法一定早于后台结束。"
)


class BackgroundRecorder(LoopRecorder):
    def __init__(self, manager):
        super().__init__(max_model_requests=MAX_MODEL_REQUESTS, max_tool_calls=MAX_TOOL_CALLS)
        self.manager = manager
        self.decisions = []

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        await super().on_llm_start(context, agent, system_prompt, input_items)
        event = {"turn": len(self.model_inputs), "at": asyncio.get_running_loop().time(),
                 "tasks": [job.snapshot() for job in self.manager.jobs.values()]}
        self.decisions.append(event)
        if event["tasks"]:
            print("主 Agent 继续发起模型请求；此时后台状态：", event["tasks"][0]["status"], flush=True)

    async def on_tool_end(self, context, agent, tool, result):
        await super().on_tool_end(context, agent, tool, result)
        print("工具实际返回：", json.dumps(result, ensure_ascii=False), flush=True)
        if not isinstance(result, dict) or not result.get("ok"):
            raise LoopStopped("后台工具失败或请求被拒绝，已停止，没有自动重试。")
        if tool.name == "add":
            print("独立加法执行时后台状态：", result["background_status"], flush=True)


def verify_run(result, recorder, manager):
    records = recorder.tool_calls
    expected_names = ["start_background", "add", "get_task_status", "get_task_result"]
    if len(records) != 4 or [r["name"] for r in records] != expected_names or len(manager.jobs) != 1:
        return {"模型实际启动、处理独立工作、查询和取结果": False}
    job = next(iter(manager.jobs.values()))
    returned_task = records[3]["output"].get("task", {})
    expected = {"task_id": job.task_id, "status": "completed", "background_sum": sum(manager.numbers),
                "count": len(manager.numbers), "receipt": (job.result or {}).get("receipt"),
                "foreground_sum": sum(manager.foreground_numbers)}
    try:
        final = json.loads(result.final_output)
    except (TypeError, ValueError):
        final = None
    second = recorder.decisions[1] if len(recorder.decisions) > 1 else {}
    # 观察模型请求开始与 worker 结束的实际时序，而不假定 API 响应耗时。
    overlap = bool(second.get("tasks") and second["tasks"][0]["status"] == "running"
                   and job.started_at <= second["at"] < job.finished_at)
    outputs = {item.call_id: item.raw_item["output"] for item in result.new_items if isinstance(item, ToolCallOutputItem)}
    checks = {
        "模型实际启动、处理独立工作、查询和取结果": [r["turn"] for r in records] == [1, 2, 3, 4],
        "提交立即返回 pending，没有等后台完成": (
            records[0]["output"]["task"]["status"] == "pending"
            and records[0]["output"]["task"]["result"] is None),
        "主 Agent 在后台 running 时已继续发起下一轮决策": overlap,
        "后台真实经历 pending → running → completed": [s["status"] for s in job.history] == ["pending", "running", "completed"],
        "主 Agent 的独立加法真实执行且没有等待后台接口": (
            len(manager.foreground_events) == 1 and records[1]["output"]["result"] == expected["foreground_sum"]),
        "状态查询和取结果使用真实同一个任务编号": all(
            r["arguments"] == {"task_id": job.task_id} and r["output"]["task"]["task_id"] == job.task_id for r in records[2:]),
        "后台结果包含正确汇总及实际生成的回执": (
            job.status == "completed" and records[3]["output"].get("ready") is True
            and returned_task.get("result") == job.result
            and job.result["sum"] == expected["background_sum"] and job.result["count"] == expected["count"]),
        "模型最终报告与两个真实计算结果及回执一致": (
            isinstance(final, dict) and final == expected
            and type(final.get("background_sum")) is int and type(final.get("foreground_sum")) is int
            and type(final.get("count")) is int),
        "实际工具返回值都进入后续请求": (
            len(outputs) == 4 and len(recorder.model_inputs) == 5
            and all(recorder.model_inputs[i].get(r["call_id"]) == outputs.get(r["call_id"])
                    and r["call_id"] in outputs for i, r in enumerate(records, start=1))),
        "五次模型请求、四次工具执行，没有额外调用": len(result.raw_responses) == len(recorder.responses) == 5,
    }
    return checks


def save_record(manager, recorder=None, verified=False):
    payload = manager.snapshot() | {"verified": verified, "model_decisions": [] if recorder is None else copy.deepcopy(recorder.decisions)}
    if recorder:
        payload["usage"] = {"requests": len(recorder.model_inputs), "tool_calls": len(recorder.tool_calls),
                            "input_tokens": sum(r.usage.input_tokens for r in recorder.responses),
                            "output_tokens": sum(r.usage.output_tokens for r in recorder.responses),
                            "warnings": copy.deepcopy(recorder.usage_warnings)}
    descriptor, name = tempfile.mkstemp(prefix="agent_background_state_", suffix=".json", dir=PROJECT_DIR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("本节后台任务记录：", name, flush=True)
    return Path(name)


async def local_demo():
    print("本地演示：真实后台计算和前台加法；没有模型或 API。", flush=True)
    manager = BackgroundTasks([137, 245, 18], 37, 58)
    try:
        started = manager.start(manager.numbers)
        task_id = started["task"]["task_id"]
        print("提交时：", started["task"]["status"], flush=True)
        await asyncio.sleep(0)
        print("执行中：", manager.get_status(task_id)["task"]["status"], flush=True)
        front = manager.add(37, 58)
        print("前台计算：", front["result"], "；此时后台：", front["background_status"], flush=True)
        result = await manager.get_result(task_id, wait=True)
        print("完成后：", json.dumps(result, ensure_ascii=False), flush=True)
        passed = front["background_status"] == "running" and result["ready"] and result["task"]["result"]["sum"] == 400
    finally:
        await manager.close()
    save_record(manager, verified=passed)
    print("本地后台并行流程验证：", "通过" if passed else "未通过", flush=True)
    return 0 if passed else 1


async def main(demo=False):
    if demo:
        return await local_demo()
    api_key, base_url = (os.environ.get(n, "").strip() for n in ("OPENAI_API_KEY", "OPENAI_BASE_URL"))
    missing = [n for n, v in (("OPENAI_API_KEY", api_key), ("OPENAI_BASE_URL", base_url)) if not v]
    if missing:
        print("配置错误：请在已设置环境变量的 Terminal 中运行；缺少 " + "、".join(missing), file=sys.stderr)
        return 1
    if base_url.rstrip("/") != "https://api.likeai520.cc/v1":
        print("配置错误：本节只允许使用已授权的中转站地址。", file=sys.stderr)
        return 1
    numbers = [secrets.randbelow(400) + 100 for _ in range(3)]
    manager = BackgroundTasks(numbers, 37, 58)
    recorder = BackgroundRecorder(manager)
    verified = False
    task = (f"启动后台任务汇总人工整数 {json.dumps(numbers)}。"
            "任务运行时继续处理独立工作：用 add 计算 37 + 58。"
            "随后查询一次后台状态，取得结果，报告两项工作的结果及后台任务回执。")
    print("Background Tasks 验证：启动 → 独立工作 → 查询状态 → 取结果 → 报告。", flush=True)
    print("安全 worker 约 4 秒结束；最多五次模型请求、四次工具执行，无自动重试。", flush=True)
    print("人工任务：", task, flush=True)
    try:
        async with AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0) as client:
            learning_agent = Agent(name="Background Learning Agent", instructions=INSTRUCTIONS,
                model=OpenAIResponsesModel(model="gpt-6-sol", openai_client=client),
                model_settings=ModelSettings(max_tokens=MAX_OUTPUT_TOKENS, reasoning=Reasoning(effort="none"),
                    store=False, tool_choice="auto", parallel_tool_calls=False),
                tools=list(create_background_tools(manager)), tool_use_behavior="run_llm_again")
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                result = await Runner.run(learning_agent, task, max_turns=MAX_MODEL_REQUESTS,
                    hooks=recorder, run_config=RunConfig(tracing_disabled=True))
        print("Agent 最终报告：", result.final_output, flush=True)
        checks = verify_run(result, recorder, manager)
        for name, passed in checks.items():
            print(f"{name}：{'通过' if passed else '未通过'}", flush=True)
        verified = bool(checks) and all(checks.values())
        if manager.foreground_events:
            front = manager.foreground_events[0]
            if front["background_status"] == "completed":
                print("时序说明：主 Agent 发起第二轮决策时后台仍在运行；加法工具执行前后台已完成，未伪造重叠。")
    except (LoopStopped, MaxTurnsExceeded) as error:
        print(f"本节已停止：{error}", file=sys.stderr)
    except TimeoutError:
        print("本节总时限已到，未自动继续。", file=sys.stderr)
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
    print("Background Tasks 后台任务验证：", "通过" if verified else "未通过", flush=True)
    if not verified:
        print("没有自动重试、追加请求或遗留后台 worker。")
    print_usage(recorder)
    return 0 if verified else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Background Tasks 课程，默认进行已授权的真实模型验证。")
    parser.add_argument("--demo", action="store_true", help="本地演示，不使用模型或凭据。")
    raise SystemExit(asyncio.run(main(demo=parser.parse_args().demo)))
