"""共同运行入口：界面只提交任务、订阅事件；复用 SDK Loop 和已有工具。"""

import ast
import asyncio
import contextlib
import io
import json
import os
import re
import threading
from dataclasses import dataclass

from agents import Agent, ModelResponse, ModelSettings, OpenAIResponsesModel, RunConfig, Runner, Usage
from agents.models.interface import Model
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
from openai.types.shared import Reasoning

from agent import LoopRecorder, LoopStopped, MAX_OUTPUT_TOKENS
from tools import add, terminal


INSTRUCTIONS = (
    "你是界面课程中的安全练习 Agent。只使用 add 计算整数加法，或 terminal 查看当前项目目录。"
    "terminal 仅允许 pwd、ls、ls .；其他操作直接说明不支持，不尝试绕过。"
    "每轮一个工具，取得真实结果后再行动；不得编造输出。简短回答最终结果和依据。"
    "每个任务最多两个工具；无需工具时直接回答。"
)
DEMO_TASK = "请用工具计算 37 + 58。"


@dataclass(frozen=True)
class Event:
    kind: str
    text: str


class DisplayRecorder(LoopRecorder):
    """原费用/循环检查不变，把原日志转成界面事件。"""

    def __init__(self, emit):
        super().__init__(max_model_requests=6, max_tool_calls=4)
        self.emit = emit

    async def forward(self, method, *args):
        # 原回调只做本地检查与打印，不包含网络等待；不重写其预算逻辑。
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                await method(*args)
        finally:
            for line in buffer.getvalue().splitlines():
                self.emit("log", line)

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        await self.forward(super().on_llm_start, context, agent, system_prompt, input_items)
        self.emit("status", "思考中")

    async def on_llm_end(self, context, agent, response):
        await self.forward(super().on_llm_end, context, agent, response)

    async def on_tool_start(self, context, agent, tool):
        await self.forward(super().on_tool_start, context, agent, tool)
        self.emit("status", "执行工具：" + tool.name)

    async def on_tool_end(self, context, agent, tool, result):
        await self.forward(super().on_tool_end, context, agent, tool, result)
        self.emit("tool_result", tool.name + " → " + json.dumps(result, ensure_ascii=False))


class DemoModel(Model):
    """无网络的模拟决策；实际工具仍由同一个 Runner 执行。"""

    async def get_response(self, system_instructions, input, *args, **kwargs):
        await asyncio.sleep(0.15)  # 让界面状态可观察，非实际模型延迟。
        outputs = [x for x in input if x.get("type") == "function_call_output"]
        if outputs:
            text = "本地模拟决策；真实工具结果：" + outputs[-1]["output"]
        else:
            task = input[0]["content"]
            match = re.search(r"(-?\d+)\s*\+\s*(-?\d+)", task)
            if match:
                name, arguments = "add", {"a": int(match[1]), "b": int(match[2])}
            elif "目录" in task or "pwd" in task or "ls" in task:
                name, arguments = "terminal", {"command": ["pwd" if "pwd" in task or "路径" in task else "ls"]}
            else:
                name = None
                text = "本地演示仅支持整数加法（例如 37 + 58）或查看当前项目目录。"
            if name:
                return ModelResponse(output=[ResponseFunctionToolCall(
                    type="function_call", name=name, call_id="demo-call",
                    arguments=json.dumps(arguments),
                )], usage=Usage(input_tokens=1, output_tokens=1), response_id=None)
        return ModelResponse(output=[ResponseOutputMessage(
            type="message", role="assistant", id="demo-message", status="completed",
            content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
        )], usage=Usage(input_tokens=1, output_tokens=1), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError("本节展示事件，不使用逐 token 流式输出。")
        yield  # 保持 Model 的异步迭代接口。


class AgentRuntime:
    def __init__(self, emit, demo=True):
        self.demo = demo
        self.api_key = "" if demo else os.environ.get("OPENAI_API_KEY", "").strip()
        self.base_url = "" if demo else os.environ.get("OPENAI_BASE_URL", "").strip()
        if not demo:
            missing = [n for n, v in (("OPENAI_API_KEY", self.api_key), ("OPENAI_BASE_URL", self.base_url)) if not v]
            if missing:
                raise ValueError("请在已设置环境变量的原 Terminal 中运行；缺少 " + "、".join(missing))
            if self.base_url.rstrip("/") != "https://api.likeai520.cc/v1":
                raise ValueError("本节仅允许已授权中转站地址。")
        self.sink = emit
        self.recorder = DisplayRecorder(self.emit)
        self.agent = None

    def emit(self, kind, text):
        text = str(text)
        if self.demo and kind == "log":
            text = "[模拟用量，不计费] " + text.replace("平台", "模拟模型")
        if self.api_key:
            text = text.replace(self.api_key, "[已隐藏]")
        text = re.sub(r"sk-[A-Za-z0-9_-]+", "[已隐藏]", text)
        text = "".join(c for c in text if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)
        self.sink(Event(kind, text))

    def usage(self):
        r = self.recorder
        return {"requests": len(r.model_inputs), "tools": len(r.tool_calls),
                "input_tokens": sum(x.usage.input_tokens for x in r.responses),
                "output_tokens": sum(x.usage.output_tokens for x in r.responses),
                "estimated_rmb": 0 if self.demo else r.spent_usd() * 10,
                "warnings": len(r.usage_warnings)}

    async def run_task(self, task):
        """TUI / GUI 的唯一 Agent 入口；每项任务独立，不注入界面布局。"""
        try:
            if not isinstance(task, str) or not task.strip():
                raise ValueError("任务不能为空。")
            if (self.api_key and self.api_key in task) or re.search(r"sk-[A-Za-z0-9_-]{12,}", task):
                raise ValueError("不要在任务中输入密钥；使用环境变量。")
            self.emit("status", "开始任务")
            if self.demo:
                result = await self.execute(DemoModel(), task.strip())
            else:
                async with AsyncOpenAI(api_key=self.api_key, base_url=self.base_url,
                                       timeout=120.0, max_retries=0) as client:
                    result = await self.execute(OpenAIResponsesModel(model="gpt-6-sol", openai_client=client), task.strip())
            self.emit("final", result.final_output)
            self.emit("status", "已完成")
            return result
        except asyncio.CancelledError:
            self.emit("status", "已取消；已发送请求的费用需查看账单")
            raise
        except (ValueError, LoopStopped) as error:
            self.emit("error", str(error))
        except APIStatusError as error:
            self.emit("error", f"中转站返回 HTTP {error.status_code}，停止且不重试。")
        except APIConnectionError as error:
            self.emit("error", type(error).__name__ + "：连接或超时异常，停止且不重试；计费请查账单。")
        except TimeoutError:
            self.emit("error", "超过单任务 400 秒时限，停止且不重试；计费请查账单。")
        except Exception as error:
            self.emit("error", type(error).__name__ + "：本次运行未完成，停止且不重试。")
        self.emit("status", "已停止")
        return None

    async def execute(self, model, task):
        self.agent = Agent(name="UI Learning Agent", instructions=INSTRUCTIONS, model=model,
            tools=[add, terminal], model_settings=ModelSettings(max_tokens=MAX_OUTPUT_TOKENS,
                reasoning=Reasoning(effort="none"), store=False, tool_choice="auto", parallel_tool_calls=False))
        async with asyncio.timeout(400):
            return await Runner.run(self.agent, task, hooks=self.recorder, max_turns=3,
                                    run_config=RunConfig(tracing_disabled=True))


class InterfaceController:
    """提交在工作线程运行；界面线程只取快照，不执行模型或工具。"""

    def __init__(self, demo=True):
        self.lock = threading.RLock()
        self.events = []
        self.busy = False
        self.closed = False
        self.worker = None
        self.loop = self.task = None
        self.runtime = AgentRuntime(self.receive, demo=demo)
        self.receive(Event("status", "就绪：本地模拟模型，费用 0 元" if demo else "就绪：真实模型，本进程累计最多 6 次请求、4 次工具；预算 1 元"))

    def receive(self, event):
        with self.lock:
            self.events.append(event)

    def submit(self, text):
        if not isinstance(text, str) or not text.strip():
            return False, "任务不能为空。"
        with self.lock:
            if self.closed or self.busy:
                return False, "已关闭或已有任务运行，请等待；不重复提交。"
            self.busy = True
            self.worker = threading.Thread(target=self.work, args=(text,), daemon=True)
            self.worker.start()
        return True, "已提交"

    def work(self, text):
        async def perform():
            with self.lock:
                self.loop = asyncio.get_running_loop()
                self.task = asyncio.current_task()
                closed = self.closed
            if closed:
                return
            await self.runtime.run_task(text)
        try:
            asyncio.run(perform())
        except asyncio.CancelledError:
            pass
        finally:
            self.runtime.emit("usage", json.dumps(self.runtime.usage(), ensure_ascii=False))
            with self.lock:
                self.loop = self.task = None
                self.busy = False

    def snapshot(self):
        with self.lock:
            return {"busy": self.busy, "demo": self.runtime.demo,
                    "events": [{"kind": x.kind, "text": x.text} for x in self.events]}

    def close(self):
        with self.lock:
            self.closed = True
            if self.loop and self.task:
                self.loop.call_soon_threadsafe(self.task.cancel)
            worker = self.worker
        if worker:
            worker.join(timeout=5)


def verify_arithmetic(runtime, result):
    """固定 37 + 58 验收：实际 add、实际回传及最终文本都必须有证据。"""
    records = runtime.recorder.tool_calls
    if not result or len(records) != 1:
        return False
    call = records[0]
    returned = [x for x in result.to_input_list() if x.get("type") == "function_call_output"]
    return (call["name"] == "add" and call["arguments"] == {"a": 37, "b": 58}
            and call["output"] == 95 and len(runtime.recorder.responses) == 2
            and len(returned) == 1 and ast.literal_eval(returned[0]["output"]) == 95
            and runtime.recorder.model_inputs[1].get(call["call_id"]) == returned[0]["output"]
            and "95" in str(result.final_output))
