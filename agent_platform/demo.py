"""可预测的离线模型替身；实际工具调用仍由 OpenAI Agents SDK 执行。"""

import ast
import json
import re
import secrets

from agents import ModelResponse, Usage
from agents.models.interface import Model
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText


def decode_output(item):
    value = item["output"]
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return ast.literal_eval(value)


def tool(name, arguments):
    return ModelResponse(output=[ResponseFunctionToolCall(type="function_call", name=name,
        call_id="platform-" + secrets.token_hex(6), arguments=json.dumps(arguments, ensure_ascii=False))],
        usage=Usage(input_tokens=1, output_tokens=1), response_id=None)


def answer(text):
    return ModelResponse(output=[ResponseOutputMessage(type="message", role="assistant",
        id="platform-message-" + secrets.token_hex(6), status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])])],
        usage=Usage(input_tokens=1, output_tokens=1), response_id=None)


def current_turn(items):
    start = max(i for i, item in enumerate(items) if item.get("role") == "user")
    task = items[start]["content"]
    if not isinstance(task, str):
        task = " ".join(x.get("text", "") for x in task)
    outputs = [x for x in items[start + 1:] if x.get("type") == "function_call_output"]
    return task, outputs


class DemoModel(Model):
    async def get_response(self, system_instructions, input, *args, **kwargs):
        task, outputs = current_turn(input)
        count = len(outputs)
        if "修复测试样例" in task:
            if count == 0:
                return tool("read_file", {"path": "calculator.py"})
            if count == 1:
                return tool("terminal", {"command": ["python", "-m", "unittest", "calculator_tests", "-v"]})
            if count == 2:
                before = decode_output(outputs[0])["content"]
                return tool("write_file", {"path": "calculator.py", "content": before.replace("return a - b", "return a + b"), "expected_content": before})
        elif "运行样例测试" in task and count == 0:
            return tool("terminal", {"command": ["python", "-m", "unittest", "calculator_tests", "-v"]})
        elif "检查 practice.py" in task:
            if count == 0:
                return tool("load_skill", {"name": "check-python-output"})
            if count == 1:
                return tool("read_file", {"path": "practice.py"})
            if count == 2:
                return tool("terminal", {"command": ["python", "practice.py"]})
        elif re.search(r"note.txt.*状态.*(completed|pending)", task):
            if count == 0:
                return tool("read_file", {"path": "note.txt"})
            if count == 1:
                before = decode_output(outputs[0])["content"]
                status = re.search(r"状态.*(completed|pending)", task)[1]
                after = re.sub(r"(?m)^status: .*?$", "status: " + status, before)
                return tool("write_file", {"path": "note.txt", "content": after, "expected_content": before})
        elif count == 0:
            match = re.fullmatch(r"记住[：:]\s*(.{1,160})", task.strip())
            if match:
                return tool("write_memory", {"value": match[1]})
            if "偏好" in task or "记忆" in task:
                return tool("read_memory", {})
            if "委派" in task and "report.json" in task:
                return tool("delegate_summary", {"path": "report.json"})
            match = re.search(r"上午\s*(-?\d+).*下午\s*(-?\d+).*目标\s*(-?\d+)", task)
            if match:
                return tool("check_goal", dict(zip(("morning", "afternoon", "target"), map(int, match.groups()))))
            if "查看任务" in task:
                return tool("list_tasks", {})
            if "汇总" in task:
                numbers = [int(x) for x in re.findall(r"-?\d+", task.split("汇总", 1)[1])]
                delay = re.search(r"(\d+(?:\.\d+)?)\s*秒后", task)
                return tool("schedule_task", {"numbers": numbers, "delay_seconds": float(delay[1])}) if delay else tool("start_background", {"numbers": numbers})
            match = re.search(r"等待任务\s+((?:bg|scheduled)-[a-f0-9]+)", task)
            if match:
                return tool("get_task_result", {"task_id": match[1]})
            match = re.search(r"读取\s*(note.txt|practice.py|report.json|calculator.py|calculator_tests.py)", task)
            if match:
                return tool("read_file", {"path": match[1]})
            if "目录" in task or "路径" in task or task.strip() in ("ls", "pwd"):
                return tool("terminal", {"command": ["pwd" if "路径" in task or "pwd" in task else "ls"]})
            match = re.search(r"(-?\d+)\s*\+\s*(-?\d+)", task)
            if match:
                return tool("add", {"a": int(match[1]), "b": int(match[2])})
            match = re.search(r"继续加\s*(-?\d+)", task)
            if match:
                previous = [decode_output(x) for x in input if x.get("type") == "function_call_output"]
                value = next((x for x in reversed(previous) if type(x) is int), None)
                if value is None:
                    summaries = [x.get("content", "") for x in input if x.get("role") == "user"]
                    facts = re.findall(r"最近加法结果=(-?\d+)", " ".join(x for x in summaries if isinstance(x, str)))
                    value = int(facts[-1]) if facts else None
                if value is not None:
                    return tool("add", {"a": value, "b": int(match[1])})
        if outputs:
            result = decode_output(outputs[-1])
            if isinstance(result, dict) and result.get("status") == "awaiting_approval":
                return answer("修改已准备好，等待你确认 Diff；文件尚未写入。")
            if "检查 practice.py" in task:
                return answer(json.dumps({"skill": "check-python-output", "path": "practice.py",
                    "returncode": result["returncode"], "expected_stdout": "95\n", "actual_stdout": result["stdout"],
                    "verdict": "通过" if result["returncode"] == 0 and result["stdout"] == "95\n" and not result["stderr"] else "未通过"}, ensure_ascii=False))
            if isinstance(result, dict) and "goal" in result:
                report = result["report"]
                return answer(f'目标核对：总计 {report["total"]}，目标 {report["target"]}，' +
                              ("已达标" if report["met"] else "未达标") + "；四步已按证据验收。")
            return answer("本地模拟决策；实际工具结果：\n" + json.dumps(result, ensure_ascii=False, indent=2))
        return answer("本地模拟支持 /help 中的示例句式。任意自然语言推理需要另行授权真实模型模式。")

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError("v1.0 使用工具事件展示进度。")
        yield


class ChildDemoModel(DemoModel):
    async def get_response(self, system_instructions, input, *args, **kwargs):
        task, outputs = current_turn(input)
        if not outputs:
            path = re.search(r"人工数据文件 (\S+)，", task)[1]
            return tool("read_file", {"path": path})
        data = json.loads(decode_output(outputs[0])["content"])
        if len(outputs) == 1:
            return tool("sum_numbers", {"numbers": data["numbers"]})
        summary = {"path": decode_output(outputs[0])["path"],
                   **{k: decode_output(outputs[1])[k] for k in ("total", "count", "dataset", "receipt")}}
        return answer(json.dumps(summary, ensure_ascii=False))
