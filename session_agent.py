"""本节：对比两轮独立请求与两轮共享 Session，观察实际模型输入。"""

import asyncio
import copy
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass

from agents import Agent, MaxTurnsExceeded, ModelSettings, OpenAIResponsesModel, RunConfig, Runner, SQLiteSession
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.types.shared import Reasoning

from agent import MAX_OUTPUT_TOKENS, LoopRecorder, LoopStopped, print_usage


MAX_MODEL_REQUESTS = 4
INSTRUCTIONS = (
    "你是 Session 课程的练习助手。用户提供两个整数和练习标记时，只回答：已记录。"
    "用户询问上一轮时，只依据当前可见对话回答，不猜测缺失数据。"
    "如果对话中没有上一轮的两个整数或练习标记，只回答：信息不足。"
    "如果有，只输出一个 JSON 对象，sum 为两整数相加的结果（整数），"
    "marker 为原样的练习标记（字符串）。不使用 Markdown，不输出其他文字。"
)
SECOND_TASK = "请计算上一轮两个整数的和，并返回上一轮的练习标记。"


@dataclass(frozen=True)
class Example:
    a: int
    b: int
    marker: str

    @property
    def first_task(self):
        return f"本次练习的两个整数是 {self.a} 和 {self.b}，练习标记是 {self.marker}。请先确认收到。"

    @property
    def expected_answer(self):
        return {"sum": self.a + self.b, "marker": self.marker}


def message_text(item):
    """把 SDK 消息中的文本取出，保留非文本条目的原始记录供核对。"""
    content = item.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


class SessionRecorder(LoopRecorder):
    def __init__(self):
        super().__init__(max_model_requests=MAX_MODEL_REQUESTS, max_tool_calls=0)
        self.contexts = []
        self.instructions = []

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        await super().on_llm_start(context, agent, system_prompt, input_items)
        # 观察 SDK 此刻实际交给模型的历史，不能只凭回答判断“记住了”。
        self.contexts.append(copy.deepcopy(input_items))
        self.instructions.append(system_prompt)
        print("本轮可见对话：", flush=True)
        for item in input_items:
            if item.get("role") in ("user", "assistant"):
                print(f"  {item['role']}: {message_text(item)}", flush=True)
            else:
                print(f"  非文本条目：{item.get('type', 'unknown')}", flush=True)

    async def on_llm_end(self, context, agent, response):
        await super().on_llm_end(context, agent, response)
        if any(getattr(item, "type", "") == "function_call" for item in response.output):
            raise LoopStopped("本节没有开放工具，平台返回了意外工具请求；已停止。")


async def run_comparison(learning_agent, recorder, example):
    results = []
    config = RunConfig(tracing_disabled=True)
    for number, task in enumerate((example.first_task, SECOND_TASK), 1):
        print(f"\n没有 Session：第 {number} 轮任务", flush=True)
        # 每次仅提交当前任务；上一轮结果不会自动成为下一轮输入。
        result = await Runner.run(learning_agent, task, max_turns=1, hooks=recorder, run_config=config)
        results.append(result)
        print("Agent 回答：", result.final_output, flush=True)

    # :memory: 表示历史仅保存在本次进程内，不生成数据库文件。
    session = SQLiteSession("session_lesson", db_path=":memory:")
    histories = []
    try:
        for number, task in enumerate((example.first_task, SECOND_TASK), 1):
            print(f"\n有 Session：第 {number} 轮任务", flush=True)
            # 两轮传入同一个对象，SDK 自动读取旧历史并保存本轮新条目。
            result = await Runner.run(learning_agent, task, session=session,
                                     max_turns=1, hooks=recorder, run_config=config)
            results.append(result)
            histories.append(await session.get_items())
            print("Agent 回答：", result.final_output, flush=True)
            print("Session 已保存条目数：", len(histories[-1]), flush=True)
    finally:
        session.close()
    return results, histories


def verify_comparison(results, histories, recorder, example):
    """同时核对实际上下文、Session 存储和模型答案。"""
    checks = {}
    if len(results) != 4 or len(histories) != 2 or len(recorder.contexts) != 4:
        return {"完整四次请求": False}
    first = [{"role": "user", "content": example.first_task}]
    second = [{"role": "user", "content": SECOND_TASK}]
    checks["没有 Session：第二轮只收到新任务"] = recorder.contexts[0] == first and recorder.contexts[1] == second
    checks["有 Session：第一轮保存了真实用户输入和模型输出"] = (
        recorder.contexts[2] == first and histories[0] == results[2].to_input_list()
        and any(item.get("role") == "assistant" for item in histories[0])
    )
    checks["有 Session：第二轮确实带入完整上一轮历史"] = recorder.contexts[3] == histories[0] + second
    checks["有 Session：第二轮新条目保存一次"] = histories[1] == results[3].to_input_list()
    checks["两组使用相同指令，答案未通过指令或第二轮任务泄漏"] = (
        recorder.instructions == [INSTRUCTIONS] * 4
        and example.marker not in INSTRUCTIONS and example.marker not in SECOND_TASK
    )
    checks["没有 Session：模型明确表示信息不足"] = results[1].final_output.strip().rstrip("。.!！") == "信息不足"
    try:
        answer = json.loads(results[3].final_output)
        correct = answer == example.expected_answer and type(answer.get("sum")) is int
    except (TypeError, ValueError, AttributeError):
        correct = False
    checks["有 Session：模型正确返回和值及随机标记"] = correct
    checks["请求次数为四次，没有执行工具"] = (
        len(recorder.responses) == 4 and not recorder.tool_calls
        and all(len(result.raw_responses) == 1 for result in results)
    )
    return checks


async def main() -> int:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    missing = [name for name, value in (("OPENAI_API_KEY", api_key), ("OPENAI_BASE_URL", base_url)) if not value]
    if missing:
        print("配置错误：请设置环境变量 " + "、".join(missing), file=sys.stderr)
        return 1
    if base_url.rstrip("/") != "https://api.likeai520.cc/v1":
        print("配置错误：本节只允许使用已授权的中转站地址。", file=sys.stderr)
        return 1

    example = Example(secrets.randbelow(900) + 100, secrets.randbelow(900) + 100, "lesson-" + secrets.token_hex(6))
    recorder = SessionRecorder()
    verified = False
    print("Session / Context 对比：两组各两轮任务，最多四次模型请求。", flush=True)
    try:
        async with AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=30.0, max_retries=0) as client:
            learning_agent = Agent(
                name="Session Learning Agent", instructions=INSTRUCTIONS,
                model=OpenAIResponsesModel(model="gpt-6-sol", openai_client=client),
                model_settings=ModelSettings(max_tokens=MAX_OUTPUT_TOKENS,
                    reasoning=Reasoning(effort="none"), store=False),
                tools=[],
            )
            async with asyncio.timeout(140):
                results, histories = await run_comparison(learning_agent, recorder, example)
            checks = verify_comparison(results, histories, recorder, example)
            for name, ok in checks.items():
                print(f"{name}：{'通过' if ok else '未通过'}", flush=True)
            verified = all(checks.values())
            print("本地核对的预期答案：", json.dumps(example.expected_answer, ensure_ascii=False), flush=True)
    except (LoopStopped, MaxTurnsExceeded) as error:
        print(f"本节已停止：{error}", file=sys.stderr)
    except TimeoutError:
        print("本节已停止：超过 140 秒总时限。", file=sys.stderr)
    except APIStatusError as error:
        detail = re.sub(r"sk-[A-Za-z0-9_-]+", "[已隐藏]", str(error.message).replace(api_key, "[已隐藏]"))
        print(f"中转站返回 HTTP {error.status_code}：{detail}", file=sys.stderr)
    except APIConnectionError as error:
        print(f"连接或超时异常：{type(error).__name__}。", file=sys.stderr)
    except Exception as error:
        print(f"本地或接口异常：{type(error).__name__}。", file=sys.stderr)

    print("Session / Context 对比验证：", "通过" if verified else "未通过")
    if not verified:
        print("已停止，没有自动重试或重新运行对比。")
    print_usage(recorder)
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
