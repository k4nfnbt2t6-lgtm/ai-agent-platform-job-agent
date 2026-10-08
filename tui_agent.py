"""终端面板：键盘输入、状态、工具事件和最终结果。"""

import argparse
import asyncio
import curses
import json
import os
import sys
import tempfile
import textwrap

from tools import PROJECT_DIR
from ui_runtime import AgentRuntime, DEMO_TASK, InterfaceController, verify_arithmetic


def screen_text(window, y, text):
    height, width = window.getmaxyx()
    if 0 <= y < height:
        try:
            window.addnstr(y, 0, text, max(0, width - 1))
        except curses.error:
            pass  # 小窗口或宽字符碰到末列，下次刷新重新排版。


def draw(window, snapshot, draft):
    window.erase()
    height, width = window.getmaxyx()
    events = snapshot["events"]
    status = next((x["text"] for x in reversed(events) if x["kind"] == "status"), "就绪")
    screen_text(window, 0, "ai-agent-lab TUI | " + ("本地模拟 · 无 API" if snapshot["demo"] else "真实模型 · 累计预算 1 元"))
    screen_text(window, 1, "状态：" + status)
    screen_text(window, 2, "工具/运行事件（显示最近事件）：")
    lines = []
    for event in events:
        if event["kind"] != "status":
            for line in (f'[{event["kind"]}] {event["text"]}').splitlines():
                lines.extend(textwrap.wrap(line, max(1, (width - 2) // 2)) or [""])
    visible = max(0, height - 7)
    for y, line in enumerate(lines[-visible:] if visible else [], start=3):
        screen_text(window, y, line)
    screen_text(window, height - 3, "Enter 提交 | Esc 退出 | 例：37 + 58 / 查看当前项目目录")
    screen_text(window, height - 2, "运行中，请等待…" if snapshot["busy"] else "输入任务：" + draft)
    window.refresh()


def panel(window, controller):
    curses.curs_set(0)
    window.timeout(100)
    draft = ""
    while True:
        state = controller.snapshot()
        draw(window, state, draft)
        try:
            key = window.get_wch()
        except curses.error:
            continue
        if key == "\x1b":
            break
        if state["busy"]:
            continue
        if key in ("\n", "\r", curses.KEY_ENTER):
            accepted, note = controller.submit(draft)
            if accepted:
                draft = ""
            else:
                from ui_runtime import Event
                controller.receive(Event("error", note))
        elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
            draft = draft[:-1]
        elif isinstance(key, str) and key.isprintable():
            draft += key


async def verify_once(demo):
    """固定安全验收，不反复调用模型；无终端面板也可复现同一个核心。"""
    events = []
    runtime = AgentRuntime(lambda e: (events.append(e), print(f"[{e.kind}] {e.text}", flush=True)), demo=demo)
    result = await runtime.run_task(DEMO_TASK)
    passed = verify_arithmetic(runtime, result)
    payload = {"kind": "local_demo" if demo else "model_run", "task": DEMO_TASK,
               "verified": passed, "usage": runtime.usage(),
               "tools": runtime.recorder.tool_calls,
               "events": [{"kind": e.kind, "text": e.text} for e in events]}
    descriptor, name = tempfile.mkstemp(prefix="agent_ui_state_", suffix=".json", dir=PROJECT_DIR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("界面共同核心验证：", "通过" if passed else "未通过")
    print("累计用量/预算：", json.dumps(runtime.usage(), ensure_ascii=False))
    print("本节验证记录：", name)
    print("本地模型决策模拟，实际 SDK Loop 和 add 工具执行。" if demo else "真实模型；实际扣费以平台账单为准。")
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="使用已授权中转站；默认无收费演示")
    parser.add_argument("--verify", action="store_true", help="仅一次固定 37 + 58 验收，随后退出")
    args = parser.parse_args()
    controller = None
    try:
        if args.verify:
            return asyncio.run(verify_once(demo=not args.live))
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("TUI 需要真实终端；非交互环境请用 --verify。", file=sys.stderr)
            return 1
        controller = InterfaceController(demo=not args.live)
        curses.wrapper(panel, controller)
        controller.close()
        print("TUI 已退出；累计用量/预算：", json.dumps(controller.runtime.usage(), ensure_ascii=False))
        return 0
    except ValueError as error:
        print("配置错误：", error, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, curses.error):
        print("界面已停止。")
        return 1
    finally:
        if controller:
            controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
