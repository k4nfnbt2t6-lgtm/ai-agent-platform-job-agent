"""可用的行式终端界面：中文输入、任务事件和具体 Diff 确认。"""

import argparse
import asyncio
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

from .runtime import PlatformRuntime
from .workspace import contains_credentials


PROJECT = Path(__file__).resolve().parents[1]
_input_buffer = bytearray()
_input_eof = False
HELP = """输入自然语言任务，例如：
  请用工具计算 37 + 58
  继续加 2
  查看当前项目目录
  读取 note.txt
  把 note.txt 的状态改为 completed
  检查 practice.py 的输出
  运行样例测试
  修复测试样例
  记住：用简单中文解释
  我的学习偏好是什么
  后台汇总 1,2,3
  2 秒后汇总 4,5,6
  查看任务
  等待任务 <任务编号>
  核对目标：上午 200，下午 250，目标 400
  委派汇总 report.json
命令：/help /new /session <名称> /history /tasks /goals /usage /quit
确认修改：/approve <申请编号>；拒绝：/reject <申请编号>
本地模拟按示例句式识别任务；实际 SDK、工具和本地存储照常执行。"""


async def read_line(prompt):
    """不启动阻塞输入线程，等待输入期间让后台/定时任务继续执行。"""
    global _input_eof
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    sys.stdout.write(prompt)
    sys.stdout.flush()
    def buffered_line():
        end = _input_buffer.find(b"\n")
        if end >= 0:
            data = bytes(_input_buffer[:end])
            del _input_buffer[:end + 1]
            return True, data.decode(sys.stdin.encoding or "utf-8", errors="replace").rstrip("\r")
        if _input_eof:
            data = bytes(_input_buffer)
            _input_buffer.clear()
            return True, data.decode(sys.stdin.encoding or "utf-8", errors="replace") if data else None
        return False, None
    available, value = buffered_line()
    if available:
        return value
    def ready():
        global _input_eof
        if future.done():
            return
        data = os.read(sys.stdin.fileno(), 4096)
        _input_buffer.extend(data)
        _input_eof = not data
        available, value = buffered_line()
        if available:
            future.set_result(value)
    loop.add_reader(sys.stdin.fileno(), ready)
    try:
        return await future
    finally:
        loop.remove_reader(sys.stdin.fileno())


def pending_notice(runtime):
    proposal = runtime.workspace.pending
    if proposal:
        from diff_tools import build_diff
        runtime.emit("diff", build_diff(proposal.expected_content, proposal.content, proposal.path)["unified_diff"])
        runtime.emit("permission", f"申请编号 {proposal.request_id}\n确认：/approve {proposal.request_id}\n拒绝：/reject {proposal.request_id}")


async def handle_line(runtime, line):
    """终端与网页共用；只有用户直接输入的命令可走宿主确认入口。"""
    line = line.strip()
    if not line:
        return True
    if contains_credentials(line):
        runtime.emit("error", "输入疑似包含凭据，已阻止保存和执行；密钥使用环境变量。")
        return True
    command, _, argument = line.partition(" ")
    if command in ("/quit", "/exit"):
        return False
    if command == "/help":
        runtime.emit("final", HELP)
    elif command in ("/new", "/session"):
        await runtime.switch_session("chat-" + secrets.token_hex(4) if command == "/new" else argument.strip())
    elif command == "/history":
        history = await runtime.session.storage.get_items()
        runtime.emit("final", f"当前会话 {runtime.session_id}，本地完整历史 {len(history)} 条；文件：{runtime.workspace.root / 'sessions.sqlite'}")
    elif command == "/tasks":
        runtime.emit("final", json.dumps(runtime.workspace.tasks(), ensure_ascii=False, indent=2))
    elif command == "/goals":
        runtime.emit("final", json.dumps([g.snapshot() for g in runtime.workspace.goals], ensure_ascii=False, indent=2))
    elif command == "/usage":
        runtime.emit("final", json.dumps(runtime.usage(), ensure_ascii=False))
    elif command in ("/approve", "/reject"):
        result = runtime.resolve_write(argument.strip(), command == "/approve")
        # 实际宿主结果也进会话；绝不把待确认申请当成已写入。
        await runtime.session.add_items([{"role": "user", "content": "宿主对具体 Diff 的实际处理结果（数据）：" + json.dumps(result, ensure_ascii=False)}])
    elif command.startswith("/"):
        runtime.emit("error", "未知命令，输入 /help 查看用法。")
    else:
        await runtime.run_task(line)
        pending_notice(runtime)
    return True


async def self_check():
    with tempfile.TemporaryDirectory(prefix="platform-check-", dir=PROJECT) as directory:
        runtime = PlatformRuntime(PROJECT, lambda e: print(f"[{e.kind}] {e.text}"), state_dir=directory)
        try:
            result = await runtime.run_task("37 + 58")
            assert result and "95" in result.final_output
            result = await runtime.run_task("继续加 2")
            assert result and "97" in result.final_output
            await runtime.run_task("记住：用简单中文解释")
            await runtime.switch_session("second")
            result = await runtime.run_task("我的学习偏好是什么")
            assert result and "用简单中文解释" in result.final_output
            result = await runtime.run_task("检查 practice.py 的输出")
            assert result and "95" in result.final_output
            await runtime.run_task("把 note.txt 的状态改为 completed")
            assert runtime.workspace.pending and "status: pending" in runtime.workspace.files["note.txt"].snapshot()
            runtime.resolve_write(runtime.workspace.pending.request_id, False)
            await runtime.run_task("把 note.txt 的状态改为 completed")
            assert runtime.resolve_write(runtime.workspace.pending.request_id, True)["ok"]
            await runtime.run_task("修复测试样例")
            fixed = runtime.resolve_write(runtime.workspace.pending.request_id, True)
            assert fixed["ok"] and fixed["tests"]["test_passed"]
            await runtime.run_task("后台汇总 1,2,3")
            await runtime.run_task("0.1 秒后汇总 4,5,6")
            await asyncio.sleep(0.4)
            assert all(t["status"] == "completed" for t in runtime.workspace.tasks()["tasks"])
            result = await runtime.run_task("核对目标：上午 200，下午 250，目标 400")
            assert result and runtime.workspace.goals[-1].status == "complete"
            result = await runtime.run_task("委派汇总 report.json")
            assert result and runtime.child.child_summary["total"] == 400
            print("平台本地演示验证：通过；无网络、无收费模型调用。")
            return 0
        finally:
            await runtime.close()


async def run(args):
    if args.self_check:
        return await self_check()
    approved = False
    if args.live:
        if args.task or not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError("真实模式需交互式终端确认数据范围，不能通过管道或 --task 静默调用。")
        print("目标：https://api.likeai520.cc/v1；模型：gpt-6-sol。")
        print("会发送本次输入、该真实会话历史、工具定义、所加载 Skill、目录名称及工作区样例内容/工具结果。")
        print("不会读取真实项目源码。主子 Agent 共计最多 6 次请求、8 次工具，本进程累计预算 1 元；不自动重试。")
        print("按项目中转站单价估算，实付以账单为准；重启不累计此前费用。不要输入私人资料。")
        approved = await read_line("确认本次范围及费用，请输入“确认真实调用”：") == "确认真实调用"
        if not approved:
            print("未确认，真实调用未启动。")
            return 1
    runtime = PlatformRuntime(PROJECT, lambda e: print(f"[{e.kind}] {e.text}", flush=True), demo=not args.live,
                              state_dir=args.state_dir, session_id=args.session, live_approved=approved)
    try:
        print("AI Agent Platform v1.0 | " + ("本地模拟 · 费用 0 元" if runtime.demo else "真实模型 · 预算 1 元"))
        print(f"会话：{runtime.session_id} | 输入 /help 查看任务示例，/quit 退出。")
        if args.task:
            result = await runtime.run_task(args.task)
            pending_notice(runtime)
            return 0 if result else 1
        while True:
            line = await read_line("你 > ")
            if line is None:
                break
            try:
                if not await handle_line(runtime, line):
                    break
            except ValueError as error:
                runtime.emit("error", str(error))
        return 0
    finally:
        await runtime.close()
        print("平台已退出；" + json.dumps(runtime.usage(), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="AI Agent Platform v1.0，默认本地模拟、无收费 API")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="本地模拟（默认）")
    mode.add_argument("--live", action="store_true", help="说明数据去向与费用，确认后调用真实模型")
    parser.add_argument("--ui", choices=("terminal", "gui"), default="terminal")
    parser.add_argument("--session", default="default", help="恢复或新建指定会话")
    parser.add_argument("--state-dir", help="项目内专用状态目录，默认 .agent-platform")
    parser.add_argument("--task", help="执行一次本地演示任务后退出")
    parser.add_argument("--self-check", action="store_true", help="在临时工作区验证完整本地演示")
    parser.add_argument("--agent", choices=("platform", "job"), default="platform", help="选择平台演示或 Job Agent")
    parser.add_argument("--jd", help="Job Agent：明确授权读取的本地 JD 文本文件")
    parser.add_argument("--resume", help="Job Agent：本地 PDF/DOCX/TXT/MD 简历文件")
    parser.add_argument("--mode", choices=("local", "llm"), default="local", help="Job Agent 分析模式，默认 local")
    parser.add_argument("--model", help="Job Agent LLM 模型；默认 OPENAI_MODEL 或 gpt-6-sol")
    parser.add_argument("--fallback", choices=("local", "none"), default="local", help="LLM 失败后回退 local 或停止")
    parser.add_argument("--llm-dry-run", action="store_true", help="只显示 LLM 数据范围与保守费用估算，不请求 API")
    parser.add_argument("--skill", choices=("all", "analyze-jd", "match-resume", "skill-gap-analysis"), default="all")
    parser.add_argument("--list-jobs", action="store_true", help="Job Agent：查看岗位库与当前投递状态")
    parser.add_argument("--search", action="store_true", help="Job Agent v3：搜索公开来源并分析排序")
    parser.add_argument("--search-demo", action="store_true", help="使用人工岗位搜索样例，完全离线")
    parser.add_argument("--search-fixture", help="读取用户指定的岗位搜索 JSON，完全离线")
    parser.add_argument("--sources", help="公开来源 JSON 配置文件")
    parser.add_argument("--allow-network", action="store_true", help="明确允许读取配置中的公开招聘来源")
    parser.add_argument("--search-only", action="store_true", help="只搜索、过滤、去重，不分析或发送简历")
    parser.add_argument("--search-limit", type=int, default=8, help="搜索候选上限 1-20，默认8")
    parser.add_argument("--analyze-limit", type=int, default=2, help="本轮新岗位分析上限 1-5，默认2")
    parser.add_argument("--city-min", type=int, default=2, help="前序城市有效候选少于此数才扩展，默认2，最多20")
    parser.add_argument("--guangzhou-only", action="store_true", help="只收广州岗位，不扩展上海/深圳")
    parser.add_argument("--watch", action="store_true", help="Job Agent v5：在当前进程运行每日 local 检查")
    parser.add_argument("--daily-at", help="每日检查北京时间 HH:MM，重启可沿用上次设置")
    parser.add_argument("--check-now", action="store_true", help="Job Agent v5：立即 local 检查一次，不改变每日计划")
    args = parser.parse_args()
    if args.agent == "job":
        if args.ui != "terminal" or args.task or args.self_check:
            parser.error("Job Agent 只支持终端；使用 --jd/--resume 或交互式输入。")
        from job_agent.cli import main as job_main
        return job_main(args)
    if args.jd or args.resume or args.list_jobs or args.skill != "all" or args.mode != "local" or args.model or args.llm_dry_run or args.fallback != "local" or args.search or args.search_demo or args.search_fixture or args.sources or args.allow_network or args.search_only or args.search_limit != 8 or args.analyze_limit != 2 or args.city_min != 2 or args.guangzhou_only or args.watch or args.daily_at or args.check_now:
        parser.error("JD、简历和岗位选项需要 --agent job。")
    if args.live and (args.ui == "gui" or args.self_check):
        parser.error("真实模式仅支持交互式终端；GUI 和自检始终无收费。")
    if args.ui == "gui" and (args.task or args.self_check):
        parser.error("GUI 不使用 --task 或 --self-check。")
    try:
        if args.ui == "gui":
            from .gui import run_gui
            return run_gui(args)
        return asyncio.run(run(args))
    except (ValueError, OSError) as error:
        print("启动失败：" + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("平台已停止。")
        return 130
