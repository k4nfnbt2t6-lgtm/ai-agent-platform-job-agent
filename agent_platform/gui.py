"""复用教学 GUI 的本机 HTTP 界面，接到平台共同入口。"""

import asyncio
import threading

import gui_agent

from .runtime import PlatformRuntime
from .terminal_ui import PROJECT, handle_line


class PlatformController:
    def __init__(self, args):
        self.lock = threading.RLock()
        self.events = []
        self.busy = self.closed = False
        self.loop = asyncio.new_event_loop()
        self.runtime = PlatformRuntime(PROJECT, self.receive, state_dir=args.state_dir, session_id=args.session)
        self.worker = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.worker.start()
        self.future = None
        self.running_task = None
        self.runtime.emit("status", "AI Agent Platform v1.0：本地模拟，无收费；输入 /help 查看示例。")

    def receive(self, event):
        with self.lock:
            self.events.append(event)

    def submit(self, text):
        if not isinstance(text, str) or not text.strip():
            return False, "任务不能为空。"
        if text.strip() in ("/quit", "/exit"):
            return False, "请在启动服务的 Terminal 按 Ctrl+C 退出。"
        with self.lock:
            if self.closed or self.busy:
                return False, "已有任务运行或界面已关闭。"
            self.busy = True
            self.future = asyncio.run_coroutine_threadsafe(self.perform(text), self.loop)
        return True, "已提交"

    async def perform(self, text):
        self.running_task = asyncio.current_task()
        try:
            await handle_line(self.runtime, text)
        except ValueError as error:
            self.runtime.emit("error", str(error))
        finally:
            self.running_task = None
            with self.lock:
                self.busy = False

    def snapshot(self):
        with self.lock:
            return {"busy": self.busy, "demo": True,
                    "events": [{"kind": e.kind, "text": e.text} for e in self.events[-300:]]}

    def close(self):
        with self.lock:
            self.closed = True
            if self.future and not self.future.done():
                self.future.cancel()
        async def finish():
            # 等待执行协程的 finally，再关闭数据库与事件循环。
            task = self.running_task
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await self.runtime.close()
        asyncio.run_coroutine_threadsafe(finish(), self.loop).result(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.worker.join(timeout=5)
        self.loop.close()


def run_gui(args):
    controller = PlatformController(args)
    server = None
    # 只替换本进程页面的文字，原 gui_agent.py 与原教学入口保留。
    gui_agent.PAGE = gui_agent.PAGE.replace("ai-agent-lab GUI", "AI Agent Platform v1.0")
    gui_agent.PAGE = gui_agent.PAGE.replace("ai-agent-lab 最小 GUI", "AI Agent Platform v1.0")
    gui_agent.PAGE = gui_agent.PAGE.replace("37 + 58 / 查看当前项目目录", "输入 /help 或自然语言任务")
    gui_agent.PAGE = gui_agent.PAGE.replace("界面只提交任务和显示事件；不要输入密钥或私人资料。",
        "输入 /help 查看示例。Diff 确认用 /approve 申请编号，拒绝用 /reject 申请编号。不要输入密钥。")
    try:
        server, url = gui_agent.create_server(controller)
        print("平台本机 GUI：" + url, flush=True)
        print("关闭网页不会停止服务；回到 Terminal 按 Ctrl+C 退出。", flush=True)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("GUI 已停止。")
    finally:
        if server:
            server.server_close()
        controller.close()
    return 0
